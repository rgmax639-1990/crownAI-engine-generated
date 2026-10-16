"""Public website content: home, products, rates, footer disclosures, notices, CMS pages, documents.

Only PUBLISHED content versions are served. Unknown locales are 404; content missing in
Hindi falls back to English with ``is_fallback: true``. Responses are cacheable for a
short time (the CMS publish step emits an ``isr.revalidate`` outbox event).
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, Query, Response

import content_seed  # noqa: F401  (registers the CMS content seed, order 20)
from content_service import (CATEGORY_LABELS, LOAN_RATE_TYPES, PAGE_TYPES, PRODUCT_CATEGORIES, RATE_PRODUCT_LABELS,
                             alternates, live_rate_for, placeholders, publish_due, published_items, query_locale,
                             render_placeholders, require_locale, resolve_published, slab_label, tenor_label)
from core import api_error, fmt_date, get_or_404, invalid, jload, many, one, today_ist
from db import get_db
from objectstore import get_bytes
from pdfgen import Section, make_pdf
from rates_service import published_rates

router = APIRouter(tags=["content"])

CACHE_CONTROL = "public, max-age=60, stale-while-revalidate=300"


def _cache(response: Response) -> None:
    response.headers["Cache-Control"] = CACHE_CONTROL


# ------------------------------------------------------------------ rates
def rates_payload(conn: sqlite3.Connection, locale: str) -> dict:
    rows = published_rates(conn)
    labels = RATE_PRODUCT_LABELS[locale]
    savings = [
        {"id": r["id"], "slab_min_paise": r["slab_min_paise"], "slab_max_paise": r["slab_max_paise"],
         "label": slab_label(r["slab_min_paise"], r["slab_max_paise"], locale), "rate_percent": r["rate_percent"]}
        for r in rows if r["product_type"] == "savings"
    ]
    fd: dict[tuple, dict] = {}
    for r in rows:
        if r["product_type"] not in ("fd_general", "fd_senior"):
            continue
        key = (r["tenor_min_days"], r["tenor_max_days"], r["slab_min_paise"], r["slab_max_paise"])
        entry = fd.setdefault(key, {
            "tenor_min_days": r["tenor_min_days"], "tenor_max_days": r["tenor_max_days"],
            "slab_min_paise": r["slab_min_paise"], "slab_max_paise": r["slab_max_paise"],
            "label": tenor_label(r["tenor_min_days"], r["tenor_max_days"], locale),
            "general_rate_percent": None, "senior_rate_percent": None,
        })
        entry["general_rate_percent" if r["product_type"] == "fd_general" else "senior_rate_percent"] = r["rate_percent"]
    fd_rows = sorted(fd.values(), key=lambda e: (e["tenor_min_days"] or 0, e["slab_min_paise"] or 0))
    rd = [
        {"id": r["id"], "tenor_min_days": r["tenor_min_days"], "tenor_max_days": r["tenor_max_days"],
         "label": tenor_label(r["tenor_min_days"], r["tenor_max_days"], locale), "rate_percent": r["rate_percent"]}
        for r in rows if r["product_type"] == "rd"
    ]
    loans = [
        {"id": r["id"], "product_type": r["product_type"], "label": labels[r["product_type"]], "benchmark": r["benchmark"],
         "benchmark_rate_percent": r["benchmark_rate_percent"], "spread_percent": r["spread_percent"],
         "effective_rate_percent": r["rate_percent"], "slab_min_paise": r["slab_min_paise"], "slab_max_paise": r["slab_max_paise"],
         "slab_label": slab_label(r["slab_min_paise"], r["slab_max_paise"], locale),
         "tenor_label": tenor_label(r["tenor_min_days"], r["tenor_max_days"], locale)}
        for r in rows if r["product_type"] in LOAN_RATE_TYPES
    ]
    loans.sort(key=lambda e: (LOAN_RATE_TYPES.index(e["product_type"]), e["slab_min_paise"] or 0))
    nri = [
        {"id": r["id"], "product_type": r["product_type"], "label": labels[r["product_type"]],
         "currency": "USD" if r["product_type"] == "fcnr" else "INR", "tenor_min_days": r["tenor_min_days"],
         "tenor_max_days": r["tenor_max_days"], "tenor_label": tenor_label(r["tenor_min_days"], r["tenor_max_days"], locale),
         "rate_percent": r["rate_percent"]}
        for r in rows if r["product_type"] in ("nre_fd", "fcnr")
    ]
    as_on = max((r["as_on_date"] for r in rows), default=None)
    version_ids = sorted({r["content_version_id"] for r in rows})
    meta = None
    if version_ids:
        meta = one(conn, """SELECT v.id, v.version_no, v.published_at, i.needs_compliance_verification
                            FROM content_versions v JOIN content_items i ON i.id = v.content_item_id WHERE v.id = ?""",
                   (version_ids[0],))
    hi = locale == "hi"
    return {
        "locale": locale,
        "as_on_date": as_on,
        "version_id": meta["id"] if meta else None,
        "version_no": meta["version_no"] if meta else None,
        "published_at": meta["published_at"] if meta else None,
        "needs_compliance_verification": bool(meta["needs_compliance_verification"]) if meta else False,
        "savings": savings,
        "fd": fd_rows,
        "rd": rd,
        "loans": loans,
        "nri": nri,
        "notes": [
            "दरें प्रति वर्ष % में हैं। वरिष्ठ नागरिक (60 वर्ष+) को सामान्य दर से 0.50% अधिक।" if hi
            else "Rates are % per annum. Senior citizens (60 years+) get 0.50% over the general rate.",
            "फ्लोटिंग ऋण दर = बेंचमार्क (EBLR/MCLR) + स्प्रेड; बेंचमार्क बदलने पर दर बदलती है।" if hi
            else "Floating loan rate = benchmark (EBLR/MCLR) + spread; it changes when the benchmark is reset.",
            "पुरानी दरों का संग्रह अनुरोध पर उपलब्ध है।" if hi else "An archive of earlier rates is available on request.",
        ],
        "empty": not rows,
    }


def rates_archive(conn: sqlite3.Connection) -> list[dict]:
    return many(
        conn,
        """SELECT v.id AS version_id, v.version_no, v.status, v.published_at, MAX(r.as_on_date) AS as_on_date
           FROM content_versions v JOIN content_items i ON i.id = v.content_item_id AND i.content_type = 'rate_table'
           LEFT JOIN rates r ON r.content_version_id = v.id
           WHERE v.status IN ('published', 'superseded')
           GROUP BY v.id ORDER BY v.published_at DESC, v.version_no DESC""",
    )


def rates_summary(conn: sqlite3.Connection, locale: str) -> dict:
    """Compact live-rates widget for the home page."""
    p = rates_payload(conn, locale)
    best_fd = max(p["fd"], key=lambda e: e["general_rate_percent"] or 0, default=None)
    best_rd = max(p["rd"], key=lambda e: e["rate_percent"], default=None)
    best_sav = max(p["savings"], key=lambda e: e["rate_percent"], default=None)
    home = [ln for ln in p["loans"] if ln["product_type"] == "home_loan"]
    home_best = min(home, key=lambda e: e["effective_rate_percent"], default=None)
    return {
        "as_on_date": p["as_on_date"],
        "savings": best_sav,
        "fd": best_fd,
        "rd": best_rd,
        "home_loan": home_best,
        "needs_compliance_verification": p["needs_compliance_verification"],
        "empty": p["empty"],
    }


@router.get("/public/rates")
def public_rates(response: Response, locale: Optional[str] = Query(None), archive: bool = Query(False),
                 conn: sqlite3.Connection = Depends(get_db)):
    loc = query_locale(locale)
    publish_due(conn)
    _cache(response)
    out = rates_payload(conn, loc)
    if archive:
        out["archive"] = rates_archive(conn)
    return out


@router.get("/public/rates/pdf")
def public_rates_pdf(conn: sqlite3.Connection = Depends(get_db)):
    publish_due(conn)
    p = rates_payload(conn, "en")
    if p["empty"]:
        raise api_error(404, "rates_unavailable", "No published interest rates are available right now")
    pct = lambda v: "-" if v is None else f"{v:.2f}%"  # noqa: E731
    sections = [
        Section("Savings account", [], (["Balance slab", "Rate (p.a.)"], [[s["label"], pct(s["rate_percent"])] for s in p["savings"]])),
        Section("Fixed deposit (domestic, below Rs. 3 crore)", [],
                (["Tenor", "General", "Senior citizen"],
                 [[f["label"], pct(f["general_rate_percent"]), pct(f["senior_rate_percent"])] for f in p["fd"]])),
        Section("Recurring deposit", [], (["Tenor", "Rate (p.a.)"], [[r["label"], pct(r["rate_percent"])] for r in p["rd"]])),
        Section("Loans (floating, benchmark-linked)", [],
                (["Loan", "Benchmark", "Benchmark rate", "Spread", "Effective"],
                 [[ln["label"] + ("" if ln["slab_label"] == "All balances" else f" ({ln['slab_label']})"), ln["benchmark"],
                   pct(ln["benchmark_rate_percent"]), pct(ln["spread_percent"]), pct(ln["effective_rate_percent"])] for ln in p["loans"]]),
                widths=[62, 25, 32, 25, 30]),
        Section("NRI deposits", [], (["Deposit", "Tenor", "Rate (p.a.)"],
                                     [[n["label"], n["tenor_label"], pct(n["rate_percent"])] for n in p["nri"]])),
    ]
    data = make_pdf(f"Interest rates as on {fmt_date(p['as_on_date'])}", sections,
                    footer_note="Rates are per annum and subject to change; the rate on the date of booking/sanction applies. "
                                "Synthetic rates for demonstration. An archive of earlier rates is available on request.")
    name = f"example-bank-interest-rates-{p['as_on_date'] or today_ist().isoformat()}.pdf"
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": CACHE_CONTROL})


# ------------------------------------------------------------------ products
def product_card(rec: dict, rates: list[dict], locale: str) -> dict:
    body = rec["body"]
    hero = body.get("hero") or {}
    return {
        "slug": rec["slug"],
        "item_id": rec["item_id"],
        "content_locale": rec["locale"],
        "is_fallback": rec.get("is_fallback", False),
        "category": body.get("category"),
        "product_code": body.get("product_code"),
        "title": hero.get("title"),
        "subtitle": hero.get("subtitle"),
        "rate_label": hero.get("rate_label"),
        "live_rate": live_rate_for(rates, body.get("rate_product_type"), locale),
        "highlights": (body.get("benefits") or [])[:3],
        "order": body.get("order", 999),
    }


def product_cards(conn: sqlite3.Connection, locale: str, category: str | None = None) -> list[dict]:
    rates = published_rates(conn)
    cards = [product_card(r, rates, locale) for r in published_items(conn, "product_page", locale)]
    if category:
        cards = [c for c in cards if c["category"] == category]
    cards.sort(key=lambda c: (PRODUCT_CATEGORIES.index(c["category"]) if c["category"] in PRODUCT_CATEGORIES else 99,
                              c["order"], c["title"] or ""))
    return cards


@router.get("/public/products")
def public_products(response: Response, category: Optional[str] = Query(None), locale: Optional[str] = Query(None),
                    conn: sqlite3.Connection = Depends(get_db)):
    loc = query_locale(locale)
    if category and category not in PRODUCT_CATEGORIES:
        raise invalid("category", f"Category must be one of: {', '.join(PRODUCT_CATEGORIES)}")
    publish_due(conn)
    _cache(response)
    all_cards = product_cards(conn, loc)
    counts = {c: sum(1 for x in all_cards if x["category"] == c) for c in PRODUCT_CATEGORIES}
    items = [c for c in all_cards if not category or c["category"] == category]
    return {
        "locale": loc,
        "category": category,
        "categories": [{"key": c, "label": CATEGORY_LABELS[loc][c], "count": counts[c]} for c in PRODUCT_CATEGORIES],
        "items": items,
    }


def public_document_ids(conn: sqlite3.Connection, translation_group_id: str) -> set[str]:
    """Document ids referenced by the PUBLISHED bodies of every item in a translation group."""
    ids: set[str] = set()
    for r in many(conn, """SELECT v.body FROM content_items i JOIN content_versions v ON v.id = i.published_version_id
                           WHERE i.translation_group_id = ?""", (translation_group_id,)):
        for d in (jload(r["body"], {}) or {}).get("document_ids") or []:
            ids.add(str(d))
    return ids


def document_public(d: dict) -> dict:
    return {"id": d["id"], "doc_type": d["doc_type"], "locale": d["locale"], "file_name": d["file_name"],
            "content_type": d["content_type"], "size_bytes": d["size_bytes"], "created_at": d["created_at"],
            "href": f"/public/documents/{d['id']}"}


@router.get("/public/products/{slug}")
def public_product(slug: str, response: Response, locale: Optional[str] = Query(None),
                   conn: sqlite3.Connection = Depends(get_db)):
    loc = query_locale(locale)
    publish_due(conn)
    rec, fallback = resolve_published(conn, slug, loc, ("product_page",))
    if not rec:
        raise api_error(404, "product_not_found", "We couldn't find this product. It may have been renamed or withdrawn.")
    _cache(response)
    body = render_placeholders(rec["body"], placeholders(conn, loc))
    doc_ids = public_document_ids(conn, rec["translation_group_id"])
    docs = []
    if doc_ids:
        marks = ",".join("?" for _ in doc_ids)
        docs = [document_public(d) for d in many(
            conn, f"""SELECT * FROM documents WHERE owner_type = 'content_item' AND id IN ({marks})
                      ORDER BY doc_type, locale""", tuple(doc_ids))]
    rates = published_rates(conn)
    category = body.get("category")
    return {
        "locale": loc,
        "content_locale": rec["locale"],
        "is_fallback": fallback,
        "slug": rec["slug"],
        "item_id": rec["item_id"],
        "version_id": rec["version_id"],
        "published_at": rec["published_at"],
        "needs_compliance_verification": rec["needs_compliance_verification"],
        "category_label": CATEGORY_LABELS[loc].get(category, category),
        "body": body,
        "live_rate": live_rate_for(rates, body.get("rate_product_type"), loc),
        "documents": docs,
        "alternates": alternates(conn, rec["translation_group_id"]),
    }


@router.get("/public/documents/{document_id}")
def public_document(document_id: str, conn: sqlite3.Connection = Depends(get_db)):
    doc = get_or_404(conn, "documents", document_id, "Document")
    if doc["owner_type"] != "content_item":
        raise api_error(404, "not_found", "Document not found")
    item = one(conn, "SELECT * FROM content_items WHERE id = ?", (doc["owner_id"],))
    if not item or not item["published_version_id"] or doc["id"] not in public_document_ids(conn, item["translation_group_id"]):
        raise api_error(404, "not_found", "Document not found")
    try:
        data = get_bytes(doc["storage_key"])
    except FileNotFoundError:
        raise api_error(404, "file_missing", "The file is not available right now. Please try again later.")
    name = (doc["file_name"] or f"{doc['doc_type']}-{doc['locale']}.pdf").replace('"', "")
    return Response(content=data, media_type=doc["content_type"] or "application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{name}"', "Cache-Control": CACHE_CONTROL,
                             "X-Content-SHA256": doc["sha256"]})


# ------------------------------------------------------------------ home
def notice_list(conn: sqlite3.Connection, locale: str, kind: str, limit: int = 20) -> list[dict]:
    out = []
    for r in published_items(conn, kind, locale):
        b = r["body"]
        if b.get("listed") is False:
            continue
        out.append({"slug": r["slug"], "content_type": r["content_type"], "content_locale": r["locale"],
                    "is_fallback": r["is_fallback"], "title": b.get("title"), "summary": b.get("summary"),
                    "date": b.get("date") or (r["published_at"] or "")[:10], "important": bool(b.get("important")),
                    "published_at": r["published_at"], "href": f"/{locale}/pages/{r['slug']}"})
    out.sort(key=lambda n: n["date"] or "", reverse=True)
    out.sort(key=lambda n: not n["important"])
    return out[:limit]


@router.get("/public/home/{locale}")
def public_home(locale: str, response: Response, conn: sqlite3.Connection = Depends(get_db)):
    require_locale(locale)
    publish_due(conn)
    rec, fallback = resolve_published(conn, "home", locale, ("home_section",))
    if not rec:
        raise api_error(404, "home_not_found", "Home page content has not been published yet")
    _cache(response)
    body = render_placeholders(rec["body"], placeholders(conn, locale))
    cards = product_cards(conn, locale)
    wanted = (body.get("highlights") or {}).get("product_slugs") or []
    by_slug = {c["slug"]: c for c in cards}
    highlights = [by_slug[s] for s in wanted if s in by_slug] or cards[:6]
    return {
        "locale": locale,
        "content_locale": rec["locale"],
        "is_fallback": fallback,
        "version_id": rec["version_id"],
        "published_at": rec["published_at"],
        "needs_compliance_verification": rec["needs_compliance_verification"],
        "body": body,
        "highlights": highlights,
        "products": cards,
        "rates": rates_summary(conn, locale),
        "notices": notice_list(conn, locale, "notice", limit=3),
        "alternates": alternates(conn, rec["translation_group_id"]),
    }


# ------------------------------------------------------------------ footer, notices, pages, faqs
@router.get("/public/footer/{locale}")
def public_footer(locale: str, response: Response, conn: sqlite3.Connection = Depends(get_db)):
    require_locale(locale)
    publish_due(conn)
    rec, fallback = resolve_published(conn, "footer", locale, ("footer_disclosure",))
    if not rec:
        raise api_error(404, "footer_not_found", "Regulatory disclosures have not been published yet")
    _cache(response)
    body = render_placeholders(rec["body"], placeholders(conn, locale))
    return {
        "locale": locale,
        "version_id": rec["version_id"],
        "published_at": rec["published_at"],
        "sections": body.get("sections") or {},
        "is_fallback": fallback,
        "needs_compliance_verification": rec["needs_compliance_verification"],
    }


@router.get("/public/notices")
def public_notices(response: Response, locale: Optional[str] = Query(None), type: str = Query("notice"),
                   limit: int = Query(20, ge=1, le=100), conn: sqlite3.Connection = Depends(get_db)):
    loc = query_locale(locale)
    if type not in ("notice", "policy"):
        raise invalid("type", "Type must be 'notice' or 'policy'")
    publish_due(conn)
    _cache(response)
    return {"locale": loc, "type": type, "items": notice_list(conn, loc, type, limit)}


@router.get("/public/pages/{locale}/{slug}")
def public_page(locale: str, slug: str, response: Response, conn: sqlite3.Connection = Depends(get_db)):
    require_locale(locale)
    publish_due(conn)
    rec, fallback = resolve_published(conn, slug, locale, PAGE_TYPES)
    if not rec:
        raise api_error(404, "page_not_found", "We couldn't find this page.")
    _cache(response)
    body = render_placeholders(rec["body"], placeholders(conn, locale))
    return {
        "locale": locale,
        "content_locale": rec["locale"],
        "is_fallback": fallback,
        "slug": rec["slug"],
        "content_type": rec["content_type"],
        "version_id": rec["version_id"],
        "published_at": rec["published_at"],
        "needs_compliance_verification": rec["needs_compliance_verification"],
        "body": body,
        "alternates": alternates(conn, rec["translation_group_id"]),
    }


@router.get("/public/faqs")
def public_faqs(response: Response, locale: Optional[str] = Query(None), category: Optional[str] = Query(None),
                product_slug: Optional[str] = Query(None), conn: sqlite3.Connection = Depends(get_db)):
    """Published FAQs (also used by search and the assistant)."""
    loc = query_locale(locale)
    publish_due(conn)
    _cache(response)
    items = []
    for r in published_items(conn, "faq", loc):
        b = r["body"]
        if category and b.get("category") != category:
            continue
        if product_slug and b.get("product_slug") != product_slug:
            continue
        items.append({"slug": r["slug"], "content_locale": r["locale"], "is_fallback": r["is_fallback"],
                      "question": b.get("question"), "answer": b.get("answer"), "category": b.get("category"),
                      "product_slug": b.get("product_slug"), "keywords": b.get("keywords") or []})
    items.sort(key=lambda f: (f["category"] or "", f["question"] or ""))
    return {"locale": loc, "items": items}

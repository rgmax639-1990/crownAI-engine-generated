"""CMS domain helpers shared by the public content, CMS and search routers.

Rules enforced here:
  * Only the PUBLISHED version of a content item is ever served publicly.
  * Publishing supersedes the previous published version, points
    ``content_items.published_version_id`` at the new one, records an
    ``isr.revalidate`` outbox event and an audit event.
  * Scheduled publishing is applied lazily: :func:`publish_due` runs at the start of
    public content requests and the CMS lists, publishing every approved version whose
    ``scheduled_publish_at`` has passed.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from audit import audit
from cms_seed import LEGAL_REQUIRED_TYPES
from core import api_error, get_reg_value, invalid, jdump, jload, many, new_id, now_iso, one, parse_date, today_ist

LOCALES = ("en", "hi")
CONTENT_TYPES = ("home_section", "product_page", "rate_table", "notice", "policy", "footer_disclosure", "faq", "branch_page",
                 "offer", "consent_notice", "regulatory_value")
TEMPLATES = ("product_savings", "product_fd", "product_loan", "product_card", "notice", "policy", "generic")
PRODUCT_TEMPLATES = ("product_savings", "product_fd", "product_loan", "product_card", "generic")
PRODUCT_CATEGORIES = ("accounts", "deposits", "loans", "cards", "nri", "wealth")
PAGE_TYPES = ("policy", "notice", "offer", "consent_notice")
RATE_PRODUCT_TYPES = ("savings", "fd_general", "fd_senior", "rd", "home_loan", "personal_loan", "vehicle_loan", "msme_loan",
                      "nre_fd", "fcnr")
LOAN_RATE_TYPES = ("home_loan", "personal_loan", "vehicle_loan", "msme_loan")
BENCHMARKS = ("none", "EBLR", "MCLR", "RLLR")
DOC_TYPES_CMS = ("kfs", "mitc", "schedule_of_charges", "policy")
RATE_TABLE_SLUG = "rates"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,80}$")

CATEGORY_LABELS = {
    "en": {"accounts": "Accounts", "deposits": "Deposits", "loans": "Loans", "cards": "Cards", "nri": "NRI", "wealth": "Wealth"},
    "hi": {"accounts": "खाते", "deposits": "जमा", "loans": "ऋण", "cards": "कार्ड", "nri": "एनआरआई", "wealth": "वेल्थ"},
}
RATE_PRODUCT_LABELS = {
    "en": {"savings": "Savings account", "fd_general": "Fixed deposit", "fd_senior": "Fixed deposit (senior citizen)",
           "rd": "Recurring deposit", "home_loan": "Home loan", "personal_loan": "Personal loan", "vehicle_loan": "Vehicle loan",
           "msme_loan": "MSME loan", "nre_fd": "NRE fixed deposit", "fcnr": "FCNR(B) deposit (USD)"},
    "hi": {"savings": "बचत खाता", "fd_general": "सावधि जमा (एफडी)", "fd_senior": "सावधि जमा (वरिष्ठ नागरिक)",
           "rd": "आवर्ती जमा (आरडी)", "home_loan": "होम लोन", "personal_loan": "पर्सनल लोन", "vehicle_loan": "वाहन ऋण",
           "msme_loan": "एमएसएमई ऋण", "nre_fd": "एनआरई सावधि जमा", "fcnr": "एफसीएनआर(बी) जमा (USD)"},
}


# ------------------------------------------------------------------ locale & formatting
def require_locale(locale: str) -> str:
    if locale not in LOCALES:
        raise api_error(404, "locale_not_found", f"Language '{locale}' is not available. Use one of: {', '.join(LOCALES)}")
    return locale


def query_locale(locale: str | None) -> str:
    """Locale from a query parameter: missing means English, unknown is 404."""
    return require_locale(locale or "en")


def needs_legal(content_type: str) -> bool:
    return content_type in LEGAL_REQUIRED_TYPES


def ready_to_publish(content_type: str, status: str) -> bool:
    if needs_legal(content_type):
        return status == "legal_approved"
    return status in ("checker_approved", "legal_approved")


def fmt_lakh(paise: Any, locale: str = "en") -> str:
    """₹5 lakh / ₹1.5 crore (Hindi: लाख / करोड़); smaller amounts in full rupees."""
    try:
        rupees = int(paise) / 100
    except (TypeError, ValueError):
        return "-"
    if rupees >= 1e7:
        v, unit = rupees / 1e7, ("करोड़" if locale == "hi" else "crore")
    elif rupees >= 1e5:
        v, unit = rupees / 1e5, ("लाख" if locale == "hi" else "lakh")
    else:
        return f"₹{int(rupees):,}"
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return f"₹{s} {unit}"


def placeholders(conn: sqlite3.Connection, locale: str) -> dict[str, str]:
    return {"dicgc_limit": fmt_lakh(get_reg_value(conn, "dicgc_limit_paise", 50000000), locale)}


def render_placeholders(value: Any, tokens: dict[str, str]) -> Any:
    """Replace ``{{key}}`` placeholders (CMS-held regulatory values) anywhere in a body."""
    if isinstance(value, str):
        for k, v in tokens.items():
            value = value.replace("{{" + k + "}}", v)
        return value
    if isinstance(value, dict):
        return {k: render_placeholders(v, tokens) for k, v in value.items()}
    if isinstance(value, list):
        return [render_placeholders(v, tokens) for v in value]
    return value


def item_title(content_type: str, body: dict | None) -> str:
    b = body or {}
    hero = b.get("hero") if isinstance(b.get("hero"), dict) else {}
    for candidate in (hero.get("title"), b.get("title"), b.get("question"), b.get("label"), b.get("key")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return content_type.replace("_", " ").capitalize()


# ------------------------------------------------------------------ published content lookups
def published_record(conn: sqlite3.Connection, slug: str, locale: str, content_types: tuple[str, ...]) -> dict | None:
    marks = ",".join("?" for _ in content_types)
    row = one(
        conn,
        f"""SELECT i.id AS item_id, i.content_type, i.template, i.slug, i.locale, i.translation_group_id,
                   i.needs_compliance_verification, v.id AS version_id, v.version_no, v.body, v.published_at
            FROM content_items i JOIN content_versions v ON v.id = i.published_version_id AND v.status = 'published'
            WHERE i.slug = ? AND i.locale = ? AND i.content_type IN ({marks})""",
        (slug, locale, *content_types),
    )
    if row:
        row["body"] = jload(row["body"], {}) or {}
        row["needs_compliance_verification"] = bool(row["needs_compliance_verification"])
    return row


def resolve_published(conn: sqlite3.Connection, slug: str, locale: str, content_types: tuple[str, ...]) -> tuple[dict | None, bool]:
    """Published record in ``locale``; falls back to English (second value True) when missing."""
    rec = published_record(conn, slug, locale, content_types)
    if rec:
        return rec, False
    if locale != "en":
        rec = published_record(conn, slug, "en", content_types)
        if rec:
            return rec, True
    return None, False


def published_items(conn: sqlite3.Connection, content_type: str, locale: str) -> list[dict]:
    """Published items of a type in ``locale`` plus English items whose translation is missing (flagged)."""
    rows = many(
        conn,
        """SELECT i.id AS item_id, i.content_type, i.template, i.slug, i.locale, i.translation_group_id,
                  i.needs_compliance_verification, v.id AS version_id, v.body, v.published_at
           FROM content_items i JOIN content_versions v ON v.id = i.published_version_id AND v.status = 'published'
           WHERE i.content_type = ? AND i.locale IN (?, 'en')""",
        (content_type, locale),
    )
    in_locale = {r["translation_group_id"] for r in rows if r["locale"] == locale}
    out = []
    for r in rows:
        if r["locale"] != locale and r["translation_group_id"] in in_locale:
            continue
        r["body"] = jload(r["body"], {}) or {}
        r["is_fallback"] = r["locale"] != locale
        r["needs_compliance_verification"] = bool(r["needs_compliance_verification"])
        out.append(r)
    return out


def alternates(conn: sqlite3.Connection, translation_group_id: str) -> list[dict]:
    return many(
        conn,
        """SELECT locale, slug FROM content_items WHERE translation_group_id = ? AND published_version_id IS NOT NULL
           ORDER BY locale""",
        (translation_group_id,),
    )


# ------------------------------------------------------------------ publishing
def isr_paths(item: dict) -> list[str]:
    loc, slug, ct = item["locale"], item["slug"], item["content_type"]
    if ct == "product_page":
        return [f"/{loc}/products/{slug}", f"/{loc}/products", f"/{loc}"]
    if ct == "rate_table":
        return [p for lc in LOCALES for p in (f"/{lc}/rates", f"/{lc}", f"/{lc}/tools")]
    if ct in ("footer_disclosure", "regulatory_value"):
        return [f"/{lc}" for lc in LOCALES] + ["layout"]
    if ct == "home_section":
        return [f"/{loc}"]
    if ct in PAGE_TYPES:
        return [f"/{loc}/pages/{slug}", f"/{loc}"]
    if ct == "faq":
        return [f"/{loc}/search"]
    return [f"/{loc}"]


def publish_version(conn: sqlite3.Connection, version_id: str, actor_id: str | None, *, actor_type: str = "staff",
                    scheduled: bool = False, ip: str | None = None) -> dict:
    v = one(conn, "SELECT * FROM content_versions WHERE id = ?", (version_id,))
    item = one(conn, "SELECT * FROM content_items WHERE id = ?", (v["content_item_id"],))  # type: ignore[index]
    now = now_iso()
    prev = item["published_version_id"]
    superseded = many(conn, "SELECT id FROM content_versions WHERE content_item_id = ? AND status = 'published' AND id != ?",
                      (item["id"], version_id))
    conn.execute("UPDATE content_versions SET status = 'superseded' WHERE content_item_id = ? AND status = 'published' AND id != ?",
                 (item["id"], version_id))
    for s in superseded:
        audit(conn, "content", "content.superseded", actor_type=actor_type, actor_id=actor_id, entity_type="content_version",
              entity_id=s["id"], details={"superseded_by": version_id, "slug": item["slug"], "locale": item["locale"]}, ip=ip)
    conn.execute("UPDATE content_versions SET status = 'published', published_at = ?, published_by = ? WHERE id = ?",
                 (now, actor_id, version_id))
    conn.execute("UPDATE content_items SET published_version_id = ? WHERE id = ?", (version_id, item["id"]))
    conn.execute(
        "INSERT INTO outbox (id, topic, payload, created_at) VALUES (?, 'isr.revalidate', ?, ?)",
        (new_id(), jdump({"paths": isr_paths(item), "content_item_id": item["id"], "version_id": version_id,
                          "content_type": item["content_type"], "slug": item["slug"], "locale": item["locale"]}), now),
    )
    audit(conn, "content", "content.published", actor_type=actor_type, actor_id=actor_id, entity_type="content_version",
          entity_id=version_id, details={"slug": item["slug"], "locale": item["locale"], "content_type": item["content_type"],
                                         "version_no": v["version_no"], "scheduled": scheduled,  # type: ignore[index]
                                         "superseded_version_id": prev if prev != version_id else None}, ip=ip)
    return {"version_id": version_id, "published_at": now, "superseded_version_id": prev if prev != version_id else None}


def publish_due(conn: sqlite3.Connection) -> int:
    """Publish approved versions whose scheduled time has passed. Returns how many went live."""
    rows = many(
        conn,
        """SELECT v.id, v.status, i.content_type FROM content_versions v JOIN content_items i ON i.id = v.content_item_id
           WHERE v.scheduled_publish_at IS NOT NULL AND v.scheduled_publish_at <= ?
             AND v.status IN ('checker_approved', 'legal_approved')
           ORDER BY v.scheduled_publish_at, v.version_no""",
        (now_iso(),),
    )
    done = 0
    for r in rows:
        if not ready_to_publish(r["content_type"], r["status"]):
            continue
        ev = one(conn, """SELECT actor_id FROM audit_events WHERE entity_type = 'content_version' AND entity_id = ?
                          AND action = 'content.publish_scheduled' ORDER BY seq DESC LIMIT 1""", (r["id"],))
        publish_version(conn, r["id"], ev["actor_id"] if ev else None, actor_type="staff" if ev else "system", scheduled=True)
        done += 1
    return done


# ------------------------------------------------------------------ diff
def _flatten(value: Any, prefix: str, out: dict[str, Any]) -> None:
    if isinstance(value, dict) and value:
        for k, v in value.items():
            _flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(value, list) and value:
        for i, v in enumerate(value):
            _flatten(v, f"{prefix}[{i}]", out)
    else:
        out[prefix or "(body)"] = value


def diff_bodies(before: Any, after: Any) -> list[dict]:
    """Field-level changes between two bodies: [{path, change, before, after}]."""
    a: dict[str, Any] = {}
    b: dict[str, Any] = {}
    if before not in (None, {}, []):
        _flatten(before, "", a)
    if after not in (None, {}, []):
        _flatten(after, "", b)
    out = []
    for path in sorted(set(a) | set(b)):
        if path in a and path in b:
            if a[path] != b[path]:
                out.append({"path": path, "change": "changed", "before": a[path], "after": b[path]})
        elif path in b:
            out.append({"path": path, "change": "added", "before": None, "after": b[path]})
        else:
            out.append({"path": path, "change": "removed", "before": a[path], "after": None})
    return out


# ------------------------------------------------------------------ validation
def parse_body(raw: Any) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise invalid("body", f"The content is not valid JSON ({exc.msg} at line {exc.lineno}, column {exc.colno})")
    if not isinstance(raw, dict):
        raise invalid("body", "The content must be a JSON object")
    return raw


def _str(body: dict, path: str, *, required: bool, label: str) -> None:
    cur: Any = body
    for part in path.split("."):
        cur = cur.get(part) if isinstance(cur, dict) else None
    if cur is None or cur == "":
        if required:
            raise invalid(f"body.{path}", f"{label} is required")
        return
    if not isinstance(cur, str):
        raise invalid(f"body.{path}", f"{label} must be text")


def _list(body: dict, key: str, *, item_keys: tuple[str, ...] = (), strings: bool = False, required: bool = False,
          label: str = "") -> None:
    val = body.get(key)
    if val is None:
        if required:
            raise invalid(f"body.{key}", f"Add at least one item to {label or key}")
        return
    if not isinstance(val, list):
        raise invalid(f"body.{key}", f"{label or key} must be a list")
    if required and not val:
        raise invalid(f"body.{key}", f"Add at least one item to {label or key}")
    for i, entry in enumerate(val):
        if strings:
            if not isinstance(entry, str) or not entry.strip():
                raise invalid(f"body.{key}[{i}]", f"Item {i + 1} of {label or key} must be non-empty text")
        elif item_keys:
            if not isinstance(entry, dict):
                raise invalid(f"body.{key}[{i}]", f"Item {i + 1} of {label or key} must be an object")
            for k in item_keys:
                if not isinstance(entry.get(k), str) or not entry.get(k, "").strip():
                    raise invalid(f"body.{key}[{i}].{k}", f"Item {i + 1} of {label or key}: '{k}' is required")


def validate_rate_row(row: Any, prefix: str = "") -> dict:
    """Normalise one rate row. Error field names are ``<prefix>.<key>`` (``row.<key>`` without a prefix)."""
    field = prefix or "row"
    if not isinstance(row, dict):
        raise invalid(field, "Each rate row must be an object")

    pt = row.get("product_type")
    if pt not in RATE_PRODUCT_TYPES:
        raise invalid(f"{field}.product_type", f"Choose a product type: {', '.join(RATE_PRODUCT_TYPES)}")
    out: dict[str, Any] = {"product_type": pt}
    for k in ("tenor_min_days", "tenor_max_days", "slab_min_paise", "slab_max_paise"):
        v = row.get(k)
        if v in (None, ""):
            out[k] = None
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v or v < 0:
            raise invalid(f"{field}.{k}", "Enter a whole number of 0 or more")
        out[k] = int(v)
    if out["tenor_min_days"] is not None and out["tenor_max_days"] is not None and out["tenor_min_days"] > out["tenor_max_days"]:
        raise invalid(f"{field}.tenor_max_days", "Maximum tenor must be at least the minimum tenor")
    if out["slab_min_paise"] is not None and out["slab_max_paise"] is not None and out["slab_min_paise"] > out["slab_max_paise"]:
        raise invalid(f"{field}.slab_max_paise", "Upper slab must be at least the lower slab")
    benchmark = row.get("benchmark") or "none"
    if benchmark not in BENCHMARKS:
        raise invalid(f"{field}.benchmark", f"Benchmark must be one of {', '.join(BENCHMARKS)}")
    out["benchmark"] = benchmark

    def pct(key: str, lo: float, hi: float) -> float | None:
        v = row.get(key)
        if v in (None, ""):
            return None
        try:
            f = round(float(v), 2)
        except (TypeError, ValueError):
            raise invalid(f"{field}.{key}", "Enter a number, for example 7.25")
        if not (lo <= f <= hi):
            raise invalid(f"{field}.{key}", f"Must be between {lo} and {hi}")
        return f

    rate = pct("rate_percent", 0.0, 30.0)
    if benchmark != "none":
        b, s = pct("benchmark_rate_percent", 0.0, 30.0), pct("spread_percent", -5.0, 15.0)
        if b is None:
            raise invalid(f"{field}.benchmark_rate_percent", f"Enter the {benchmark} benchmark rate")
        if s is None:
            raise invalid(f"{field}.spread_percent", "Enter the spread over the benchmark")
        effective = round(b + s, 2)
        if rate is not None and abs(rate - effective) > 0.001:
            raise invalid(f"{field}.rate_percent", f"Effective rate must equal benchmark + spread ({effective:.2f}%)")
        out.update(rate_percent=effective, benchmark_rate_percent=b, spread_percent=s)
    else:
        if rate is None:
            raise invalid(f"{field}.rate_percent", "Enter the interest rate")
        out.update(rate_percent=rate, benchmark_rate_percent=None, spread_percent=None)
    return out


def rate_key(row: dict) -> tuple:
    return (row["product_type"], row.get("tenor_min_days"), row.get("tenor_max_days"), row.get("slab_min_paise"),
            row.get("slab_max_paise"))


def validate_body(content_type: str, body: Any, *, strict: bool) -> dict:
    """Validate a version body for its content type. ``strict`` (submit) also enforces required fields."""
    body = parse_body(body)
    req = strict
    if content_type == "product_page":
        hero = body.get("hero")
        if hero is not None and not isinstance(hero, dict):
            raise invalid("body.hero", "Hero must be an object with title, subtitle and rate_label")
        if req and not isinstance(hero, dict):
            raise invalid("body.hero.title", "Hero title is required")
        _str(body, "hero.title", required=req, label="Hero title")
        _str(body, "hero.subtitle", required=False, label="Hero subtitle")
        _str(body, "hero.rate_label", required=False, label="Rate label")
        cat = body.get("category")
        if (cat is not None or req) and cat not in PRODUCT_CATEGORIES:
            raise invalid("body.category", f"Category must be one of: {', '.join(PRODUCT_CATEGORIES)}")
        _list(body, "benefits", strings=True, required=req, label="Benefits")
        _list(body, "schedule_of_charges", item_keys=("item", "charge"), label="Schedule of charges")
        _list(body, "eligibility", strings=True, label="Eligibility")
        _list(body, "documents", strings=True, label="Documents required")
        _list(body, "faqs", item_keys=("q", "a"), label="FAQs")
        cta = body.get("cta")
        if cta is not None:
            if not isinstance(cta, dict):
                raise invalid("body.cta", "CTA must be an object")
            for k in ("apply", "request_call"):
                if k in cta and not isinstance(cta[k], bool):
                    raise invalid(f"body.cta.{k}", f"'{k}' must be true or false")
            if cta.get("apply") and req:
                _str(body, "cta.apply_href", required=True, label="Apply link")
        if body.get("rate_product_type") not in (None, "") and body["rate_product_type"] not in RATE_PRODUCT_TYPES:
            raise invalid("body.rate_product_type", f"Rate product must be one of {', '.join(RATE_PRODUCT_TYPES)}")
        if body.get("document_ids") is not None and not isinstance(body["document_ids"], list):
            raise invalid("body.document_ids", "document_ids must be a list")
    elif content_type == "home_section":
        _str(body, "hero.title", required=req, label="Hero title")
    elif content_type in PAGE_TYPES:
        _str(body, "title", required=req, label="Title")
        secs = body.get("sections")
        if secs is not None:
            if not isinstance(secs, list):
                raise invalid("body.sections", "Sections must be a list")
            for i, s in enumerate(secs):
                if not isinstance(s, dict):
                    raise invalid(f"body.sections[{i}]", f"Section {i + 1} must be an object")
                for k in ("paragraphs", "list"):
                    if s.get(k) is not None and not isinstance(s[k], list):
                        raise invalid(f"body.sections[{i}].{k}", f"Section {i + 1}: '{k}' must be a list")
        elif req and not body.get("summary"):
            raise invalid("body.sections", "Add at least one section")
    elif content_type == "footer_disclosure":
        secs = body.get("sections")
        if not isinstance(secs, dict):
            raise invalid("body.sections", "Footer content must have a 'sections' object")
        if req:
            for k in ("dicgc", "regulatory_notice", "escalation_matrix", "principal_nodal_officer", "rbi_ombudsman",
                      "safe_banking", "link_groups", "copyright"):
                if k not in secs:
                    raise invalid(f"body.sections.{k}", f"Footer section '{k}' is required")
            levels = (secs.get("escalation_matrix") or {}).get("levels")
            if not isinstance(levels, list) or not levels:
                raise invalid("body.sections.escalation_matrix.levels", "Add the escalation levels")
            if not str((secs.get("rbi_ombudsman") or {}).get("url", "")).startswith("https://"):
                raise invalid("body.sections.rbi_ombudsman.url", "Enter the RBI Ombudsman https:// link")
            if not isinstance(secs.get("link_groups"), list):
                raise invalid("body.sections.link_groups", "Link groups must be a list")
    elif content_type == "faq":
        _str(body, "question", required=req, label="Question")
        _str(body, "answer", required=req, label="Answer")
    elif content_type == "rate_table":
        rows = body.get("rows")
        if not isinstance(rows, list) or (req and not rows):
            raise invalid("body.rows", "Rate table needs a list of rate rows")
        seen = set()
        norm = []
        for i, r in enumerate(rows):
            n = validate_rate_row(r, f"body.rows[{i}]")
            k = rate_key(n)
            if k in seen:
                raise invalid(f"body.rows[{i}]", f"Row {i + 1} duplicates another row for the same product, tenor and slab")
            seen.add(k)
            norm.append(n)
        body["rows"] = norm
        if body.get("as_on_date") and not parse_date(body["as_on_date"]):
            raise invalid("body.as_on_date", "Enter the as-on date as YYYY-MM-DD")
    elif content_type == "regulatory_value":
        if req and ("value" not in body or not body.get("key")):
            raise invalid("body.value", "A regulatory value needs 'key' and 'value'")
    return body


def write_rate_rows(conn: sqlite3.Connection, version_id: str, body: dict) -> None:
    """Replace the rate rows of a (draft) rate_table version with ``body['rows']``."""
    as_on = parse_date(body.get("as_on_date")) or today_ist()
    conn.execute("DELETE FROM rates WHERE content_version_id = ?", (version_id,))
    for r in body.get("rows") or []:
        conn.execute(
            """INSERT INTO rates (id, content_version_id, product_type, tenor_min_days, tenor_max_days, slab_min_paise,
               slab_max_paise, rate_percent, benchmark, benchmark_rate_percent, spread_percent, as_on_date)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (new_id(), version_id, r["product_type"], r.get("tenor_min_days"), r.get("tenor_max_days"), r.get("slab_min_paise"),
             r.get("slab_max_paise"), r["rate_percent"], r.get("benchmark") or "none", r.get("benchmark_rate_percent"),
             r.get("spread_percent"), as_on.isoformat()),
        )


# ------------------------------------------------------------------ rate presentation
def _years(d: int, locale: str) -> str:
    y = d // 365
    if locale == "hi":
        return f"{y} वर्ष"
    return f"{y} year" + ("s" if y != 1 else "")


def _days(d: int, locale: str) -> str:
    if d >= 365 and d % 365 == 0:
        return _years(d, locale)
    return f"{d} दिन" if locale == "hi" else f"{d} day" + ("s" if d != 1 else "")


def tenor_label(min_d: int | None, max_d: int | None, locale: str = "en") -> str:
    hi = locale == "hi"
    if min_d is None and max_d is None:
        return "सभी अवधि" if hi else "All tenors"
    if max_d is None:
        return f"{_days(min_d, locale)} और अधिक" if hi else f"{_days(min_d, locale)} and above"  # type: ignore[arg-type]
    lo = _days(min_d or 0, locale)
    if (max_d + 1) % 365 == 0 and max_d + 1 >= 365:
        upper = _years(max_d + 1, locale)
        return f"{lo} से {upper} से कम" if hi else f"{lo} to less than {upper}"
    return f"{lo} से {_days(max_d, locale)}" if hi else f"{lo} to {_days(max_d, locale)}"


def slab_label(min_p: int | None, max_p: int | None, locale: str = "en") -> str:
    hi = locale == "hi"
    if min_p in (None, 0) and max_p is None:
        return "सभी राशि" if hi else "All balances"
    if min_p in (None, 0):
        top = fmt_lakh(max_p + 1, locale)  # type: ignore[operator]
        return f"{top} से कम" if hi else f"Below {top}"
    if max_p is None:
        return f"{fmt_lakh(min_p, locale)} और अधिक" if hi else f"{fmt_lakh(min_p, locale)} and above"
    return (f"{fmt_lakh(min_p, locale)} से {fmt_lakh(max_p + 1, locale)} से कम" if hi
            else f"{fmt_lakh(min_p, locale)} to below {fmt_lakh(max_p + 1, locale)}")


def live_rate_for(rows: list[dict], product_type: str | None, locale: str) -> dict | None:
    """Headline live rate for a product card: 'Up to x%' for deposits, 'From x%' for loans."""
    if not product_type:
        return None
    sel = [r for r in rows if r["product_type"] == product_type]
    if not sel:
        return None
    hi = locale == "hi"
    if product_type in LOAN_RATE_TYPES:
        best = min(sel, key=lambda r: r["rate_percent"])
        label = f"{best['rate_percent']:.2f}% प्रति वर्ष से शुरू" if hi else f"From {best['rate_percent']:.2f}% p.a."
        kind = "from"
    else:
        best = max(sel, key=lambda r: r["rate_percent"])
        label = f"{best['rate_percent']:.2f}% प्रति वर्ष तक" if hi else f"Up to {best['rate_percent']:.2f}% p.a."
        kind = "up_to"
    return {"product_type": product_type, "rate_percent": best["rate_percent"], "kind": kind, "label": label,
            "benchmark": best.get("benchmark"), "as_on_date": best.get("as_on_date")}

"""CMS with maker-checker(-legal) workflow.

Roles:
  content_maker      creates items, saves drafts, drafts rate changes, attaches PDFs, submits
  content_checker    approves/rejects the "checker" stage; may publish
  legal_approver     approves/rejects the "legal" stage (product_page, policy, footer_disclosure,
                     consent_notice, regulatory_value); may publish
  compliance_reviewer read access; sets/clears the compliance-verification flag

Rules: a version is editable only while it is the maker's own draft; once submitted it is
immutable. Nobody approves, rejects or publishes a version they made (403). Publishing
needs every required approval, supersedes the previous published version, emits an
``isr.revalidate`` outbox event and can be scheduled (applied lazily by ``publish_due``).
Every transition is written to the audit log (category ``content``).
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from audit import audit_ctx
from content_service import (BENCHMARKS, CONTENT_TYPES, DOC_TYPES_CMS, LOCALES, PRODUCT_CATEGORIES, PRODUCT_TEMPLATES,
                             RATE_PRODUCT_LABELS, RATE_PRODUCT_TYPES, RATE_TABLE_SLUG, SLUG_RE, TEMPLATES, diff_bodies,
                             item_title, needs_legal, publish_due, publish_version, rate_key, ready_to_publish, validate_body,
                             validate_rate_row, write_rate_rows)
from core import (api_error, forbidden, get_or_404, invalid, iso, jdump, jload, many, new_id, now_iso, one, parse_date,
                  parse_iso, today_ist, utcnow)
from db import get_db
from objectstore import consume_upload, create_document, get_bytes
from rates_service import published_rates
from security import AuthContext, require_staff

router = APIRouter(tags=["cms"])

CMS_ROLES = ("content_maker", "content_checker", "legal_approver", "compliance_reviewer")
reader = require_staff(*CMS_ROLES)
maker = require_staff("content_maker")
approver = require_staff("content_checker", "legal_approver")
RATE_FIELDS = ("product_type", "tenor_min_days", "tenor_max_days", "slab_min_paise", "slab_max_paise", "rate_percent",
               "benchmark", "benchmark_rate_percent", "spread_percent")


class Body(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class ContentCreateIn(Body):
    content_type: Optional[str] = None
    template: Optional[str] = None
    slug: Optional[str] = None
    locale: str = "en"
    translate_from_item_id: Optional[str] = None
    body: Any = None
    change_note: Optional[str] = None
    needs_compliance_verification: bool = False


class DraftIn(Body):
    body: Any = None
    change_note: Optional[str] = None


class ApproveIn(Body):
    comment: Optional[str] = None


class RejectIn(Body):
    reason: Optional[str] = None


class PublishIn(Body):
    scheduled_at: Optional[str] = None
    comment: Optional[str] = None


class DocumentIn(Body):
    upload_ref: str
    doc_type: str
    locale: str = "en"


class ComplianceIn(Body):
    needs_compliance_verification: bool


class RateIn(Body):
    product_type: str
    tenor_min_days: Optional[int] = None
    tenor_max_days: Optional[int] = None
    slab_min_paise: Optional[int] = None
    slab_max_paise: Optional[int] = None
    rate_percent: Optional[float] = None
    benchmark: str = "none"
    benchmark_rate_percent: Optional[float] = None
    spread_percent: Optional[float] = None
    as_on_date: Optional[str] = None
    change_note: Optional[str] = None
    remove: bool = False
    submit: bool = False


# ------------------------------------------------------------------ templates
def _skeleton(content_type: str, template: str, locale: str) -> dict:
    today = today_ist().isoformat()
    if content_type == "product_page":
        cat = {"product_fd": "deposits", "product_loan": "loans", "product_card": "cards"}.get(template, "accounts")
        return {"category": cat, "product_code": "", "order": 50, "rate_product_type": "",
                "hero": {"title": "", "subtitle": "", "rate_label": ""}, "benefits": [""],
                "schedule_of_charges": [{"item": "", "charge": ""}], "eligibility": [""], "documents": [""],
                "faqs": [{"q": "", "a": ""}], "cta": {"apply": False, "request_call": True, "apply_href": ""},
                "calculator_href": "", "disclaimer": "", "keywords": [], "document_ids": []}
    if content_type in ("notice", "policy", "offer", "consent_notice"):
        return {"title": "", "summary": "", "date": today, "important": False,
                "sections": [{"heading": "", "paragraphs": [""], "list": []}]}
    if content_type == "faq":
        return {"question": "", "answer": "", "category": "", "product_slug": "", "keywords": []}
    if content_type == "home_section":
        return {"hero": {"title": "", "subtitle": ""}}
    if content_type == "footer_disclosure":
        return {"sections": {"dicgc": {"title": "", "text": ""}, "regulatory_notice": {"text": ""},
                             "escalation_matrix": {"title": "", "intro": "", "levels": []},
                             "principal_nodal_officer": {"title": "", "name": ""},
                             "rbi_ombudsman": {"title": "", "text": "", "url": "https://cms.rbi.org.in"},
                             "safe_banking": {"title": "", "text": "", "links": []}, "link_groups": [], "copyright": ""}}
    if content_type == "regulatory_value":
        return {"key": "", "value": None, "unit": "", "label": "", "source": "",
                "verification_note": "To be verified by the bank's compliance team against the official source"}
    return {}


TEMPLATE_DEFS = [
    {"content_type": "product_page", "label": "Product page", "templates": list(PRODUCT_TEMPLATES), "editor": "product"},
    {"content_type": "notice", "label": "Notice", "templates": ["notice"], "editor": "page"},
    {"content_type": "policy", "label": "Policy / page", "templates": ["policy", "generic"], "editor": "page"},
    {"content_type": "offer", "label": "Offer", "templates": ["generic"], "editor": "page"},
    {"content_type": "faq", "label": "FAQ", "templates": ["generic"], "editor": "faq"},
    {"content_type": "home_section", "label": "Home page section", "templates": ["generic"], "editor": "json"},
    {"content_type": "footer_disclosure", "label": "Footer disclosures", "templates": ["generic"], "editor": "json"},
    {"content_type": "consent_notice", "label": "Consent notice", "templates": ["generic", "policy"], "editor": "json"},
    {"content_type": "regulatory_value", "label": "Regulatory value", "templates": ["generic"], "editor": "json"},
    {"content_type": "branch_page", "label": "Branch page", "templates": ["generic"], "editor": "json"},
]
CREATABLE = {d["content_type"]: d for d in TEMPLATE_DEFS}


@router.get("/cms/templates")
def cms_templates(ctx: AuthContext = Depends(reader)):
    return {
        "locales": list(LOCALES),
        "product_categories": list(PRODUCT_CATEGORIES),
        "rate_product_types": list(RATE_PRODUCT_TYPES),
        "doc_types": list(DOC_TYPES_CMS),
        "content_types": [
            {**d, "needs_legal": needs_legal(d["content_type"]),
             "skeletons": {t: _skeleton(d["content_type"], t, "en") for t in d["templates"]}}
            for d in TEMPLATE_DEFS
        ],
    }


# ------------------------------------------------------------------ helpers
def _names(conn: sqlite3.Connection, ids: list[str | None]) -> dict[str, str]:
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    return {r["id"]: r["display_name"] or r["email"] for r in many(conn, f"SELECT id, display_name, email FROM users WHERE id IN ({marks})", ids)}


def _person(uid: str | None, names: dict[str, str]) -> dict | None:
    return {"id": uid, "name": names.get(uid, "Unknown user")} if uid else None


def _version_out(v: dict, names: dict[str, str], include_body: bool = True) -> dict:
    out = {
        "id": v["id"], "content_item_id": v["content_item_id"], "version_no": v["version_no"], "status": v["status"],
        "change_note": v["change_note"], "maker": _person(v["maker_id"], names), "checker": _person(v["checker_id"], names),
        "legal_approver": _person(v["legal_approver_id"], names), "created_at": v["created_at"], "submitted_at": v["submitted_at"],
        "checker_approved_at": v["checker_approved_at"], "legal_approved_at": v["legal_approved_at"],
        "scheduled_publish_at": v["scheduled_publish_at"], "published_at": v["published_at"],
        "published_by": _person(v["published_by"], names), "rejected_by": _person(v["rejected_by"], names),
        "rejected_reason": v["rejected_reason"],
    }
    if include_body:
        out["body"] = jload(v["body"], {})
    return out


def _item_out(item: dict) -> dict:
    return {"id": item["id"], "content_type": item["content_type"], "template": item["template"], "slug": item["slug"],
            "locale": item["locale"], "translation_group_id": item["translation_group_id"],
            "published_version_id": item["published_version_id"],
            "needs_compliance_verification": bool(item["needs_compliance_verification"]),
            "needs_legal": needs_legal(item["content_type"]), "created_at": item["created_at"]}


def _version(conn: sqlite3.Connection, version_id: str) -> tuple[dict, dict]:
    v = get_or_404(conn, "content_versions", version_id, "Content version")
    item = one(conn, "SELECT * FROM content_items WHERE id = ?", (v["content_item_id"],))
    return v, item  # type: ignore[return-value]


def _published_body(conn: sqlite3.Connection, item: dict) -> dict | None:
    if not item["published_version_id"]:
        return None
    row = one(conn, "SELECT body FROM content_versions WHERE id = ?", (item["published_version_id"],))
    return jload(row["body"], {}) if row else None


def _stage(item: dict, v: dict) -> str | None:
    s = v["status"]
    if s == "submitted":
        return "checker"
    if s == "checker_approved":
        return "legal" if needs_legal(item["content_type"]) else "publish"
    if s == "legal_approved":
        return "publish"
    return None


def _can_act(ctx: AuthContext, item: dict, v: dict) -> tuple[bool, str | None]:
    stage = _stage(item, v)
    if not stage:
        return False, "This version is not awaiting approval."
    if v["maker_id"] == ctx.user_id:
        return False, "You created this version, so a different colleague must review it (maker-checker)."
    if stage == "checker" and not ctx.has_role("content_checker"):
        return False, "This stage needs a content checker."
    if stage == "legal" and not ctx.has_role("legal_approver"):
        return False, "This stage needs a legal approver."
    if stage == "publish" and not ctx.has_role("content_checker", "legal_approver"):
        return False, "Publishing needs a content checker or legal approver."
    return True, None


def _own_draft(conn: sqlite3.Connection, item_id: str, user_id: str) -> dict | None:
    return one(conn, """SELECT * FROM content_versions WHERE content_item_id = ? AND status = 'draft' AND maker_id = ?
                        ORDER BY version_no DESC LIMIT 1""", (item_id, user_id))


def _save_draft(conn: sqlite3.Connection, ctx: AuthContext, item: dict, body: dict, change_note: str | None,
                request: Request) -> dict:
    """Update the caller's own draft, or create a new draft version. Returns the version row."""
    own = _own_draft(conn, item["id"], ctx.user_id)  # type: ignore[arg-type]
    if own:
        conn.execute("UPDATE content_versions SET body = ?, change_note = COALESCE(?, change_note) WHERE id = ?",
                     (jdump(body), change_note, own["id"]))
        vid, action, no = own["id"], "content.draft_updated", own["version_no"]
    else:
        no = (one(conn, "SELECT COALESCE(MAX(version_no), 0) AS n FROM content_versions WHERE content_item_id = ?",
                  (item["id"],)) or {"n": 0})["n"] + 1
        vid = new_id()
        conn.execute(
            """INSERT INTO content_versions (id, content_item_id, version_no, body, change_note, status, maker_id, created_at)
               VALUES (?,?,?,?,?, 'draft', ?, ?)""",
            (vid, item["id"], no, jdump(body), change_note, ctx.user_id, now_iso()),
        )
        action = "content.version_created"
    if item["content_type"] == "rate_table":
        write_rate_rows(conn, vid, body)
    audit_ctx(conn, ctx, "content", action, entity_type="content_version", entity_id=vid,
              details={"slug": item["slug"], "locale": item["locale"], "version_no": no, "change_note": change_note},
              request=request)
    return one(conn, "SELECT * FROM content_versions WHERE id = ?", (vid,))  # type: ignore[return-value]


def _submit(conn: sqlite3.Connection, ctx: AuthContext, item: dict, v: dict, request: Request) -> None:
    if v["status"] != "draft":
        raise api_error(409, "version_locked", "This version was already submitted and can no longer change. Save a new draft instead.")
    if v["maker_id"] != ctx.user_id:
        raise forbidden("Only the maker who created this draft can submit it", "not_your_draft")
    body = validate_body(item["content_type"], jload(v["body"], {}), strict=True)
    if item["content_type"] == "rate_table":
        conn.execute("UPDATE content_versions SET body = ? WHERE id = ?", (jdump(body), v["id"]))
        write_rate_rows(conn, v["id"], body)
    conn.execute("UPDATE content_versions SET status = 'submitted', submitted_at = ? WHERE id = ?", (now_iso(), v["id"]))
    audit_ctx(conn, ctx, "content", "content.submitted", entity_type="content_version", entity_id=v["id"],
              details={"slug": item["slug"], "locale": item["locale"], "version_no": v["version_no"],
                       "next_stage": "checker"}, request=request)


def _version_detail(conn: sqlite3.Connection, ctx: AuthContext, v: dict, item: dict) -> dict:
    names = _names(conn, [v["maker_id"], v["checker_id"], v["legal_approver_id"], v["published_by"], v["rejected_by"]])
    published = _published_body(conn, item)
    body = jload(v["body"], {})
    can, why = _can_act(ctx, item, v)
    out = {
        "item": _item_out(item),
        "title": item_title(item["content_type"], body),
        "version": _version_out(v, names),
        "published_version_id": item["published_version_id"],
        "published_body": published,
        "diff": diff_bodies(published, body) if v["id"] != item["published_version_id"] else [],
        "stage": _stage(item, v),
        "viewer_is_maker": v["maker_id"] == ctx.user_id,
        "can_act": can,
        "cannot_act_reason": why,
        "ready_to_publish": ready_to_publish(item["content_type"], v["status"]),
        "required_approvals": ["checker", "legal"] if needs_legal(item["content_type"]) else ["checker"],
    }
    if item["content_type"] == "rate_table":
        out["rate_changes"] = _rate_changes(conn, v["id"])
    return out


# ------------------------------------------------------------------ content list / detail / create
@router.get("/cms/content")
def cms_list(content_type: Optional[str] = Query(None), locale: Optional[str] = Query(None), status: Optional[str] = Query(None),
             q: Optional[str] = Query(None), ctx: AuthContext = Depends(reader), conn: sqlite3.Connection = Depends(get_db)):
    publish_due(conn)
    where, params = [], []
    if content_type:
        if content_type not in CONTENT_TYPES:
            raise invalid("content_type", "Unknown content type")
        where.append("i.content_type = ?")
        params.append(content_type)
    if locale:
        if locale not in LOCALES:
            raise invalid("locale", "Unknown locale")
        where.append("i.locale = ?")
        params.append(locale)
    rows = many(conn, f"""
        SELECT i.*, lv.id AS latest_id, lv.version_no AS latest_version_no, lv.status AS latest_status, lv.body AS latest_body,
               lv.maker_id AS latest_maker_id, lv.created_at AS latest_created_at, lv.scheduled_publish_at,
               pv.version_no AS published_version_no, pv.published_at
        FROM content_items i
        JOIN content_versions lv ON lv.id = (SELECT id FROM content_versions WHERE content_item_id = i.id ORDER BY version_no DESC LIMIT 1)
        LEFT JOIN content_versions pv ON pv.id = i.published_version_id
        {"WHERE " + " AND ".join(where) if where else ""}
        ORDER BY i.content_type, i.slug, i.locale""", params)
    names = _names(conn, [r["latest_maker_id"] for r in rows])
    items = []
    needle = (q or "").strip().lower()
    for r in rows:
        title = item_title(r["content_type"], jload(r["latest_body"], {}))
        if status and r["latest_status"] != status:
            continue
        if needle and needle not in title.lower() and needle not in r["slug"]:
            continue
        items.append({
            **_item_out(r), "title": title, "latest_version_id": r["latest_id"], "latest_version_no": r["latest_version_no"],
            "latest_status": r["latest_status"], "latest_maker": _person(r["latest_maker_id"], names),
            "latest_created_at": r["latest_created_at"], "scheduled_publish_at": r["scheduled_publish_at"],
            "published_version_no": r["published_version_no"], "published_at": r["published_at"],
        })
    return {"items": items, "total": len(items)}


@router.get("/cms/content/{item_id}")
def cms_item(item_id: str, ctx: AuthContext = Depends(reader), conn: sqlite3.Connection = Depends(get_db)):
    publish_due(conn)
    item = get_or_404(conn, "content_items", item_id, "Content item")
    versions = many(conn, "SELECT * FROM content_versions WHERE content_item_id = ? ORDER BY version_no DESC", (item_id,))
    names = _names(conn, [x for v in versions for x in (v["maker_id"], v["checker_id"], v["legal_approver_id"],
                                                         v["published_by"], v["rejected_by"])])
    siblings = many(conn, """SELECT i.id, i.locale, i.slug, i.published_version_id,
                             (SELECT status FROM content_versions WHERE content_item_id = i.id ORDER BY version_no DESC LIMIT 1) AS latest_status
                             FROM content_items i WHERE i.translation_group_id = ? ORDER BY i.locale""",
                      (item["translation_group_id"],))
    published = _published_body(conn, item) or {}
    own = _own_draft(conn, item_id, ctx.user_id)  # type: ignore[arg-type]
    draft_ids = set((jload(own["body"], {}) or {}).get("document_ids") or []) if own else set()
    pub_ids = set(published.get("document_ids") or [])
    docs = [{"id": d["id"], "doc_type": d["doc_type"], "locale": d["locale"], "file_name": d["file_name"],
             "size_bytes": d["size_bytes"], "sha256": d["sha256"], "created_at": d["created_at"],
             "in_published": d["id"] in pub_ids, "in_my_draft": d["id"] in draft_ids,
             "href": f"/cms/documents/{d['id']}"}
            for d in many(conn, "SELECT * FROM documents WHERE owner_type = 'content_item' AND owner_id = ? ORDER BY created_at",
                          (item_id,))]
    latest = versions[0] if versions else None
    return {
        "item": _item_out(item),
        "title": item_title(item["content_type"], jload(latest["body"], {}) if latest else {}),
        "versions": [_version_out(v, names) for v in versions],
        "published_body": published if item["published_version_id"] else None,
        "my_draft_version_id": own["id"] if own else None,
        "translations": [{"id": s["id"], "locale": s["locale"], "slug": s["slug"], "published": bool(s["published_version_id"]),
                          "latest_status": s["latest_status"]} for s in siblings],
        "missing_locales": [lc for lc in LOCALES if lc not in {s["locale"] for s in siblings}],
        "documents": docs,
        "viewer": {"can_edit": ctx.has_role("content_maker") and item["content_type"] in (*CREATABLE, "rate_table"),
                   "can_flag_compliance": ctx.has_role("compliance_reviewer", "legal_approver")},
    }


@router.post("/cms/content", status_code=201)
def cms_create(payload: ContentCreateIn, request: Request, ctx: AuthContext = Depends(maker),
               conn: sqlite3.Connection = Depends(get_db)):
    if payload.locale not in LOCALES:
        raise invalid("locale", f"Locale must be one of {', '.join(LOCALES)}")
    group = new_id()
    default_body = None
    if payload.translate_from_item_id:
        src = get_or_404(conn, "content_items", payload.translate_from_item_id, "Source content item")
        content_type, template, group = src["content_type"], src["template"], src["translation_group_id"]
        slug = payload.slug or src["slug"]
        if one(conn, "SELECT 1 AS x FROM content_items WHERE translation_group_id = ? AND locale = ?", (group, payload.locale)):
            raise api_error(409, "translation_exists", "This item already has a version in that language", field="locale")
        latest = one(conn, "SELECT body FROM content_versions WHERE content_item_id = ? ORDER BY version_no DESC LIMIT 1", (src["id"],))
        default_body = jload(latest["body"], {}) if latest else None
        if isinstance(default_body, dict):
            default_body.pop("document_ids", None)
    else:
        content_type, template, slug = payload.content_type, payload.template, payload.slug
        if content_type == "rate_table":
            raise invalid("content_type", "Interest rates are changed from the Rates tab, not as a new content item")
        if content_type not in CREATABLE:
            raise invalid("content_type", "Choose a content type")
        allowed = CREATABLE[content_type]["templates"]
        template = template or allowed[0]
        if template not in allowed or template not in TEMPLATES:
            raise invalid("template", f"Template for {content_type} must be one of {', '.join(allowed)}")
    if content_type == "rate_table":
        raise invalid("content_type", "Interest rates are changed from the Rates tab")
    if not slug or not SLUG_RE.match(slug):
        raise invalid("slug", "Use 2-80 lowercase letters, numbers and hyphens, for example 'fixed-deposit'")
    if one(conn, "SELECT 1 AS x FROM content_items WHERE slug = ? AND locale = ?", (slug, payload.locale)):
        raise api_error(409, "slug_taken", f"'{slug}' is already used in this language", field="slug")
    raw = payload.body if payload.body is not None else (default_body or _skeleton(content_type, template, payload.locale))  # type: ignore[arg-type]
    body = validate_body(content_type, raw, strict=False)  # type: ignore[arg-type]
    item_id, vid, now = new_id(), new_id(), now_iso()
    conn.execute(
        """INSERT INTO content_items (id, content_type, template, slug, locale, translation_group_id, needs_compliance_verification,
           created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
        (item_id, content_type, template, slug, payload.locale, group, int(payload.needs_compliance_verification), ctx.user_id, now),
    )
    conn.execute(
        """INSERT INTO content_versions (id, content_item_id, version_no, body, change_note, status, maker_id, created_at)
           VALUES (?,?,1,?,?, 'draft', ?, ?)""",
        (vid, item_id, jdump(body), payload.change_note or "New content", ctx.user_id, now),
    )
    audit_ctx(conn, ctx, "content", "content.item_created", entity_type="content_item", entity_id=item_id,
              details={"content_type": content_type, "slug": slug, "locale": payload.locale,
                       "translation_of": payload.translate_from_item_id}, request=request)
    audit_ctx(conn, ctx, "content", "content.version_created", entity_type="content_version", entity_id=vid,
              details={"slug": slug, "locale": payload.locale, "version_no": 1}, request=request)
    return {"item_id": item_id, "version_id": vid, "translation_group_id": group}


@router.post("/cms/content/{item_id}/versions", status_code=201)
def cms_save_draft(item_id: str, payload: DraftIn, request: Request, ctx: AuthContext = Depends(maker),
                   conn: sqlite3.Connection = Depends(get_db)):
    item = get_or_404(conn, "content_items", item_id, "Content item")
    if payload.body is None:
        raise invalid("body", "Content is required")
    body = validate_body(item["content_type"], payload.body, strict=False)
    v = _save_draft(conn, ctx, item, body, payload.change_note, request)
    return _version_detail(conn, ctx, v, item)


@router.post("/cms/content/{item_id}/compliance")
def cms_compliance_flag(item_id: str, payload: ComplianceIn, request: Request,
                        ctx: AuthContext = Depends(require_staff("compliance_reviewer", "legal_approver")),
                        conn: sqlite3.Connection = Depends(get_db)):
    item = get_or_404(conn, "content_items", item_id, "Content item")
    conn.execute("UPDATE content_items SET needs_compliance_verification = ? WHERE id = ?",
                 (int(payload.needs_compliance_verification), item_id))
    audit_ctx(conn, ctx, "content", "content.compliance_flag_set" if payload.needs_compliance_verification else "content.compliance_verified",
              entity_type="content_item", entity_id=item_id, details={"slug": item["slug"], "locale": item["locale"]}, request=request)
    return {"item_id": item_id, "needs_compliance_verification": payload.needs_compliance_verification}


# ------------------------------------------------------------------ documents
@router.post("/cms/content/{item_id}/documents", status_code=201)
def cms_attach_document(item_id: str, payload: DocumentIn, request: Request, ctx: AuthContext = Depends(maker),
                        conn: sqlite3.Connection = Depends(get_db)):
    item = get_or_404(conn, "content_items", item_id, "Content item")
    if payload.doc_type not in DOC_TYPES_CMS:
        raise invalid("doc_type", f"Document type must be one of {', '.join(DOC_TYPES_CMS)}")
    if payload.locale not in LOCALES:
        raise invalid("locale", f"Locale must be one of {', '.join(LOCALES)}")
    up = consume_upload(conn, payload.upload_ref, ctx.user_id, "upload_ref")
    if up["content_type"] != "application/pdf":
        raise invalid("upload_ref", "Upload a PDF file")
    try:
        data = get_bytes(up["storage_key"])
    except FileNotFoundError:
        raise invalid("upload_ref", "Upload not found. Upload the file again.")
    if not data.startswith(b"%PDF"):
        raise invalid("upload_ref", "The file is not a valid PDF")
    doc = create_document(conn, owner_type="content_item", owner_id=item_id, doc_type=payload.doc_type, data=data,
                          file_name=up["file_name"], content_type="application/pdf", locale=payload.locale)
    # The document becomes public only through a version that references it and is approved and published.
    own = _own_draft(conn, item_id, ctx.user_id)  # type: ignore[arg-type]
    base = own or one(conn, "SELECT * FROM content_versions WHERE content_item_id = ? ORDER BY version_no DESC LIMIT 1", (item_id,))
    body = jload(base["body"], {}) if base else {}
    ids = [d for d in (body.get("document_ids") or []) if d != doc["id"]]
    body["document_ids"] = ids + [doc["id"]]
    v = _save_draft(conn, ctx, item, validate_body(item["content_type"], body, strict=False),
                    f"Attached {payload.doc_type.upper()} ({payload.locale})", request)
    audit_ctx(conn, ctx, "content", "content.document_attached", entity_type="content_version", entity_id=v["id"],
              details={"document_id": doc["id"], "doc_type": payload.doc_type, "locale": payload.locale, "sha256": doc["sha256"],
                       "slug": item["slug"]}, request=request)
    return {"document": {k: doc[k] for k in ("id", "doc_type", "locale", "file_name", "size_bytes", "sha256", "created_at")},
            "version_id": v["id"], "version_no": v["version_no"]}


@router.get("/cms/documents/{document_id}")
def cms_document(document_id: str, ctx: AuthContext = Depends(reader), conn: sqlite3.Connection = Depends(get_db)):
    doc = get_or_404(conn, "documents", document_id, "Document")
    if doc["owner_type"] != "content_item":
        raise api_error(404, "not_found", "Document not found")
    try:
        data = get_bytes(doc["storage_key"])
    except FileNotFoundError:
        raise api_error(404, "file_missing", "The stored file is missing")
    name = (doc["file_name"] or "document.pdf").replace('"', "")
    return Response(content=data, media_type=doc["content_type"] or "application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{name}"'})


# ------------------------------------------------------------------ versions & workflow
@router.get("/cms/versions/{version_id}")
def cms_version(version_id: str, ctx: AuthContext = Depends(reader), conn: sqlite3.Connection = Depends(get_db)):
    v, item = _version(conn, version_id)
    return _version_detail(conn, ctx, v, item)


@router.post("/cms/versions/{version_id}/submit")
def cms_submit(version_id: str, request: Request, ctx: AuthContext = Depends(maker), conn: sqlite3.Connection = Depends(get_db)):
    v, item = _version(conn, version_id)
    _submit(conn, ctx, item, v, request)
    v, item = _version(conn, version_id)
    return _version_detail(conn, ctx, v, item)


@router.post("/cms/versions/{version_id}/approve")
def cms_approve(version_id: str, payload: ApproveIn, request: Request, ctx: AuthContext = Depends(approver),
                conn: sqlite3.Connection = Depends(get_db)):
    v, item = _version(conn, version_id)
    stage = _stage(item, v)
    if stage is None:
        raise api_error(409, "not_awaiting_approval", f"This version is {v['status'].replace('_', ' ')} and is not awaiting approval")
    if stage == "publish":
        raise api_error(409, "already_approved", "All approvals are complete. Publish the version or schedule it.")
    if v["maker_id"] == ctx.user_id:
        raise forbidden("You created this version, so you cannot approve it. Another colleague must review it.", "maker_cannot_approve")
    now = now_iso()
    if stage == "checker":
        if not ctx.has_role("content_checker"):
            raise forbidden("The checker stage needs the content checker role", "role_required")
        conn.execute("UPDATE content_versions SET status = 'checker_approved', checker_id = ?, checker_approved_at = ? WHERE id = ?",
                     (ctx.user_id, now, version_id))
        action = "content.checker_approved"
    else:
        if not ctx.has_role("legal_approver"):
            raise forbidden("The legal stage needs the legal approver role", "role_required")
        conn.execute("UPDATE content_versions SET status = 'legal_approved', legal_approver_id = ?, legal_approved_at = ? WHERE id = ?",
                     (ctx.user_id, now, version_id))
        action = "content.legal_approved"
    audit_ctx(conn, ctx, "content", action, entity_type="content_version", entity_id=version_id,
              details={"slug": item["slug"], "locale": item["locale"], "version_no": v["version_no"], "comment": payload.comment},
              request=request)
    v, item = _version(conn, version_id)
    return _version_detail(conn, ctx, v, item)


@router.post("/cms/versions/{version_id}/reject")
def cms_reject(version_id: str, payload: RejectIn, request: Request, ctx: AuthContext = Depends(approver),
               conn: sqlite3.Connection = Depends(get_db)):
    v, item = _version(conn, version_id)
    stage = _stage(item, v)
    if stage is None:
        raise api_error(409, "not_awaiting_approval", f"This version is {v['status'].replace('_', ' ')} and cannot be rejected")
    if v["maker_id"] == ctx.user_id:
        raise forbidden("You created this version, so you cannot review it.", "maker_cannot_approve")
    can, why = _can_act(ctx, item, v)
    if not can:
        raise forbidden(why or "You cannot act on this stage", "role_required")
    reason = (payload.reason or "").strip()
    if len(reason) < 5:
        raise invalid("reason", "Give the maker a reason (at least 5 characters)")
    conn.execute("""UPDATE content_versions SET status = 'rejected', rejected_by = ?, rejected_reason = ?, scheduled_publish_at = NULL
                    WHERE id = ?""", (ctx.user_id, reason[:1000], version_id))
    audit_ctx(conn, ctx, "content", "content.rejected", entity_type="content_version", entity_id=version_id,
              details={"slug": item["slug"], "locale": item["locale"], "version_no": v["version_no"], "stage": stage,
                       "reason": reason[:1000]}, request=request)
    v, item = _version(conn, version_id)
    return _version_detail(conn, ctx, v, item)


@router.post("/cms/versions/{version_id}/publish")
def cms_publish(version_id: str, payload: PublishIn, request: Request, ctx: AuthContext = Depends(approver),
                conn: sqlite3.Connection = Depends(get_db)):
    v, item = _version(conn, version_id)
    if v["status"] == "published":
        raise api_error(409, "already_published", "This version is already live")
    if v["maker_id"] == ctx.user_id:
        raise forbidden("You created this version, so you cannot publish it.", "maker_cannot_publish")
    if not ready_to_publish(item["content_type"], v["status"]):
        missing = "checker approval" if v["status"] in ("draft", "submitted") else "legal approval"
        raise api_error(409, "approvals_pending", f"This version still needs {missing} before it can be published")
    when = None
    if payload.scheduled_at:
        when = parse_iso(payload.scheduled_at)
        if when is None:
            raise invalid("scheduled_at", "Enter a valid date and time")
        if when > utcnow() + timedelta(days=366):
            raise invalid("scheduled_at", "Schedule within the next 12 months")
    if when and when > utcnow() + timedelta(seconds=30):
        conn.execute("UPDATE content_versions SET scheduled_publish_at = ? WHERE id = ?", (iso(when), version_id))
        audit_ctx(conn, ctx, "content", "content.publish_scheduled", entity_type="content_version", entity_id=version_id,
                  details={"slug": item["slug"], "locale": item["locale"], "version_no": v["version_no"],
                           "scheduled_publish_at": iso(when), "comment": payload.comment}, request=request)
        result = {"status": "scheduled", "scheduled_publish_at": iso(when)}
    else:
        res = publish_version(conn, version_id, ctx.user_id, ip=request.client.host if request.client else None)
        result = {"status": "published", **res}
    v, item = _version(conn, version_id)
    return {**result, "detail": _version_detail(conn, ctx, v, item)}


@router.post("/cms/versions/{version_id}/unschedule")
def cms_unschedule(version_id: str, request: Request, ctx: AuthContext = Depends(approver),
                   conn: sqlite3.Connection = Depends(get_db)):
    v, item = _version(conn, version_id)
    if v["status"] == "published" or not v["scheduled_publish_at"]:
        raise api_error(409, "not_scheduled", "This version has no pending scheduled publish")
    if v["maker_id"] == ctx.user_id:
        raise forbidden("You created this version, so you cannot change its publishing.", "maker_cannot_publish")
    conn.execute("UPDATE content_versions SET scheduled_publish_at = NULL WHERE id = ?", (version_id,))
    audit_ctx(conn, ctx, "content", "content.publish_unscheduled", entity_type="content_version", entity_id=version_id,
              details={"slug": item["slug"], "locale": item["locale"], "version_no": v["version_no"]}, request=request)
    v, item = _version(conn, version_id)
    return _version_detail(conn, ctx, v, item)


@router.get("/cms/versions/{version_id}/audit")
def cms_version_audit(version_id: str, ctx: AuthContext = Depends(reader), conn: sqlite3.Connection = Depends(get_db)):
    v, item = _version(conn, version_id)
    events = many(conn, """SELECT seq, id, action, actor_type, actor_id, entity_type, entity_id, details, occurred_at, hash, prev_hash
                           FROM audit_events WHERE category = 'content'
                             AND ((entity_type = 'content_version' AND entity_id = ?) OR (entity_type = 'content_item' AND entity_id = ?))
                           ORDER BY seq""", (version_id, item["id"]))
    names = _names(conn, [e["actor_id"] for e in events])
    return {
        "version_id": version_id,
        "item_id": item["id"],
        "events": [{**e, "details": jload(e["details"], {}), "actor_name": names.get(e["actor_id"]) if e["actor_id"] else "System"}
                   for e in events],
    }


@router.get("/cms/approvals")
def cms_approvals(stage: Optional[str] = Query(None), ctx: AuthContext = Depends(reader), conn: sqlite3.Connection = Depends(get_db)):
    publish_due(conn)
    rows = many(conn, """SELECT v.* FROM content_versions v
                         WHERE v.status IN ('submitted', 'checker_approved', 'legal_approved')
                         ORDER BY COALESCE(v.submitted_at, v.created_at)""")
    items: dict[str, dict] = {}
    names = _names(conn, [r["maker_id"] for r in rows] + [r["checker_id"] for r in rows] + [r["legal_approver_id"] for r in rows])
    out = []
    for v in rows:
        item = items.get(v["content_item_id"]) or one(conn, "SELECT * FROM content_items WHERE id = ?", (v["content_item_id"],))
        items[v["content_item_id"]] = item  # type: ignore[assignment]
        st = _stage(item, v)  # type: ignore[arg-type]
        if stage and st != stage:
            continue
        body = jload(v["body"], {})
        published = _published_body(conn, item)  # type: ignore[arg-type]
        can, why = _can_act(ctx, item, v)  # type: ignore[arg-type]
        entry = {
            "version_id": v["id"], "version_no": v["version_no"], "status": v["status"], "stage": st,
            "item": _item_out(item),  # type: ignore[arg-type]
            "title": item_title(item["content_type"], body),  # type: ignore[index]
            "maker": _person(v["maker_id"], names), "checker": _person(v["checker_id"], names),
            "legal_approver": _person(v["legal_approver_id"], names),
            "submitted_at": v["submitted_at"], "change_note": v["change_note"], "scheduled_publish_at": v["scheduled_publish_at"],
            "viewer_is_maker": v["maker_id"] == ctx.user_id, "can_act": can, "cannot_act_reason": why,
            "body": body, "published_body": published, "diff": diff_bodies(published, body),
        }
        if item["content_type"] == "rate_table":  # type: ignore[index]
            entry["rate_changes"] = _rate_changes(conn, v["id"])
        out.append(entry)
    return {"items": out, "total": len(out)}


# ------------------------------------------------------------------ rates
def _rate_item(conn: sqlite3.Connection) -> dict:
    item = one(conn, "SELECT * FROM content_items WHERE content_type = 'rate_table' AND slug = ? AND locale = 'en'", (RATE_TABLE_SLUG,))
    if not item:
        raise api_error(404, "rate_table_missing", "The interest rate table has not been set up")
    return item


def _rows_of(conn: sqlite3.Connection, version_id: str | None) -> list[dict]:
    if not version_id:
        return []
    return [{k: r[k] for k in RATE_FIELDS} for r in many(
        conn, """SELECT * FROM rates WHERE content_version_id = ?
                 ORDER BY product_type, COALESCE(tenor_min_days, 0), COALESCE(slab_min_paise, 0)""", (version_id,))]


def _rate_changes(conn: sqlite3.Connection, version_id: str) -> list[dict]:
    """Row-level differences between a rate_table version and the live rates."""
    live = {rate_key(r): r for r in ({k: r[k] for k in RATE_FIELDS} for r in published_rates(conn))}
    mine = {rate_key(r): r for r in _rows_of(conn, version_id)}
    out = []
    for key in sorted(set(live) | set(mine), key=lambda k: tuple("" if x is None else str(x).zfill(12) for x in k)):
        a, b = live.get(key), mine.get(key)
        if a and b and all(a[f] == b[f] for f in RATE_FIELDS):
            continue
        out.append({"product_type": key[0], "tenor_min_days": key[1], "tenor_max_days": key[2], "slab_min_paise": key[3],
                    "slab_max_paise": key[4], "change": "changed" if a and b else ("added" if b else "removed"),
                    "before": a, "after": b})
    return out


@router.get("/cms/rates")
def cms_rates(ctx: AuthContext = Depends(reader), conn: sqlite3.Connection = Depends(get_db)):
    publish_due(conn)
    item = _rate_item(conn)
    live_rows = published_rates(conn)
    pending = many(conn, """SELECT * FROM content_versions WHERE content_item_id = ?
                            AND status IN ('draft', 'submitted', 'checker_approved', 'legal_approved') ORDER BY version_no DESC""",
                   (item["id"],))
    names = _names(conn, [p["maker_id"] for p in pending] + [p["checker_id"] for p in pending])
    return {
        "item_id": item["id"],
        "live": {
            "version_id": item["published_version_id"],
            "as_on_date": max((r["as_on_date"] for r in live_rows), default=None),
            "published_at": live_rows[0]["published_at"] if live_rows else None,
            "rows": [{"id": r["id"], **{k: r[k] for k in RATE_FIELDS}, "as_on_date": r["as_on_date"]} for r in live_rows],
        },
        "drafts": [{**_version_out(p, names, include_body=False), "viewer_is_maker": p["maker_id"] == ctx.user_id,
                    "as_on_date": (jload(p["body"], {}) or {}).get("as_on_date"), "rows": _rows_of(conn, p["id"]),
                    "changes": _rate_changes(conn, p["id"])} for p in pending],
        "product_types": [{"key": k, "label": RATE_PRODUCT_LABELS["en"][k]} for k in RATE_PRODUCT_TYPES],
        "benchmarks": list(BENCHMARKS),
    }


@router.post("/cms/rates", status_code=201)
def cms_draft_rate(payload: RateIn, request: Request, ctx: AuthContext = Depends(maker), conn: sqlite3.Connection = Depends(get_db)):
    item = _rate_item(conn)
    row_in = {k: getattr(payload, k) for k in RATE_FIELDS}
    if payload.remove:
        if payload.product_type not in RATE_PRODUCT_TYPES:
            raise invalid("product_type", "Choose a product type")
        row = {**row_in, "benchmark": payload.benchmark or "none"}
    else:
        row = validate_rate_row(row_in)  # 422 field names: row.<field>
    as_on = payload.as_on_date
    if as_on and not parse_date(as_on):
        raise invalid("as_on_date", "Enter the as-on date as YYYY-MM-DD")
    own = _own_draft(conn, item["id"], ctx.user_id)  # type: ignore[arg-type]
    rows = _rows_of(conn, own["id"] if own else item["published_version_id"])
    key = rate_key(row)
    match = [i for i, r in enumerate(rows) if rate_key(r) == key]
    if payload.remove:
        if not match:
            raise api_error(404, "rate_row_not_found", "No rate row matches that product, tenor and slab")
        rows.pop(match[0])
    elif match:
        rows[match[0]] = row
    else:
        rows.append(row)
    body = {"title": "Interest rates",
            "as_on_date": (parse_date(as_on) or today_ist()).isoformat() if as_on else
            ((jload(own["body"], {}) or {}).get("as_on_date") if own else None) or today_ist().isoformat(),
            "rows": rows}
    body = validate_body("rate_table", body, strict=False)
    note = payload.change_note or (f"Remove {row['product_type']} row" if payload.remove else f"Rate change: {row['product_type']}")
    v = _save_draft(conn, ctx, item, body, note, request)
    audit_ctx(conn, ctx, "content", "content.rate_change_drafted", entity_type="content_version", entity_id=v["id"],
              details={"row": row, "removed": payload.remove, "as_on_date": body["as_on_date"]}, request=request)
    if payload.submit:
        _submit(conn, ctx, item, v, request)
        v = one(conn, "SELECT * FROM content_versions WHERE id = ?", (v["id"],))  # type: ignore[assignment]
    names = _names(conn, [v["maker_id"]])
    return {**_version_out(v, names, include_body=False), "rows": _rows_of(conn, v["id"]), "changes": _rate_changes(conn, v["id"]),
            "as_on_date": body["as_on_date"]}

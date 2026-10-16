"""Complaints and grievance redressal (RBI Integrated Ombudsman Scheme aligned).

Levels: branch (grievance officer) -> nodal -> principal nodal -> internal ombudsman, after
which the customer may approach the RBI Ombudsman (cms.rbi.org.in) once the complaint has
been open for ``rbi_ombudsman_days`` (CMS-held regulatory value) without resolution.
Every change is a ``complaint_events`` row; internal notes are never shown to customers.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict

from audit import audit, audit_ctx
from core import (IST, api_error, client_ip, forbidden, get_reg_value, invalid, iso, jload, many, mask_mobile, new_id,
                  normalize_mobile, not_found, now_iso, one, parse_iso, redact_pii, reference_no, require_mobile, utcnow,
                  valid_email)
from db import get_db, register_seed
from notify import notify
from security import AuthContext, require_customer, require_otp, require_staff

router = APIRouter(tags=["grievance"])

CATEGORIES = ("atm_card", "digital_payments", "account", "loan", "deposit", "charges", "fraud", "staff_behaviour", "other")
LEVELS = ("branch", "nodal", "principal_nodal", "internal_ombudsman")
LEVEL_ROLE = {"branch": "grievance_officer", "nodal": "nodal_officer", "principal_nodal": "principal_nodal_officer",
              "internal_ombudsman": "internal_ombudsman"}
GRIEVANCE_ROLES = tuple(LEVEL_ROLE.values())
LEVEL_LABELS = {
    "en": {"branch": "Branch / Grievance Officer", "nodal": "Nodal Officer", "principal_nodal": "Principal Nodal Officer",
           "internal_ombudsman": "Internal Ombudsman", "rbi_ombudsman": "RBI Ombudsman"},
    "hi": {"branch": "शाखा / शिकायत अधिकारी", "nodal": "नोडल अधिकारी", "principal_nodal": "प्रधान नोडल अधिकारी",
           "internal_ombudsman": "आंतरिक लोकपाल", "rbi_ombudsman": "आरबीआई लोकपाल"},
}
DEFAULT_CONTACTS = {
    "branch": "Your home branch or grievance@example.bank.in · 1800-000-0000",
    "nodal": "nodal.officer@example.bank.in · 022-0000-0001",
    "principal_nodal": "pno@example.bank.in · 022-0000-0002",
    "internal_ombudsman": "Reviewed internally before a complaint is fully or partly rejected",
    "rbi_ombudsman": "https://cms.rbi.org.in · 14448",
}
CLOSED_STATES = ("resolved", "closed", "referred_rbi_ombudsman")
OPEN_STATES = ("open", "in_progress", "escalated", "reopened")
RBI_CMS_URL = "https://cms.rbi.org.in"
REOPEN_WINDOW_DAYS = 30
STAFF_TRANSITIONS = ("in_progress", "resolved", "escalated", "closed", "reassigned")
NAME_RE = re.compile(r"^[A-Za-zऀ-ॿ][A-Za-zऀ-ॿ .'\-]{1,99}$")
TICKET_RE = re.compile(r"^CMP-\d{8}-[A-Z0-9]{6}$")
_TRACK_SECRET = (os.getenv("ASR_TRACK_SECRET") or secrets.token_hex(32)).encode("utf-8")
TRACK_TOKEN_MINUTES = 15


class Body(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class ComplaintIn(Body):
    name: str = ""
    mobile: str = ""
    otp: Optional[str] = None
    email: Optional[str] = None
    category: str = ""
    account_last4: Optional[str] = None
    description: str = ""
    language: Optional[str] = "en"


class TrackIn(Body):
    ticket_id: str = ""
    mobile: str = ""
    otp: Optional[str] = None


class EscalateIn(Body):
    ticket_id: str = ""
    mobile: str = ""
    otp: Optional[str] = None
    track_token: Optional[str] = None
    action: str = "escalate"
    reason: Optional[str] = None


class TransitionIn(Body):
    to_status: str = ""
    note: str = ""
    customer_visible: bool = True
    assign_to: Optional[str] = None


# ------------------------------------------------------------------ helpers
def _lang(v: str | None) -> str:
    return "hi" if v == "hi" else "en"


def _now():
    return utcnow()


def _next_level(level: str) -> str | None:
    i = LEVELS.index(level)
    return LEVELS[i + 1] if i + 1 < len(LEVELS) else None


def _footer_levels(conn: sqlite3.Connection, locale: str) -> list[dict]:
    """Escalation contacts from the published footer (CMS), if that content exists."""
    for loc in (locale, "en"):
        row = one(
            conn,
            """SELECT v.body FROM content_items i JOIN content_versions v ON v.id = i.published_version_id
               WHERE i.content_type = 'footer_disclosure' AND i.locale = ? ORDER BY i.created_at LIMIT 1""",
            (loc,),
        )
        if not row:
            continue
        body = jload(row["body"], {}) or {}
        sections = body.get("sections", body) if isinstance(body, dict) else {}
        matrix = (sections or {}).get("escalation_matrix") or {}
        levels = matrix.get("levels") if isinstance(matrix, dict) else None
        if isinstance(levels, list) and levels:
            return [lv for lv in levels if isinstance(lv, dict)]
    return []


def escalation_path(conn: sqlite3.Connection, locale: str, current_level: str | None = None) -> list[dict]:
    lang = _lang(locale)
    footer = _footer_levels(conn, lang)
    level_days = get_reg_value(conn, "complaint_level_days", {}) or {}
    if not isinstance(level_days, dict):
        level_days = {}
    ombudsman_days = _ombudsman_days(conn)
    keys = list(LEVELS) + ["rbi_ombudsman"]
    out = []
    for i, key in enumerate(keys):
        fl = footer[i] if i < len(footer) else {}
        if key == "rbi_ombudsman":
            tat = (f"If not resolved in {ombudsman_days} days, or you are not satisfied" if lang == "en"
                   else f"{ombudsman_days} दिनों में समाधान न होने पर, या असंतुष्ट होने पर")
        else:
            d = level_days.get(key)
            tat = (f"About {d} days at this level" if lang == "en" else f"इस स्तर पर लगभग {d} दिन") if d else None
        out.append({
            "level": key,
            "label": LEVEL_LABELS[lang][key],
            "name": fl.get("name") or LEVEL_LABELS[lang][key],
            "contact": fl.get("contact") or DEFAULT_CONTACTS[key],
            "tat": fl.get("tat") or tat,
            "current": key == current_level,
        })
    return out


def _ombudsman_days(conn: sqlite3.Connection) -> int:
    try:
        return int(get_reg_value(conn, "rbi_ombudsman_days", 30) or 30)
    except (TypeError, ValueError):
        return 30


def _tat_days(conn: sqlite3.Connection, category: str) -> int:
    table = get_reg_value(conn, "complaint_tat_days", {}) or {}
    try:
        return int(table.get(category) or table.get("other") or 21) if isinstance(table, dict) else 21
    except (TypeError, ValueError):
        return 21


def _officers(conn: sqlite3.Connection, role: str | None = None) -> list[dict]:
    rows = many(conn, "SELECT id, display_name, email, staff_roles, status FROM users WHERE user_type = 'staff' AND status = 'active'")
    out = []
    for r in rows:
        roles = jload(r["staff_roles"], []) or []
        if any(x in GRIEVANCE_ROLES for x in roles) and (role is None or role in roles):
            out.append({"id": r["id"], "name": r["display_name"] or r["email"], "email": r["email"],
                        "roles": [x for x in roles if x in GRIEVANCE_ROLES]})
    return out


def _pick_officer(conn: sqlite3.Connection, level: str) -> str | None:
    """Least-loaded active officer whose role covers ``level`` (dedicated officers preferred)."""
    role = LEVEL_ROLE[level]
    cands = _officers(conn, role)
    if not cands:
        return None

    def load(o: dict) -> tuple[int, int, str]:
        n = conn.execute(
            f"SELECT COUNT(*) FROM complaints WHERE assigned_officer_id = ? AND status IN ({','.join('?' * len(OPEN_STATES))})",
            (o["id"], *OPEN_STATES)).fetchone()[0]
        return (len(o["roles"]), n, o["id"])

    return sorted(cands, key=load)[0]["id"]


def _event(conn: sqlite3.Connection, complaint_id: str, *, from_status: str | None, to_status: str, from_level: str | None,
           to_level: str | None, actor_type: str, actor_id: str | None, note: str | None, customer_visible: bool,
           at: str | None = None) -> dict:
    ev = {"id": new_id(), "complaint_id": complaint_id, "from_status": from_status, "to_status": to_status,
          "from_level": from_level, "to_level": to_level, "actor_type": actor_type, "actor_id": actor_id,
          "note": note, "customer_visible": int(bool(customer_visible)), "occurred_at": at or now_iso()}
    conn.execute(
        """INSERT INTO complaint_events (id, complaint_id, from_status, to_status, from_level, to_level, actor_type, actor_id,
           note, customer_visible, occurred_at) VALUES (:id, :complaint_id, :from_status, :to_status, :from_level, :to_level,
           :actor_type, :actor_id, :note, :customer_visible, :occurred_at)""",
        ev,
    )
    return ev


def _customer_users(conn: sqlite3.Connection, c: dict) -> list[str]:
    ids = set()
    if c.get("customer_id"):
        ids |= {r["id"] for r in conn.execute("SELECT id FROM users WHERE customer_id = ? AND user_type = 'retail'", (c["customer_id"],))}
    ids |= {r["id"] for r in conn.execute("SELECT id FROM users WHERE mobile = ? AND user_type = 'retail'", (c["mobile"],))}
    return sorted(ids)


def _notify_customer(conn: sqlite3.Connection, c: dict, title: str, body: str) -> None:
    for uid in _customer_users(conn, c):
        notify(conn, uid, "complaint_update", title, body)


def _tat_breached(c: dict, now=None) -> bool:
    now = now or _now()
    due = parse_iso(c["tat_due_at"])
    if c["status"] in CLOSED_STATES:
        resolved = parse_iso(c.get("resolved_at")) if c.get("resolved_at") else None
        return bool(due and resolved and resolved > due)
    return bool(due and now > due)


def _ombudsman_eligible(c: dict, now=None) -> bool:
    now = now or _now()
    eligible_at = parse_iso(c["rbi_ombudsman_eligible_at"])
    return bool(eligible_at and now >= eligible_at and c["status"] not in CLOSED_STATES)


def _can_reopen(c: dict, now=None) -> bool:
    now = now or _now()
    resolved = parse_iso(c.get("resolved_at")) if c.get("resolved_at") else None
    return c["status"] == "resolved" and bool(resolved) and now - resolved <= timedelta(days=REOPEN_WINDOW_DAYS)  # type: ignore[operator]


def _can_escalate(c: dict) -> bool:
    return c["status"] in OPEN_STATES and _next_level(c["current_level"]) is not None


def _age_days(c: dict, now=None) -> int:
    now = now or _now()
    created = parse_iso(c["created_at"])
    return max(0, (now - created).days) if created else 0


def _status_label(status: str, lang: str) -> str:
    labels = {
        "en": {"open": "Registered", "in_progress": "Being worked on", "escalated": "Escalated", "resolved": "Resolved",
               "closed": "Closed", "reopened": "Reopened", "referred_rbi_ombudsman": "Referred to RBI Ombudsman",
               "reassigned": "Reassigned"},
        "hi": {"open": "दर्ज", "in_progress": "कार्रवाई जारी", "escalated": "आगे बढ़ाई गई", "resolved": "समाधान हो गया",
               "closed": "बंद", "reopened": "फिर से खोली गई", "referred_rbi_ombudsman": "आरबीआई लोकपाल को भेजी गई",
               "reassigned": "पुनः सौंपी गई"},
    }
    return labels[lang].get(status, status.replace("_", " "))


def _timeline(conn: sqlite3.Connection, complaint_id: str, *, internal: bool, lang: str = "en") -> list[dict]:
    rows = many(
        conn,
        f"""SELECT e.*, u.display_name AS actor_name FROM complaint_events e LEFT JOIN users u ON u.id = e.actor_id
            WHERE e.complaint_id = ? {'' if internal else 'AND e.customer_visible = 1'} ORDER BY e.occurred_at, e.rowid""",
        (complaint_id,),
    )
    out = []
    for e in rows:
        item = {
            "id": e["id"],
            "status": e["to_status"],
            "status_label": _status_label(e["to_status"], lang),
            "from_status": e["from_status"],
            "level": e["to_level"] or e["from_level"],
            "level_label": LEVEL_LABELS[lang].get(e["to_level"] or e["from_level"] or "", None),
            "from_level": e["from_level"],
            "note": e["note"],
            "occurred_at": e["occurred_at"],
            "actor_type": e["actor_type"],
        }
        if internal:
            item["customer_visible"] = bool(e["customer_visible"])
            item["actor_id"] = e["actor_id"]
            item["actor_name"] = e["actor_name"] or ("Customer" if e["actor_type"] in ("customer", "visitor") else "System")
        else:
            item["actor"] = ("bank" if e["actor_type"] in ("staff", "system") else "you")
        out.append(item)
    return out


def _public_view(conn: sqlite3.Connection, c: dict, lang: str | None = None) -> dict:
    lang = _lang(lang or c.get("language"))
    now = _now()
    nxt = _next_level(c["current_level"])
    path = escalation_path(conn, lang, c["current_level"])
    eligible = _ombudsman_eligible(c, now)
    next_option = None
    if _can_escalate(c) and nxt:
        next_option = next((p for p in path if p["level"] == nxt), None)
    elif eligible or (c["status"] in OPEN_STATES and nxt is None):
        next_option = next((p for p in path if p["level"] == "rbi_ombudsman"), None)
    return {
        "ticket_id": c["ticket_id"],
        "category": c["category"],
        "status": c["status"],
        "status_label": _status_label(c["status"], lang),
        "current_level": c["current_level"],
        "current_level_label": LEVEL_LABELS[lang][c["current_level"]],
        "created_at": c["created_at"],
        "updated_at": c["updated_at"],
        "tat_due_at": c["tat_due_at"],
        "tat_breached": _tat_breached(c, now),
        "resolved_at": c.get("resolved_at"),
        "resolution_note": c.get("resolution_note") if c["status"] in ("resolved", "closed") else None,
        "rbi_ombudsman_eligible_at": c["rbi_ombudsman_eligible_at"],
        "rbi_ombudsman_eligible": eligible,
        "rbi_cms_url": RBI_CMS_URL,
        "can_escalate": _can_escalate(c),
        "can_reopen": _can_reopen(c, now),
        "next_escalation": next_option,
        "escalation_path": path,
        "timeline": _timeline(conn, c["id"], internal=False, lang=lang),
        "mobile_masked": mask_mobile(c["mobile"]),
    }


def _track_token(ticket_id: str, mobile: str, expires: str) -> str:
    msg = f"{ticket_id}|{mobile}|{expires}".encode("utf-8")
    sig = hmac.new(_TRACK_SECRET, msg, hashlib.sha256).hexdigest()[:40]
    return f"{expires}.{sig}"


def _track_token_ok(token: str | None, ticket_id: str, mobile: str) -> bool:
    if not token or "." not in token:
        return False
    expires, _, _sig = token.rpartition(".")
    exp = parse_iso(expires)
    if not exp or exp <= _now():
        return False
    return hmac.compare_digest(_track_token(ticket_id, mobile, expires), token)


def _complaint_for_customer(conn: sqlite3.Connection, ticket_id: str, mobile: str) -> dict:
    """404 for unknown tickets AND for a mobile that does not match (never reveal existence)."""
    tid = (ticket_id or "").strip().upper()
    c = one(conn, "SELECT * FROM complaints WHERE ticket_id = ?", (tid,)) if TICKET_RE.match(tid) else None
    if not c or c["mobile"] != mobile:
        raise api_error(404, "not_found", "We could not find a complaint with this ticket ID and mobile number")
    return c


# ------------------------------------------------------------------ customer (public) endpoints
@router.post("/complaints", status_code=201)
def file_complaint(body: ComplaintIn, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    name = re.sub(r"\s+", " ", body.name or "").strip()
    if not NAME_RE.match(name):
        raise invalid("name", "Enter your full name (2 to 100 letters)")
    mobile = require_mobile(body.mobile)
    email = (body.email or "").strip().lower() or None
    if email and not valid_email(email):
        raise invalid("email", "Enter a valid email address or leave it blank")
    if body.category not in CATEGORIES:
        raise invalid("category", "Choose a complaint category")
    last4 = (body.account_last4 or "").strip() or None
    if last4 and not re.fullmatch(r"\d{4}", last4):
        raise invalid("account_last4", "Enter only the last 4 digits of your account or card")
    description = (body.description or "").strip()
    if len(description) < 20:
        raise invalid("description", "Describe the issue in at least 20 characters")
    if len(description) > 2000:
        raise invalid("description", "Keep the description within 2000 characters")
    lang = _lang(body.language)
    require_otp(conn, mobile, "complaint", body.otp)

    now = _now()
    tat_days = _tat_days(conn, body.category)
    ombudsman_days = _ombudsman_days(conn)
    customer = one(conn, "SELECT id FROM customers WHERE mobile = ? ORDER BY created_at LIMIT 1", (mobile,))
    officer = _pick_officer(conn, "branch")
    cid = new_id()
    ticket = reference_no("CMP")
    c = {
        "id": cid, "ticket_id": ticket, "customer_id": customer["id"] if customer else None, "name": name, "mobile": mobile,
        "email": email, "category": body.category, "description": redact_pii(description), "account_last4": last4,
        "current_level": "branch", "status": "open", "tat_due_at": iso(now + timedelta(days=tat_days)),
        "rbi_ombudsman_eligible_at": iso(now + timedelta(days=ombudsman_days)), "assigned_officer_id": officer,
        "resolution_note": None, "language": lang, "created_at": iso(now), "updated_at": iso(now), "resolved_at": None,
    }
    conn.execute(
        """INSERT INTO complaints (id, ticket_id, customer_id, name, mobile, email, category, description, account_last4,
           current_level, status, tat_due_at, rbi_ombudsman_eligible_at, assigned_officer_id, resolution_note, language,
           created_at, updated_at, resolved_at) VALUES (:id, :ticket_id, :customer_id, :name, :mobile, :email, :category,
           :description, :account_last4, :current_level, :status, :tat_due_at, :rbi_ombudsman_eligible_at,
           :assigned_officer_id, :resolution_note, :language, :created_at, :updated_at, :resolved_at)""",
        c,
    )
    _event(conn, cid, from_status=None, to_status="open", from_level=None, to_level="branch",
           actor_type="customer" if customer else "visitor", actor_id=None,
           note="Complaint registered" if lang == "en" else "शिकायत दर्ज की गई", customer_visible=True, at=c["created_at"])
    if officer:
        _event(conn, cid, from_status="open", to_status="open", from_level="branch", to_level="branch", actor_type="system",
               actor_id=None, note="Assigned to a grievance officer", customer_visible=False)
    audit(conn, "admin", "complaint.filed", actor_type="customer" if customer else "visitor", entity_type="complaint",
          entity_id=cid, details={"ticket_id": ticket, "category": body.category, "mobile": mask_mobile(mobile),
                                  "tat_days": tat_days}, ip=client_ip(request))
    due_text = (now + timedelta(days=tat_days)).astimezone(IST).strftime("%d/%m/%Y")
    _notify_customer(conn, c, "Complaint registered", f"Your complaint {ticket} is registered. We aim to resolve it by {due_text}.")
    view = _public_view(conn, c, lang)
    view.update({"tat_days": tat_days, "rbi_ombudsman_days": ombudsman_days})
    return view


@router.post("/complaints/track")
def track_complaint(body: TrackIn, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    mobile = require_mobile(body.mobile)
    if not (body.ticket_id or "").strip():
        raise invalid("ticket_id", "Enter your complaint ticket ID")
    require_otp(conn, mobile, "complaint_track", body.otp)
    c = _complaint_for_customer(conn, body.ticket_id, mobile)
    view = _public_view(conn, c)
    expires = iso(_now() + timedelta(minutes=TRACK_TOKEN_MINUTES))
    view["track_token"] = _track_token(c["ticket_id"], mobile, expires)  # lets the customer act without a second OTP
    view["track_token_expires_at"] = expires
    audit(conn, "admin", "complaint.tracked", actor_type="visitor", entity_type="complaint", entity_id=c["id"],
          details={"ticket_id": c["ticket_id"]}, ip=client_ip(request))
    return view


@router.post("/complaints/escalate")
def escalate_complaint(body: EscalateIn, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    mobile = require_mobile(body.mobile)
    if not (body.ticket_id or "").strip():
        raise invalid("ticket_id", "Enter your complaint ticket ID")
    if body.action not in ("escalate", "reopen", "refer_rbi_ombudsman"):
        raise invalid("action", "Choose escalate, reopen or refer_rbi_ombudsman")
    reason = redact_pii((body.reason or "").strip())[:1000] or None
    tid = (body.ticket_id or "").strip().upper()
    if not _track_token_ok(body.track_token, tid, mobile):
        require_otp(conn, mobile, "complaint_track", body.otp)
    c = _complaint_for_customer(conn, tid, mobile)
    lang = _lang(c.get("language"))
    now = _now()
    prev_status, prev_level = c["status"], c["current_level"]

    if body.action == "escalate":
        nxt = _next_level(c["current_level"])
        if c["status"] not in OPEN_STATES:
            raise api_error(422, "cannot_escalate", "This complaint is already resolved or closed. Reopen it if you are not satisfied.",
                            field="action")
        if nxt is None:
            raise api_error(422, "cannot_escalate",
                            "Your complaint is already with the Internal Ombudsman, the highest level at the bank. "
                            "You can approach the RBI Ombudsman once it is eligible.", field="action")
        if not reason or len(reason) < 10:
            raise invalid("reason", "Tell us briefly why you want to escalate (at least 10 characters)")
        officer = _pick_officer(conn, nxt)
        conn.execute("UPDATE complaints SET current_level = ?, status = 'escalated', assigned_officer_id = ?, updated_at = ? WHERE id = ?",
                     (nxt, officer, iso(now), c["id"]))
        _event(conn, c["id"], from_status=prev_status, to_status="escalated", from_level=prev_level, to_level=nxt,
               actor_type="customer", actor_id=None, note=f"Escalated by customer: {reason}", customer_visible=True)
        message = f"Escalated to the {LEVEL_LABELS['en'][nxt]}."
    elif body.action == "reopen":
        if not _can_reopen(c, now):
            raise api_error(422, "cannot_reopen",
                            f"Only a resolved complaint can be reopened, within {REOPEN_WINDOW_DAYS} days of resolution. "
                            "File a new complaint or approach the RBI Ombudsman.", field="action")
        if not reason or len(reason) < 10:
            raise invalid("reason", "Tell us briefly why the resolution did not work for you (at least 10 characters)")
        officer = _pick_officer(conn, c["current_level"]) or c["assigned_officer_id"]
        conn.execute("UPDATE complaints SET status = 'reopened', resolved_at = NULL, assigned_officer_id = ?, updated_at = ? WHERE id = ?",
                     (officer, iso(now), c["id"]))
        _event(conn, c["id"], from_status=prev_status, to_status="reopened", from_level=prev_level, to_level=prev_level,
               actor_type="customer", actor_id=None, note=f"Reopened by customer: {reason}", customer_visible=True)
        message = "Your complaint is reopened."
    else:
        if c["status"] == "referred_rbi_ombudsman":
            raise api_error(422, "already_referred", "This complaint is already recorded as referred to the RBI Ombudsman.", field="action")
        if not _ombudsman_eligible(c, now):
            eligible_at = parse_iso(c["rbi_ombudsman_eligible_at"])
            when = eligible_at.astimezone(IST).strftime("%d/%m/%Y") if eligible_at else "-"
            raise api_error(422, "not_yet_eligible",
                            f"You can approach the RBI Ombudsman from {when} if the complaint is still unresolved, "
                            "or earlier if the bank fully or partly rejects it.", field="action")
        conn.execute("UPDATE complaints SET status = 'referred_rbi_ombudsman', updated_at = ? WHERE id = ?", (iso(now), c["id"]))
        _event(conn, c["id"], from_status=prev_status, to_status="referred_rbi_ombudsman", from_level=prev_level,
               to_level=prev_level, actor_type="customer", actor_id=None,
               note="Customer chose to approach the RBI Ombudsman (cms.rbi.org.in)" + (f": {reason}" if reason else ""),
               customer_visible=True)
        message = "Recorded. File your complaint on the RBI Complaint Management System and quote this ticket ID."

    audit(conn, "admin", f"complaint.customer_{body.action}", actor_type="customer", entity_type="complaint", entity_id=c["id"],
          details={"ticket_id": c["ticket_id"], "from_level": prev_level, "from_status": prev_status}, ip=client_ip(request))
    updated = one(conn, "SELECT * FROM complaints WHERE id = ?", (c["id"],))
    _notify_customer(conn, updated, "Complaint update", f"{updated['ticket_id']}: {message}")  # type: ignore[index]
    view = _public_view(conn, updated, lang)  # type: ignore[arg-type]
    view["message"] = message
    return view


@router.get("/me/complaints")
def my_complaints(ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    mobile = normalize_mobile(ctx.user.get("mobile")) or ctx.user.get("mobile")
    rows = many(conn, "SELECT * FROM complaints WHERE customer_id = ? OR mobile = ? ORDER BY created_at DESC LIMIT 100",
                (ctx.customer_id, mobile))
    lang = _lang(ctx.user.get("preferred_language"))
    return {"items": [_public_view(conn, c, lang) for c in rows]}


# ------------------------------------------------------------------ staff
staff_dep = require_staff(*GRIEVANCE_ROLES)


def _levels_for(ctx: AuthContext) -> list[str]:
    return [lvl for lvl, role in LEVEL_ROLE.items() if ctx.has_role(role)]


def _truthy(v: str | None) -> bool | None:
    if v is None or v == "":
        return None
    return v.lower() in ("1", "true", "yes", "on")


def _staff_row(c: dict, ctx: AuthContext, officers: dict[str, str], now) -> dict:
    return {
        "ticket_id": c["ticket_id"],
        "name": c["name"],
        "mobile_masked": mask_mobile(c["mobile"]),
        "category": c["category"],
        "current_level": c["current_level"],
        "current_level_label": LEVEL_LABELS["en"][c["current_level"]],
        "status": c["status"],
        "created_at": c["created_at"],
        "updated_at": c["updated_at"],
        "tat_due_at": c["tat_due_at"],
        "breached": _tat_breached(c, now),
        "tat_breached": _tat_breached(c, now),
        "age_days": _age_days(c, now),
        "rbi_ombudsman_eligible_at": c["rbi_ombudsman_eligible_at"],
        "rbi_ombudsman_eligible": _ombudsman_eligible(c, now),
        "assigned_officer_id": c["assigned_officer_id"],
        "assigned_officer_name": officers.get(c["assigned_officer_id"] or ""),
        "assigned_to_me": c["assigned_officer_id"] == ctx.user_id,
        "can_act": c["current_level"] in _levels_for(ctx),
        "linked_customer": bool(c["customer_id"]),
    }


@router.get("/staff/complaints")
def staff_list(
    level: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    tat_breached: Optional[str] = Query(None),
    ombudsman_eligible: Optional[str] = Query(None),
    assigned_to_me: Optional[str] = Query(None),
    ctx: AuthContext = Depends(staff_dep),
    conn: sqlite3.Connection = Depends(get_db),
):
    if level and level not in LEVELS:
        raise invalid("level", f"Level must be one of: {', '.join(LEVELS)}")
    if category and category not in CATEGORIES:
        raise invalid("category", "Unknown category")
    where, params = ["1=1"], []
    if level:
        where.append("current_level = ?")
        params.append(level)
    if category:
        where.append("category = ?")
        params.append(category)
    if status == "open_all":
        where.append(f"status IN ({','.join('?' * len(OPEN_STATES))})")
        params.extend(OPEN_STATES)
    elif status:
        where.append("status = ?")
        params.append(status[:40])
    if _truthy(assigned_to_me):
        where.append("assigned_officer_id = ?")
        params.append(ctx.user_id)
    rows = many(conn, f"SELECT * FROM complaints WHERE {' AND '.join(where)} ORDER BY tat_due_at ASC LIMIT 500", params)
    now = _now()
    officers = {o["id"]: o["name"] for o in _officers(conn)}
    items = [_staff_row(c, ctx, officers, now) for c in rows]
    tb, oe = _truthy(tat_breached), _truthy(ombudsman_eligible)
    if tb is not None:
        items = [i for i in items if i["breached"] == tb]
    if oe is not None:
        items = [i for i in items if i["rbi_ombudsman_eligible"] == oe]
    open_items = [i for i in items if i["status"] in OPEN_STATES]
    return {
        "items": items,
        "total": len(items),
        "summary": {
            "open": len(open_items),
            "breached": sum(1 for i in open_items if i["breached"]),
            "ombudsman_eligible": sum(1 for i in items if i["rbi_ombudsman_eligible"]),
            "assigned_to_me": sum(1 for i in open_items if i["assigned_to_me"]),
        },
        "my_levels": _levels_for(ctx),
        "officers": _officers(conn),
        "levels": [{"value": lvl, "label": LEVEL_LABELS["en"][lvl]} for lvl in LEVELS],
        "categories": list(CATEGORIES),
    }


def _staff_complaint(conn: sqlite3.Connection, ticket_id: str) -> dict:
    tid = (ticket_id or "").strip().upper()
    c = one(conn, "SELECT * FROM complaints WHERE ticket_id = ?", (tid,)) if TICKET_RE.match(tid) else None
    if not c:
        raise not_found("Complaint")
    return c


def _staff_detail(conn: sqlite3.Connection, c: dict, ctx: AuthContext) -> dict:
    now = _now()
    officers = {o["id"]: o["name"] for o in _officers(conn)}
    row = _staff_row(c, ctx, officers, now)
    allowed: list[str] = []
    if row["can_act"]:
        if c["status"] in OPEN_STATES:
            allowed = ["in_progress", "resolved", "reassigned"] + (["escalated"] if _next_level(c["current_level"]) else [])
        elif c["status"] == "resolved":
            allowed = ["closed"]
    row.update({
        "id": c["id"],
        "email": c["email"],
        "account_last4": c["account_last4"],
        "description": c["description"],
        "resolution_note": c["resolution_note"],
        "resolved_at": c["resolved_at"],
        "language": c["language"],
        "timeline": _timeline(conn, c["id"], internal=True),
        "escalation_path": escalation_path(conn, "en", c["current_level"]),
        "allowed_actions": allowed,
        "next_level": _next_level(c["current_level"]),
    })
    return row


@router.get("/staff/complaints/{ticket_id}")
def staff_get(ticket_id: str, ctx: AuthContext = Depends(staff_dep), conn: sqlite3.Connection = Depends(get_db)):
    return _staff_detail(conn, _staff_complaint(conn, ticket_id), ctx)


@router.post("/staff/complaints/{ticket_id}/transition")
def staff_transition(ticket_id: str, body: TransitionIn, request: Request, ctx: AuthContext = Depends(staff_dep),
                     conn: sqlite3.Connection = Depends(get_db)):
    c = _staff_complaint(conn, ticket_id)
    if body.to_status not in STAFF_TRANSITIONS:
        raise invalid("to_status", f"Choose one of: {', '.join(STAFF_TRANSITIONS)}")
    if c["current_level"] not in _levels_for(ctx):
        raise forbidden(f"This complaint is at the {LEVEL_LABELS['en'][c['current_level']]} level, which your role does not cover",
                        "level_not_covered")
    note = redact_pii((body.note or "").strip())
    if len(note) < 5:
        raise invalid("note", "Add a note of at least 5 characters")
    if len(note) > 2000:
        raise invalid("note", "Keep the note within 2000 characters")
    status, level = c["status"], c["current_level"]
    if status in ("closed", "referred_rbi_ombudsman"):
        raise api_error(422, "invalid_transition", "This complaint is closed; no further action is possible.", field="to_status")
    if status == "resolved" and body.to_status != "closed":
        raise api_error(422, "invalid_transition", "A resolved complaint can only be closed (the customer may reopen it).",
                        field="to_status")
    if body.to_status == "closed" and status != "resolved":
        raise api_error(422, "invalid_transition", "Resolve the complaint before closing it.", field="to_status")

    now = iso(_now())
    visible = bool(body.customer_visible)
    new_status, new_level, assigned = status, level, c["assigned_officer_id"]
    if body.to_status == "in_progress":
        new_status = "in_progress"
    elif body.to_status == "resolved":
        new_status = "resolved"
        visible = True  # the customer must always see the resolution
    elif body.to_status == "closed":
        new_status = "closed"
        visible = True
    elif body.to_status == "escalated":
        nxt = _next_level(level)
        if not nxt:
            raise api_error(422, "invalid_transition", "The Internal Ombudsman is the highest internal level.", field="to_status")
        new_status, new_level = "escalated", nxt
        assigned = _pick_officer(conn, nxt)
        visible = True
    else:  # reassigned
        target = (body.assign_to or "").strip()
        officer = next((o for o in _officers(conn) if o["id"] == target), None)
        if not officer:
            raise invalid("assign_to", "Choose a staff member who holds a grievance role")
        if target == c["assigned_officer_id"]:
            raise invalid("assign_to", "The complaint is already assigned to this officer")
        assigned = target
        note = f"Reassigned to {officer['name']}: {note}"

    conn.execute(
        """UPDATE complaints SET status = ?, current_level = ?, assigned_officer_id = ?, updated_at = ?,
           resolution_note = CASE WHEN ? = 'resolved' THEN ? ELSE resolution_note END,
           resolved_at = CASE WHEN ? = 'resolved' THEN ? ELSE resolved_at END WHERE id = ?""",
        (new_status, new_level, assigned, now, body.to_status, note, body.to_status, now, c["id"]),
    )
    _event(conn, c["id"], from_status=status, to_status=new_status if body.to_status != "reassigned" else "reassigned",
           from_level=level, to_level=new_level, actor_type="staff", actor_id=ctx.user_id, note=note, customer_visible=visible)
    audit_ctx(conn, ctx, "admin", f"complaint.{body.to_status}", entity_type="complaint", entity_id=c["id"],
              details={"ticket_id": c["ticket_id"], "from_status": status, "to_status": new_status, "from_level": level,
                       "to_level": new_level, "assigned_officer_id": assigned, "customer_visible": visible}, request=request)
    updated = one(conn, "SELECT * FROM complaints WHERE id = ?", (c["id"],))
    if visible:
        lang = _lang(c["language"])
        _notify_customer(conn, updated, "Complaint update" if lang == "en" else "शिकायत अपडेट",  # type: ignore[arg-type]
                         f"{c['ticket_id']}: {_status_label(new_status, lang)}. {note[:200]}")
    return _staff_detail(conn, updated, ctx)  # type: ignore[arg-type]


# ------------------------------------------------------------------ seed
def seed_complaints(conn: sqlite3.Connection) -> None:
    """Synthetic complaints for the demo customer: one open > 30 days (RBI Ombudsman eligible), one resolved, one new."""
    officers = {r["email"]: r["id"] for r in conn.execute("SELECT id, email FROM users WHERE user_type = 'staff'")}
    go, nodal = officers.get("grievance@example.bank.in"), officers.get("nodal@example.bank.in")
    cust = one(conn, "SELECT id, mobile, full_name FROM customers WHERE mobile = '+919876543210'")
    if not cust:
        return
    tat =get_reg_value(conn, "complaint_tat_days", {}) or {}
    ombudsman_days = int(get_reg_value(conn, "rbi_ombudsman_days", 30) or 30)
    now = _now()

    def make(category: str, desc: str, created_days_ago: int, *, level: str, status: str, officer: str | None,
             events: list[tuple], resolved_days_ago: int | None = None, resolution: str | None = None) -> None:
        created = now - timedelta(days=created_days_ago)
        cid = new_id()
        ticket = f"CMP-{created.astimezone(IST).strftime('%Y%m%d')}-" + new_id().replace("-", "")[:6].upper()
        tat_days = int(tat.get(category, 21)) if isinstance(tat, dict) else 21
        updated = now - timedelta(days=resolved_days_ago) if resolved_days_ago is not None else created + timedelta(days=events[-1][0])
        conn.execute(
            """INSERT INTO complaints (id, ticket_id, customer_id, name, mobile, email, category, description, account_last4,
               current_level, status, tat_due_at, rbi_ombudsman_eligible_at, assigned_officer_id, resolution_note, language,
               created_at, updated_at, resolved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'en', ?,?,?)""",
            (cid, ticket, cust["id"], cust["full_name"], cust["mobile"], "customer@example.bank.in", category, desc, "4821",
             level, status, iso(created + timedelta(days=tat_days)), iso(created + timedelta(days=ombudsman_days)), officer,
             resolution, iso(created), iso(updated), iso(updated) if resolved_days_ago is not None else None),
        )
        prev_s, prev_l = None, None
        for day, to_status, to_level, actor, actor_id, note, visible in events:
            _event(conn, cid, from_status=prev_s, to_status=to_status, from_level=prev_l, to_level=to_level, actor_type=actor,
                   actor_id=actor_id, note=note, customer_visible=visible, at=iso(created + timedelta(days=day, minutes=5)))
            prev_s, prev_l = (to_status if to_status != "reassigned" else prev_s), to_level

    make("atm_card", "ATM at Andheri station debited Rs. 5,000 but did not dispense cash. Reversal not received yet.", 38,
         level="nodal", status="escalated", officer=nodal, events=[
             (0, "open", "branch", "customer", None, "Complaint registered", True),
             (1, "in_progress", "branch", "staff", go, "Chargeback raised with the acquiring bank via NPCI DMS", True),
             (2, "in_progress", "branch", "staff", go, "Internal: EJ log requested from ATM vendor", False),
             (9, "escalated", "nodal", "customer", None, "Escalated by customer: no reversal after a week", True),
         ])
    make("charges", "Annual debit card fee charged twice in the same month on my savings account.", 25, level="branch",
         status="resolved", officer=go, resolved_days_ago=20, resolution="Duplicate fee of Rs. 590 reversed on the account.",
         events=[
             (0, "open", "branch", "customer", None, "Complaint registered", True),
             (2, "in_progress", "branch", "staff", go, "Verified duplicate charge in Finacle", False),
             (5, "resolved", "branch", "staff", go, "Duplicate fee of Rs. 590 reversed on the account.", True),
         ])
    make("digital_payments", "UPI payment to a merchant failed but the amount was debited from my account yesterday.", 2,
         level="branch", status="open", officer=go, events=[
             (0, "open", "branch", "customer", None, "Complaint registered", True),
         ])


register_seed(70, "complaints", seed_complaints)

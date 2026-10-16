"""Profile, preferences, contact/address changes with dual OTP, re-KYC and the alerts history.

A mobile, email or address change is a high-risk event: OTPs go to BOTH the old and the new
contact, both are alerted once it is applied, and a 24-hour cooling period then restricts
other high-risk actions (adding beneficiaries, high-value transfers, card limit increases,
LRS remittances). New values are stored encrypted with a masked copy for display.
"""
from __future__ import annotations

import re
import secrets
import sqlite3
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict

from audit import audit_ctx
from banking import customer_for, in_cooling_period
from core import (MOCK_OTP_CODE, api_error, fmt_ist, format_inr, get_reg_value, invalid, iso, jdump, jload, mask_aadhaar_last4,
                  mask_email, mask_mobile, new_id, normalize_mobile, not_found, forbidden, now_iso, one, parse_date, today_ist,
                  utcnow, valid_email)
from crypto import decrypt_str, encrypt_str
from db import get_db
from integrations import sms_gateway
from integrations.base import is_mock
from notify import notify, notify_contact
from objectstore import consume_upload
from security import (OTP_MAX_SENDS, OTP_SEND_WINDOW_MINUTES, OTP_TTL_SECONDS, AuthContext, check_otp, code_hash, require_auth,
                      require_customer, send_otp)

router = APIRouter(tags=["profile"])

LANGUAGES = ("en", "hi", "ta", "te", "kn", "mr", "bn", "gu", "ml")
CHANNELS = ("sms", "email", "push")
CHANGE_TYPES = ("mobile", "email", "address", "rekyc")
OPEN_STATUSES = ("otp_pending", "verified")
OTP_PURPOSE = "profile_change"
COOLING_HOURS = 24
REKYC_WINDOW_DAYS = 60
REKYC_PERIOD_YEARS = 2
PENDING_EXPIRY_MINUTES = 30
DOC_TYPES_ALLOWED = ("application/pdf", "image/png", "image/jpeg")


class Body(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class PrefsIn(Body):
    preferred_language: Optional[str] = None
    senior_mode: Optional[bool] = None
    notification_channels: Optional[list[str]] = None


class ChangeIn(Body):
    change_type: Optional[str] = None
    new_value: Optional[str] = None
    document_upload_ref: Optional[str] = None
    no_change_declaration: Optional[bool] = None


class VerifyIn(Body):
    otp_old: Optional[str] = None
    otp_new: Optional[str] = None


# ------------------------------------------------------------------ helpers
def _prefs(user: dict) -> dict:
    return {
        "preferred_language": user.get("preferred_language") or "en",
        "senior_mode": bool(user.get("senior_mode")),
        "notification_channels": jload(user.get("notification_channels"), ["sms", "email", "push"]) or ["sms"],
    }


def restricted_actions(conn: sqlite3.Connection) -> list[dict]:
    limit = int(get_reg_value(conn, "beneficiary_cooling_limit_paise", 5000000) or 5000000)
    return [
        {"action": "add_beneficiary", "label": "Adding a new beneficiary"},
        {"action": "high_value_transfer", "label": f"Transfers above {format_inr(limit)}", "limit_paise": limit},
        {"action": "card_limit_increase", "label": "Increasing card limits or enabling international use"},
        {"action": "lrs_remittance", "label": "Overseas (LRS) remittances"},
    ]


def _mask_address(a: str | None) -> str:
    if not a:
        return ""
    a = a.strip()
    return a if len(a) <= 14 else f"{a[:8]}…{a[-6:]}"


def _mask_value(change_type: str, value: str | None) -> str:
    if change_type == "mobile":
        return mask_mobile(value)
    if change_type == "email":
        return mask_email(value)
    if change_type == "address":
        return _mask_address(value)
    return "Re-KYC"


def _email_key(email: str) -> str:
    return "email:" + email.strip().lower()


def _send_email_otp(conn: sqlite3.Connection, email: str) -> dict:
    """Email OTP through the gateway; stored as an OTP challenge keyed by ``email:<address>``."""
    key = _email_key(email)
    since = iso(utcnow() - timedelta(minutes=OTP_SEND_WINDOW_MINUTES))
    sent = conn.execute("SELECT COUNT(*) FROM otp_challenges WHERE mobile = ? AND created_at >= ?", (key, since)).fetchone()[0]
    if sent >= OTP_MAX_SENDS:
        raise api_error(429, "otp_rate_limited", f"Too many OTP requests. Try again in {OTP_SEND_WINDOW_MINUTES} minutes.")
    mock = is_mock(conn, "sms_gateway")
    code = MOCK_OTP_CODE if mock else f"{secrets.randbelow(1_000_000):06d}"
    cid = new_id()
    conn.execute(
        "INSERT INTO otp_challenges (id, mobile, purpose, code_hash, created_at, expires_at) VALUES (?,?,?,?,?,?)",
        (cid, key, OTP_PURPOSE, code_hash(code, cid), now_iso(), iso(utcnow() + timedelta(seconds=OTP_TTL_SECONDS))),
    )
    sms_gateway.get(conn).send_email(email, "Your verification code",
                                     f"{code} is your verification code for a profile change. Valid for 5 minutes. Never share it.")
    return {"channel": "email", "destination": mask_email(email)}


def _contacts(conn: sqlite3.Connection, ctx: AuthContext, cust: dict) -> tuple[str, str]:
    return (cust.get("mobile") or ctx.user["mobile"]), (cust.get("email") or ctx.user["email"])


def _send_otps(conn: sqlite3.Connection, ctx: AuthContext, cust: dict, change_type: str, new_value: str | None) -> list[dict]:
    old_mobile, old_email = _contacts(conn, ctx, cust)
    out = []
    if change_type == "email":
        out.append({"role": "old", **_send_email_otp(conn, old_email)})
        out.append({"role": "new", **_send_email_otp(conn, new_value)})  # type: ignore[arg-type]
        return out
    send_otp(conn, old_mobile, OTP_PURPOSE)
    out.append({"role": "old", "channel": "sms", "destination": mask_mobile(old_mobile)})
    if change_type == "mobile":
        send_otp(conn, new_value, OTP_PURPOSE)  # type: ignore[arg-type]
        out.append({"role": "new", "channel": "sms", "destination": mask_mobile(new_value)})
    return out


def _public_change(row: dict) -> dict:
    return {
        "id": row["id"],
        "change_type": row["change_type"],
        "old_value_masked": row["old_value_masked"],
        "new_value_masked": row["new_value_masked"],
        "has_document": bool(row["supporting_document_id"]),
        "no_change_declaration": bool(row["no_change_declaration"]),
        "status": row["status"],
        "otp_required": ["otp_old", "otp_new"] if row["change_type"] in ("mobile", "email") else ["otp_old"],
        "cooling_until": row["cooling_until"],
        "created_at": row["created_at"],
        "verified_at": row["verified_at"],
        "applied_at": row["applied_at"],
    }


def _otp_hint(conn: sqlite3.Connection) -> dict:
    if is_mock(conn, "sms_gateway"):
        return {"mock": True, "mock_otp": MOCK_OTP_CODE, "mock_label": "MOCK SMS/email gateway: no message is sent; use the OTP shown"}
    return {"mock": False}


def _expire_stale(conn: sqlite3.Connection, customer_id: str) -> None:
    cutoff = iso(utcnow() - timedelta(minutes=PENDING_EXPIRY_MINUTES))
    conn.execute("UPDATE profile_change_requests SET status = 'expired' WHERE customer_id = ? AND status = 'otp_pending' "
                 "AND created_at < ?", (customer_id, cutoff))


def _attach_upload(conn: sqlite3.Connection, ctx: AuthContext, upload_ref: str, request_id: str, doc_type: str) -> str:
    up = consume_upload(conn, upload_ref, ctx.user_id, field="document_upload_ref")
    if up["content_type"] not in DOC_TYPES_ALLOWED:
        raise invalid("document_upload_ref", "Upload the document as a PDF, PNG or JPEG file")
    doc_id = new_id()
    conn.execute(
        """INSERT INTO documents (id, owner_type, owner_id, doc_type, locale, storage_key, sha256, file_name, content_type,
           size_bytes, created_at) VALUES (?, 'profile_change', ?, ?, 'en', ?, ?, ?, ?, ?, ?)""",
        (doc_id, request_id, doc_type, up["storage_key"], up["sha256"], up["file_name"], up["content_type"], up["size_bytes"],
         now_iso()),
    )
    return doc_id


# ------------------------------------------------------------------ profile
@router.get("/me")
def me(ctx: AuthContext = Depends(require_auth), conn: sqlite3.Connection = Depends(get_db)):
    user = ctx.user
    if ctx.user_type != "retail" or not ctx.customer_id:
        return {
            "user_type": ctx.user_type,
            "full_name": user.get("display_name"),
            "email_masked": mask_email(user["email"]),
            "mobile_masked": mask_mobile(user["mobile"]),
            "staff_roles": jload(user.get("staff_roles"), []),
            "corporate_id": user.get("corporate_id"),
            "preferences": _prefs(user),
        }
    require_customer(ctx)  # same checks as the dependency (MFA-complete session when enforced)
    cust = customer_for(conn, ctx.customer_id)
    today = today_ist()
    due = parse_date(cust.get("kyc_due_date"))
    days_left = (due - today).days if due else None
    rekyc_due = cust["kyc_status"] in ("due", "expired") or (days_left is not None and days_left <= REKYC_WINDOW_DAYS)
    cooling = in_cooling_period(cust)
    rm = None
    if cust.get("rm_staff_id"):
        r = one(conn, "SELECT display_name, email, mobile FROM users WHERE id = ?", (cust["rm_staff_id"],))
        if r:
            rm = {"name": r["display_name"], "email": r["email"], "mobile": r["mobile"], "title": "Relationship manager"}
    open_changes = [dict(r) for r in conn.execute(
        "SELECT * FROM profile_change_requests WHERE customer_id = ? AND status IN ('otp_pending','verified') ORDER BY created_at DESC",
        (cust["id"],))]
    return {
        "user_type": "retail",
        "customer_id": cust["id"],
        "full_name": cust["full_name"],
        "pan_masked": cust.get("pan_masked"),
        "aadhaar_last4": cust.get("aadhaar_last4"),
        "aadhaar_masked": mask_aadhaar_last4(cust.get("aadhaar_last4")),
        "mobile_masked": mask_mobile(cust.get("mobile") or user["mobile"]),
        "email_masked": mask_email(cust.get("email") or user["email"]),
        "login_email_masked": mask_email(user["email"]),
        "address": cust.get("address"),
        "date_of_birth": cust.get("date_of_birth"),
        "segment": cust["segment"],
        "residency_status": cust["residency_status"],
        "kyc_status": cust["kyc_status"],
        "kyc_due_date": cust.get("kyc_due_date"),
        "rekyc_due": rekyc_due,
        "rekyc_days_left": days_left,
        "ckyc_number_masked": ("XXXXXXXXXX" + cust["ckyc_number"][-4:]) if cust.get("ckyc_number") else None,
        "cooling_active": cooling,
        "cooling_until": cust.get("high_risk_cooling_until") if cooling else None,
        "restricted_actions": restricted_actions(conn) if cooling else [],
        "relationship_manager": rm,
        "open_change_requests": [_public_change(r) for r in open_changes],
        "preferences": _prefs(user),
    }


@router.put("/me/preferences")
def update_preferences(body: PrefsIn, request: Request, ctx: AuthContext = Depends(require_auth),
                       conn: sqlite3.Connection = Depends(get_db)):
    updates: dict = {}
    if body.preferred_language is not None:
        if body.preferred_language not in LANGUAGES:
            raise invalid("preferred_language", f"Choose one of: {', '.join(LANGUAGES)}")
        updates["preferred_language"] = body.preferred_language
    if body.senior_mode is not None:
        updates["senior_mode"] = int(body.senior_mode)
    if body.notification_channels is not None:
        chans = list(dict.fromkeys(c.strip().lower() for c in body.notification_channels))
        if any(c not in CHANNELS for c in chans):
            raise invalid("notification_channels", "Channels can be sms, email and push")
        if "sms" not in chans:
            raise invalid("notification_channels", "SMS alerts are mandatory for transactions and security events")
        updates["notification_channels"] = jdump([c for c in CHANNELS if c in chans])
    if updates:
        sets = ", ".join(f"{k} = ?" for k in updates)  # keys are fixed literals above
        conn.execute(f"UPDATE users SET {sets} WHERE id = ?", (*updates.values(), ctx.user_id))
        audit_ctx(conn, ctx, "admin", "preferences.updated", entity_type="user", entity_id=ctx.user_id, request=request,
                  details={k: (jload(v) if k == "notification_channels" else v) for k, v in updates.items()})
    user = one(conn, "SELECT * FROM users WHERE id = ?", (ctx.user_id,)) or ctx.user
    return _prefs(user)


# ------------------------------------------------------------------ profile changes
@router.post("/me/profile-changes", status_code=201)
def create_change(body: ChangeIn, request: Request, ctx: AuthContext = Depends(require_customer),
                  conn: sqlite3.Connection = Depends(get_db)):
    ctype = body.change_type
    if ctype not in CHANGE_TYPES:
        raise invalid("change_type", "Choose mobile, email, address or re-KYC")
    cust = customer_for(conn, ctx.customer_id)  # type: ignore[arg-type]
    old_mobile, old_email = _contacts(conn, ctx, cust)
    new_value: str | None = None
    if ctype == "mobile":
        new_value = normalize_mobile(body.new_value)
        if not new_value:
            raise invalid("new_value", "Enter a valid mobile number (10 digits, or +country code)")
        if new_value == old_mobile:
            raise invalid("new_value", "This is already your registered mobile number")
        if one(conn, "SELECT id FROM users WHERE mobile = ? AND id != ?", (new_value, ctx.user_id)):
            raise api_error(409, "contact_in_use", "This mobile number is registered to another internet banking user",
                            field="new_value")
    elif ctype == "email":
        new_value = (body.new_value or "").strip().lower()
        if not valid_email(new_value):
            raise invalid("new_value", "Enter a valid email address")
        if new_value == (old_email or "").lower():
            raise invalid("new_value", "This is already your registered email address")
        if one(conn, "SELECT id FROM users WHERE email = ? AND id != ?", (new_value, ctx.user_id)):
            raise api_error(409, "contact_in_use", "This email is registered to another internet banking user", field="new_value")
    elif ctype == "address":
        new_value = re.sub(r"\s+", " ", body.new_value or "").strip()
        if not (10 <= len(new_value) <= 300):
            raise invalid("new_value", "Enter the full new address (10 to 300 characters)")
        if not body.document_upload_ref:
            raise invalid("document_upload_ref", "Upload a proof of the new address (Aadhaar, passport, utility bill...)")
    else:
        if body.no_change_declaration is not True and not body.document_upload_ref:
            raise invalid("no_change_declaration", "Confirm that your KYC details are unchanged, or upload an updated KYC document")

    _expire_stale(conn, cust["id"])
    if one(conn, "SELECT id FROM profile_change_requests WHERE customer_id = ? AND change_type = ? AND status IN ('otp_pending','verified')",
           (cust["id"], ctype)):
        raise api_error(409, "request_open", "You already have an open request of this type. Verify it or wait for it to expire.")

    rid = new_id()
    doc_id = None
    if body.document_upload_ref and ctype in ("address", "rekyc"):
        doc_id = _attach_upload(conn, ctx, body.document_upload_ref, rid, "address_proof" if ctype == "address" else "kyc_doc")
    old_masked = {"mobile": mask_mobile(old_mobile), "email": mask_email(old_email), "address": _mask_address(cust.get("address")),
                  "rekyc": f"KYC due {cust.get('kyc_due_date') or '-'}"}[ctype]
    row = {
        "id": rid, "customer_id": cust["id"], "change_type": ctype, "old_value_masked": old_masked,
        "new_value_enc": encrypt_str(new_value) if new_value else None, "new_value_masked": _mask_value(ctype, new_value),
        "supporting_document_id": doc_id,
        "no_change_declaration": int(ctype == "rekyc" and body.no_change_declaration is True),
        "status": "otp_pending", "cooling_until": None, "created_at": now_iso(), "verified_at": None, "applied_at": None,
    }
    conn.execute(
        """INSERT INTO profile_change_requests (id, customer_id, change_type, old_value_masked, new_value_enc, new_value_masked,
           supporting_document_id, no_change_declaration, status, cooling_until, created_at) VALUES (:id, :customer_id,
           :change_type, :old_value_masked, :new_value_enc, :new_value_masked, :supporting_document_id, :no_change_declaration,
           :status, :cooling_until, :created_at)""",
        row,
    )
    sent = _send_otps(conn, ctx, cust, ctype, new_value)
    audit_ctx(conn, ctx, "admin", "profile_change.requested", entity_type="profile_change_request", entity_id=rid, request=request,
              details={"change_type": ctype, "old": old_masked, "new": row["new_value_masked"], "document": bool(doc_id)})
    return {**_public_change(row), "otp_sent_to": sent, **_otp_hint(conn)}


@router.get("/me/profile-changes")
def list_changes(ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    _expire_stale(conn, ctx.customer_id)  # type: ignore[arg-type]
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM profile_change_requests WHERE customer_id = ? ORDER BY created_at DESC, rowid DESC", (ctx.customer_id,))]
    return {"items": [_public_change(r) for r in rows]}


def _own_change(conn: sqlite3.Connection, ctx: AuthContext, change_id: str) -> dict:
    row = one(conn, "SELECT * FROM profile_change_requests WHERE id = ?", (change_id[:64],))
    if not row:
        raise not_found("Profile change request")
    if row["customer_id"] != ctx.customer_id:
        raise forbidden("This request does not belong to you")
    return row


@router.post("/me/profile-changes/{change_id}/resend-otp")
def resend_otp(change_id: str, ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    row = _own_change(conn, ctx, change_id)
    if row["status"] != "otp_pending":
        raise api_error(409, "not_pending", "This request is no longer waiting for OTP verification")
    cust = customer_for(conn, ctx.customer_id)  # type: ignore[arg-type]
    sent = _send_otps(conn, ctx, cust, row["change_type"], decrypt_str(row["new_value_enc"]))
    return {**_public_change(row), "otp_sent_to": sent, **_otp_hint(conn)}


@router.post("/me/profile-changes/{change_id}/verify")
def verify_change(change_id: str, body: VerifyIn, request: Request, ctx: AuthContext = Depends(require_customer),
                  conn: sqlite3.Connection = Depends(get_db)):
    from db import commit_now

    row = _own_change(conn, ctx, change_id)
    _expire_stale(conn, row["customer_id"])
    row = _own_change(conn, ctx, change_id)
    if row["status"] != "otp_pending":
        raise api_error(409, "not_pending", "This request is not waiting for verification (it may have expired or been applied)")
    cust = customer_for(conn, ctx.customer_id)  # type: ignore[arg-type]
    ctype = row["change_type"]
    old_mobile, old_email = _contacts(conn, ctx, cust)
    new_value = decrypt_str(row["new_value_enc"]) if row["new_value_enc"] else None
    if ctype in ("mobile", "email") and not new_value:
        raise api_error(409, "request_unreadable", "This request can no longer be read. Start a new request.")

    old_key = _email_key(old_email) if ctype == "email" else old_mobile
    new_key = (_email_key(new_value) if ctype == "email" else new_value) if ctype in ("mobile", "email") else None  # type: ignore[arg-type]
    ok_old = check_otp(conn, old_key, OTP_PURPOSE, body.otp_old)
    ok_new = True if new_key is None else check_otp(conn, new_key, OTP_PURPOSE, body.otp_new)
    if not ok_old or not ok_new:
        commit_now(conn)  # keep attempt counters
        field = "otp_old" if not ok_old else "otp_new"
        which = "current" if not ok_old else "new"
        raise api_error(422, "otp_invalid", f"The OTP sent to your {which} contact is incorrect or has expired. Request a new OTP.",
                        field=field)

    now = utcnow()
    cooling_until = iso(now + timedelta(hours=COOLING_HOURS)) if ctype in ("mobile", "email", "address") else None
    if ctype == "mobile":
        if one(conn, "SELECT id FROM users WHERE mobile = ? AND id != ?", (new_value, ctx.user_id)):
            raise api_error(409, "contact_in_use", "This mobile number is registered to another internet banking user")
        conn.execute("UPDATE customers SET mobile = ? WHERE id = ?", (new_value, cust["id"]))
        conn.execute("UPDATE users SET mobile = ? WHERE customer_id = ?", (new_value, cust["id"]))
    elif ctype == "email":
        if one(conn, "SELECT id FROM users WHERE email = ? AND id != ?", (new_value, ctx.user_id)):
            raise api_error(409, "contact_in_use", "This email is registered to another internet banking user")
        conn.execute("UPDATE customers SET email = ? WHERE id = ?", (new_value, cust["id"]))
        conn.execute("UPDATE users SET email = ? WHERE id = ?", (new_value, ctx.user_id))
    elif ctype == "address":
        conn.execute("UPDATE customers SET address = ? WHERE id = ?", (decrypt_str(row["new_value_enc"]), cust["id"]))
    else:
        # Re-KYC periodicity is risk based (RBI KYC Master Direction: high risk every 2 years, medium 8, low 10).
        # The platform applies the most conservative 2-year period until the risk category is read from core.
        new_due = today_ist()
        try:
            new_due = new_due.replace(year=new_due.year + REKYC_PERIOD_YEARS)
        except ValueError:  # 29 February
            new_due = new_due.replace(year=new_due.year + REKYC_PERIOD_YEARS, day=28)
        conn.execute("UPDATE customers SET kyc_status = 'full', kyc_due_date = ? WHERE id = ?", (new_due.isoformat(), cust["id"]))
    if cooling_until:
        conn.execute("UPDATE customers SET high_risk_cooling_until = ? WHERE id = ?", (cooling_until, cust["id"]))
    ts = iso(now)
    conn.execute("UPDATE profile_change_requests SET status = 'applied', verified_at = ?, applied_at = ?, cooling_until = ? WHERE id = ?",
                 (ts, ts, cooling_until, row["id"]))
    row.update(status="applied", verified_at=ts, applied_at=ts, cooling_until=cooling_until)

    label = {"mobile": "Mobile number changed", "email": "Email address changed", "address": "Address changed",
             "rekyc": "Re-KYC completed"}[ctype]
    msg = (f"{label} on {fmt_ist(ts)}. " +
           (f"For your safety, adding beneficiaries, high-value transfers, card limit increases and LRS remittances are "
            f"restricted until {fmt_ist(cooling_until)}. " if cooling_until else "") +
           "Not you? Call 1930 or report fraud immediately.")
    if ctype == "mobile":
        notify_contact(conn, old_mobile, None, label, msg)
        notify_contact(conn, new_value, None, label, msg)
    elif ctype == "email":
        notify_contact(conn, None, old_email, label, msg)
        notify_contact(conn, None, new_value, label, msg)
    else:
        notify_contact(conn, old_mobile, old_email, label, msg)
    notify(conn, ctx.user_id, "profile_change", label, msg)
    audit_ctx(conn, ctx, "admin", "profile_change.applied", entity_type="profile_change_request", entity_id=row["id"],
              request=request, details={"change_type": ctype, "old": row["old_value_masked"], "new": row["new_value_masked"],
                                        "cooling_until": cooling_until})
    return {**_public_change(row), "restricted_actions": restricted_actions(conn) if cooling_until else []}


# ------------------------------------------------------------------ alerts history
@router.get("/me/notifications")
def notifications(limit: int = Query(50, ge=1, le=200), ctx: AuthContext = Depends(require_auth),
                  conn: sqlite3.Connection = Depends(get_db)):
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM notifications WHERE user_id = ? ORDER BY triggered_at DESC, rowid DESC LIMIT ?", (ctx.user_id, limit))]
    total = conn.execute("SELECT COUNT(*) FROM notifications WHERE user_id = ?", (ctx.user_id,)).fetchone()[0]
    return {
        "items": [{"id": r["id"], "event_type": r["event_type"], "channels": jload(r["channels"], []), "title": r["title"],
                   "body": r["body"], "triggered_at": r["triggered_at"], "delivered_at": r["delivered_at"], "read_at": r["read_at"]}
                  for r in rows],
        "total": total,
    }

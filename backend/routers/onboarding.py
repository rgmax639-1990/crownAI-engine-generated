"""Digital account opening (US-14, US-15, US-17): save & resume, PAN, Aadhaar eKYC
(OTP or DigiLocker) through the Aadhaar Data Vault, V-CIP (customer side) and account
creation in Finacle with a virtual debit card.

Privacy rules applied here:
  * the Aadhaar number goes straight into ``aadhaar_vault``; only the reference key and
    the last four digits are stored on the application, and the number is never logged,
    audited or echoed back;
  * the full PAN is kept only encrypted (``onboarding_kyc_details.pan_enc``) for the
    CKYC search/upload and the Finacle customer record; the API returns it masked;
  * the resume link token is random, single-use and stored as a SHA-256 hash.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

import aadhaar_vault
from audit import audit
from banking import account_hash, upsert_customer_from_finacle
from consent_service import current_consent_notice_version, record_consent
from core import (IST, api_error, client_ip, forbidden, get_or_404, invalid, iso, jdump, jload, mask_aadhaar_last4,
                  mask_email, mask_mobile, mask_pan, new_id, now_iso, one, parse_date, parse_iso, require_mobile,
                  today_ist, utcnow, valid_aadhaar, valid_email, valid_pan)
from crypto import decrypt_str, encrypt_str
from db import commit_now, get_db, register_ddl
from integrations import card_management, ckycr, digilocker, finacle, nsdl_pan, sms_gateway, uidai
from integrations.base import is_mock, mock_meta
from notify import notify_contact
from security import AuthContext, mint_onboarding_token, require_onboarding, require_otp, token_hash

router = APIRouter(tags=["onboarding"])

RESUME_DAYS = 7
PUBLIC_SITE_URL = os.getenv("PUBLIC_SITE_URL", "").rstrip("/")
INDIA_BBOX = {"lat_min": 6.5, "lat_max": 35.7, "lng_min": 68.1, "lng_max": 97.5}
MEDIA_SERVER = {
    "region": "India (Mumbai)",
    "note": "Video is relayed and recorded on a media server hosted in India (mock in this environment).",
    "mock": True,
    "label": "MOCK video media server",
}

PRODUCTS: dict[str, dict[str, str]] = {
    "savings_premium": {"label": "Premium Savings Account", "account_type": "savings", "residency": "resident"},
    "savings_classic": {"label": "Classic Savings Account", "account_type": "savings", "residency": "resident"},
    "salary_account": {"label": "Salary Account", "account_type": "savings", "residency": "resident"},
    "nri_nre": {"label": "NRE Savings Account", "account_type": "nre", "residency": "nri"},
}

NEXT_STEP = {
    "started": "pan",
    "pan_verified": "ekyc",
    "ekyc_completed": "vcip",
    "vcip_scheduled": "vcip_scheduled",
    "vcip_waiting": "vcip_waiting",
    "vcip_in_call": "vcip_in_call",
    "vcip_approved": "complete",
    "vcip_rejected": "rejected",
    "completed": "done",
}

register_ddl(
    """
CREATE TABLE IF NOT EXISTS onboarding_kyc_details (
  application_id TEXT PRIMARY KEY,
  pan_enc TEXT,
  pan_name TEXT,
  ekyc_name TEXT,
  ekyc_dob TEXT,
  ekyc_gender TEXT,
  ekyc_photo_ref TEXT,
  name_match INTEGER,
  dob_match INTEGER,
  flags TEXT NOT NULL DEFAULT '[]',
  mock_providers TEXT NOT NULL DEFAULT '[]',
  recording_consent_at TEXT,
  completion TEXT,
  updated_at TEXT NOT NULL
);
"""
)


class Body(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class StartIn(Body):
    mobile: str
    email: str
    otp: str
    product_code: str
    consent_given: Optional[bool] = None
    language: Optional[str] = "en"


class ResumeIn(Body):
    resume_token: str
    mobile: str
    otp: str


class PanIn(Body):
    pan: str
    full_name: Optional[str] = None
    name: Optional[str] = None
    date_of_birth: Optional[str] = None
    dob: Optional[str] = None


class AadhaarOtpIn(Body):
    # ``Any`` so that a malformed value is never echoed back in a validation error.
    aadhaar_number: Any = None
    aadhaar: Any = None
    consent_given: Optional[bool] = None


class AadhaarVerifyIn(Body):
    txn_id: str
    otp: str


class DigilockerIn(Body):
    auth_code: str = ""
    consent_given: Optional[bool] = None


class VcipIn(Body):
    geo_lat: Any = None
    geo_lng: Any = None
    join_now: Optional[bool] = False
    preferred_slot: Optional[str] = None
    consent_recording: Optional[bool] = None
    recording_consent: Optional[bool] = None


# ------------------------------------------------------------------ shared helpers (also used by routers/vcip.py)
def details(conn: sqlite3.Connection, application_id: str) -> dict:
    row = one(conn, "SELECT * FROM onboarding_kyc_details WHERE application_id = ?", (application_id,))
    if not row:
        return {"application_id": application_id, "flags": [], "mock_providers": []}
    row["flags"] = jload(row.get("flags"), []) or []
    row["mock_providers"] = jload(row.get("mock_providers"), []) or []
    return row


def save_details(conn: sqlite3.Connection, application_id: str, **fields: Any) -> None:
    """Upsert the side record. Field names are trusted literals from this module."""
    conn.execute("INSERT OR IGNORE INTO onboarding_kyc_details (application_id, updated_at) VALUES (?, ?)",
                 (application_id, now_iso()))
    if fields:
        cols = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE onboarding_kyc_details SET {cols}, updated_at = ? WHERE application_id = ?",
                     (*fields.values(), now_iso(), application_id))


def _note_mock(conn: sqlite3.Connection, application_id: str, result: dict | None) -> None:
    meta = (result or {}).get("mock")
    if not isinstance(meta, dict) or not meta.get("provider"):
        return
    current = details(conn, application_id)["mock_providers"]
    if meta["provider"] not in current:
        save_details(conn, application_id, mock_providers=jdump(current + [meta["provider"]]))


def norm_name(name: str | None) -> str:
    return " ".join(re.sub(r"[^A-Z ]", " ", (name or "").upper()).split())


def names_match(a: str | None, b: str | None) -> bool:
    na, nb = norm_name(a), norm_name(b)
    return bool(na) and (na == nb or sorted(na.split()) == sorted(nb.split()))


def is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def inside_india(lat: Any, lng: Any) -> bool:
    return (is_number(lat) and is_number(lng) and INDIA_BBOX["lat_min"] <= lat <= INDIA_BBOX["lat_max"]
            and INDIA_BBOX["lng_min"] <= lng <= INDIA_BBOX["lng_max"])


def face_match_score(session_id: str) -> float:
    """Mock face match (eKYC photo vs live video): deterministic 0.80 - 0.99."""
    h = int(hashlib.sha256(("face:" + session_id).encode("utf-8")).hexdigest()[:8], 16)
    return round(0.80 + (h % 1900) / 10000, 2)


def queue_position(conn: sqlite3.Connection, session: dict) -> int | None:
    if session.get("status") != "waiting" or session.get("decision") != "pending":
        return None
    ahead = conn.execute(
        "SELECT COUNT(*) FROM vcip_sessions WHERE status = 'waiting' AND decision = 'pending' AND joined_at < ?",
        (session.get("joined_at") or "",),
    ).fetchone()[0]
    return int(ahead) + 1


def mask_cif(cif: str | None) -> str | None:
    if not cif:
        return None
    return cif[:3] + "XXXXX" + cif[-4:] if len(cif) > 7 else "XXXX"


def session_public(conn: sqlite3.Connection, s: dict | None) -> dict | None:
    """Customer-facing view of a V-CIP session."""
    if not s:
        return None
    live = s["status"] in ("waiting", "in_call") and s["decision"] == "pending"
    return {
        "session_id": s["id"],
        "status": s["status"],
        "decision": s["decision"],
        "officer_message": s["remarks"] if s["decision"] in ("rejected", "reschedule") else None,
        "scheduled_at": s["scheduled_at"],
        "joined_at": s["joined_at"],
        "decided_at": s["decided_at"],
        "random_code": s["random_code"] if live else None,
        "queue_position": queue_position(conn, s),
        "officer_assigned": bool(s["officer_id"]) and s["status"] == "in_call",
        "geo_inside_india": inside_india(s["geo_lat"], s["geo_lng"]),
        "media_server": MEDIA_SERVER,
    }


def _session(conn: sqlite3.Connection, app: dict) -> dict | None:
    if not app.get("vcip_session_id"):
        return None
    return one(conn, "SELECT * FROM vcip_sessions WHERE id = ?", (app["vcip_session_id"],))


def app_public(conn: sqlite3.Connection, app: dict) -> dict:
    det = details(conn, app["id"])
    card = one(conn, "SELECT last4 FROM cards WHERE id = ?", (app["virtual_card_id"],)) if app.get("virtual_card_id") else None
    product = PRODUCTS.get(app["product_code"], {})
    completion = jload(det.get("completion"), None)
    return {
        "application_id": app["id"],
        "product_code": app["product_code"],
        "product_label": product.get("label", app["product_code"]),
        "status": app["status"],
        "next_step": NEXT_STEP.get(app["status"], "pan"),
        "language": app["language"],
        "mobile_masked": mask_mobile(app["mobile"]),
        "email_masked": mask_email(app["email"]),
        "full_name": app["full_name"],
        "date_of_birth": app["date_of_birth"],
        "pan_masked": app["pan_masked"],
        "pan_verified": bool(app["pan_verified"]),
        "ekyc_completed": bool(app["ekyc_completed"]),
        "ekyc_method": app["ekyc_method"],
        "aadhaar_last4": app["aadhaar_last4"],
        "aadhaar_masked": mask_aadhaar_last4(app["aadhaar_last4"]) or None,
        "ekyc_address": app["ekyc_address"],
        "ekyc_name_match": None if det.get("name_match") is None else bool(det.get("name_match")),
        "vcip": session_public(conn, _session(conn, app)),
        "ckyc_result": app["ckyc_result"],
        "account_masked_number": app["account_masked_number"],
        "ifsc": completion.get("ifsc") if isinstance(completion, dict) else None,
        "virtual_card_last4": card["last4"] if card else None,
        "customer_cif_masked": mask_cif(app["finacle_cif"]),
        "netbanking_registration_link": f"/{app['language'] or 'en'}/register" if app["status"] == "completed" else None,
        "resume_expires_at": app["resume_expires_at"],
        "mock_providers": det["mock_providers"],
        "mock": bool(det["mock_providers"]),
        "created_at": app["created_at"],
        "updated_at": app["updated_at"],
    }


def _app(conn: sqlite3.Connection, application_id: str) -> dict:
    return get_or_404(conn, "onboarding_applications", application_id, "Application")


def _update_app(conn: sqlite3.Connection, application_id: str, **fields: Any) -> None:
    fields["updated_at"] = now_iso()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE onboarding_applications SET {cols} WHERE id = ?", (*fields.values(), application_id))


def _add_consent_id(conn: sqlite3.Connection, app: dict, consent_id: str) -> None:
    ids = jload(app.get("consent_record_ids"), []) or []
    ids.append(consent_id)
    _update_app(conn, app["id"], consent_record_ids=jdump(ids))


def _issue_resume_link(conn: sqlite3.Connection, app_id: str, mobile: str, language: str) -> tuple[str | None, str]:
    """Rotate the resume token, SMS the link. Returns (link shown only with the mock SMS gateway, expiry)."""
    token = secrets.token_urlsafe(32)
    expires = iso(utcnow() + timedelta(days=RESUME_DAYS))
    _update_app(conn, app_id, resume_token_hash=token_hash(token), resume_expires_at=expires)
    path = f"/{language}/open-account?resume={token}"
    sms_gateway.get(conn).send_sms(
        mobile,
        f"Continue your account application securely: {PUBLIC_SITE_URL}{path} (valid {RESUME_DAYS} days). "
        "Do not share this link.",
    )
    return (path if is_mock(conn, "sms_gateway") else None), expires  # type: ignore[return-value]


def _age(dob) -> int:
    today = today_ist()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def _slot_ok(dt_ist: datetime) -> bool:
    return dt_ist.weekday() != 6 and 9 <= dt_ist.hour < 18


def available_slots(days: int = 3) -> list[str]:
    """Hourly V-CIP slots (10:00 - 17:00 IST, Monday to Saturday) for the next few business days."""
    now = utcnow()
    out: list[str] = []
    d = now.astimezone(IST).date()
    used_days = 0
    for _ in range(10):
        if d.weekday() != 6:
            day_slots = []
            for hour in range(10, 18):
                start = datetime(d.year, d.month, d.day, hour, 0, tzinfo=IST)
                if start > now + timedelta(minutes=30):
                    day_slots.append(iso(start))
            if day_slots:
                out.extend(s for s in day_slots if s)
                used_days += 1
        if used_days >= days:
            break
        d = d + timedelta(days=1)
    return out


# ------------------------------------------------------------------ start / resume
@router.post("/onboarding/applications", status_code=201)
def start_application(body: StartIn, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    email = (body.email or "").strip().lower()
    if not valid_email(email):
        raise invalid("email", "Enter a valid email address")
    if body.product_code not in PRODUCTS:
        raise invalid("product_code", f"Choose a product: {', '.join(PRODUCTS)}")
    if body.consent_given is not True:
        raise invalid("consent_given", "Please give your consent to the KYC checks to continue", code="consent_required")
    mobile = require_mobile(body.mobile)
    require_otp(conn, mobile, "onboarding", body.otp)
    lang = body.language if body.language in ("en", "hi") else "en"

    app_id = new_id()
    now = now_iso()
    conn.execute(
        """INSERT INTO onboarding_applications (id, product_code, mobile, email, language, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'started', ?, ?)""",
        (app_id, body.product_code, mobile, email, lang, now, now),
    )
    consent = record_consent(conn, subject_type="visitor", subject_id=app_id, purpose="onboarding_kyc", granted=True,
                             notice_version_id=current_consent_notice_version(conn, lang), language=lang)
    _update_app(conn, app_id, consent_record_ids=jdump([consent["id"]]))
    save_details(conn, app_id)
    link, resume_expires = _issue_resume_link(conn, app_id, mobile, lang)
    token, token_expires = mint_onboarding_token(conn, app_id)
    audit(conn, "consent", "onboarding.started", actor_type="visitor", entity_type="onboarding_application", entity_id=app_id,
          details={"product_code": body.product_code, "mobile": mask_mobile(mobile), "consent_record_id": consent["id"]},
          ip=client_ip(request))
    sms_mock = is_mock(conn, "sms_gateway")
    out = {
        "application_id": app_id,
        "access_token": token,
        "token_type": "bearer",
        "token_expires_at": token_expires,
        "status": "started",
        "next_step": NEXT_STEP["started"],
        "resume_link_sent": True,
        "resume_expires_at": resume_expires,
        "consent_record_id": consent["id"],
        "mock": mock_meta("sms_gateway") if sms_mock else None,
    }
    if link:
        out["resume_link"] = link
    return out


@router.post("/onboarding/resume")
def resume_application(body: ResumeIn, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    token = (body.resume_token or "").strip()
    app = one(conn, "SELECT * FROM onboarding_applications WHERE resume_token_hash = ?", (token_hash(token),)) if token else None
    if not app:
        raise api_error(404, "resume_not_found", "This resume link is not valid. It may already have been used. "
                        "Start a new application or use the latest link we sent.")
    if not app["resume_expires_at"] or parse_iso(app["resume_expires_at"]) <= utcnow():  # type: ignore[operator]
        raise api_error(410, "resume_expired", "This resume link has expired. Please start a new application.")
    mobile = require_mobile(body.mobile)
    if mobile != app["mobile"]:
        raise invalid("mobile", "This is not the mobile number used for this application", code="mobile_mismatch")
    require_otp(conn, mobile, "onboarding_resume", body.otp)
    link, resume_expires = _issue_resume_link(conn, app["id"], mobile, app["language"] or "en")
    access, access_expires = mint_onboarding_token(conn, app["id"])
    audit(conn, "consent", "onboarding.resumed", actor_type="visitor", entity_type="onboarding_application", entity_id=app["id"],
          details={"status": app["status"]}, ip=client_ip(request))
    fresh = _app(conn, app["id"])
    out = {
        "application_id": app["id"],
        "access_token": access,
        "token_type": "bearer",
        "token_expires_at": access_expires,
        "status": fresh["status"],
        "next_step": NEXT_STEP.get(fresh["status"], "pan"),
        "resume_link_sent": True,
        "resume_expires_at": resume_expires,
        "application": app_public(conn, fresh),
    }
    if link:
        out["resume_link"] = link
    return out


@router.get("/onboarding/applications/{application_id}")
def get_application(application_id: str, ctx: AuthContext = Depends(require_onboarding),
                    conn: sqlite3.Connection = Depends(get_db)):
    return app_public(conn, _app(conn, application_id))


# ------------------------------------------------------------------ PAN
@router.post("/onboarding/applications/{application_id}/pan")
def verify_pan(application_id: str, body: PanIn, request: Request, ctx: AuthContext = Depends(require_onboarding),
               conn: sqlite3.Connection = Depends(get_db)):
    app = _app(conn, application_id)
    if app["ekyc_completed"] or app["status"] not in ("started", "pan_verified"):
        raise api_error(409, "pan_locked", "PAN cannot be changed after Aadhaar eKYC is complete.")
    pan = (body.pan or "").strip().upper()
    if not valid_pan(pan):
        raise invalid("pan", "Enter PAN as 5 letters, 4 digits and 1 letter, for example ABCPE1234F")
    name = (body.full_name or body.name or "").strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z .'\-]{1,99}", name):
        raise invalid("full_name", "Enter your full name exactly as on your PAN card (letters only)")
    dob = parse_date(body.date_of_birth or body.dob)
    if not dob:
        raise invalid("date_of_birth", "Enter your date of birth as DD/MM/YYYY")
    if dob > today_ist() or _age(dob) > 120:
        raise invalid("date_of_birth", "Enter a valid date of birth")
    if _age(dob) < 18:
        raise invalid("date_of_birth", "You must be at least 18 years old to open this account online", code="underage")

    res = nsdl_pan.get(conn).verify(pan, name, dob.isoformat())
    _note_mock(conn, application_id, res)
    if not res.get("valid"):
        audit(conn, "integration", "onboarding.pan_failed", actor_type="visitor", entity_type="onboarding_application",
              entity_id=application_id, details={"pan": mask_pan(pan), "status": res.get("status")}, ip=client_ip(request))
        commit_now(conn)
        raise invalid("pan", "We could not verify this PAN with the Income Tax Department. Check the number and try again.",
                      code="pan_invalid")
    if not res.get("individual"):
        raise invalid("pan", "Only an individual's PAN (4th character 'P') can be used to open this account",
                      code="pan_not_individual")
    if res.get("name_match") is False:
        raise invalid("full_name", "The name does not match the PAN records", code="pan_name_mismatch")

    _update_app(conn, application_id, pan_masked=mask_pan(pan), full_name=name, date_of_birth=dob.isoformat(),
                pan_verified=1, status="pan_verified")
    save_details(conn, application_id, pan_enc=encrypt_str(pan), pan_name=name)
    audit(conn, "integration", "onboarding.pan_verified", actor_type="visitor", entity_type="onboarding_application",
          entity_id=application_id, details={"pan": mask_pan(pan), "status": res.get("status")}, ip=client_ip(request))
    out = app_public(conn, _app(conn, application_id))
    out["pan_verification"] = {"status": res.get("status"), "status_text": res.get("status_text"),
                               "category": res.get("category"), "mock": res.get("mock")}
    return out


# ------------------------------------------------------------------ Aadhaar eKYC
def _require_pan_first(app: dict) -> None:
    if app["ekyc_completed"]:
        raise api_error(409, "ekyc_already_completed", "Aadhaar eKYC is already complete for this application.")
    if not app["pan_verified"]:
        raise api_error(409, "pan_required", "Verify your PAN before Aadhaar eKYC.")


def _finish_ekyc(conn: sqlite3.Connection, app: dict, method: str, ekyc: dict, request: Request, **extra: Any) -> dict:
    name_ok = names_match(app["full_name"], ekyc.get("name"))
    ek_dob = parse_date(ekyc.get("dob"))
    dob_ok = bool(ek_dob and app["date_of_birth"] and ek_dob.isoformat() == app["date_of_birth"])
    flags = []
    if not name_ok:
        flags.append("name_mismatch")
    if not dob_ok:
        flags.append("dob_mismatch")
    _update_app(conn, app["id"], ekyc_completed=1, ekyc_method=method, ekyc_address=ekyc.get("address"),
                status="ekyc_completed", **extra)
    save_details(conn, app["id"], ekyc_name=ekyc.get("name"), ekyc_dob=ek_dob.isoformat() if ek_dob else None,
                 ekyc_gender=ekyc.get("gender"), ekyc_photo_ref=ekyc.get("photo_ref"), name_match=int(name_ok),
                 dob_match=int(dob_ok), flags=jdump(flags))
    audit(conn, "integration", "onboarding.ekyc_completed", actor_type="visitor", entity_type="onboarding_application",
          entity_id=app["id"], details={"method": method, "name_match": name_ok, "dob_match": dob_ok}, ip=client_ip(request))
    out = app_public(conn, _app(conn, app["id"]))
    out["ekyc"] = {
        "name": ekyc.get("name"),
        "date_of_birth": ek_dob.isoformat() if ek_dob else None,
        "gender": ekyc.get("gender"),
        "address": ekyc.get("address"),
        "name_match": name_ok,
        "flagged_for_officer": bool(flags),
        "flags": flags,
    }
    return out


@router.post("/onboarding/applications/{application_id}/ekyc/aadhaar-otp")
def aadhaar_otp(application_id: str, body: AadhaarOtpIn, request: Request, ctx: AuthContext = Depends(require_onboarding),
                conn: sqlite3.Connection = Depends(get_db)):
    app = _app(conn, application_id)
    _require_pan_first(app)
    if body.consent_given is not True:
        raise invalid("consent_given", "Please give your consent for Aadhaar OTP eKYC to continue", code="consent_required")
    raw = body.aadhaar_number if body.aadhaar_number is not None else body.aadhaar
    if isinstance(raw, int) and not isinstance(raw, bool):
        raw = str(raw)
    digits = re.sub(r"[\s-]", "", raw) if isinstance(raw, str) else ""
    if not valid_aadhaar(digits):
        raise invalid("aadhaar_number", "Enter a valid 12-digit Aadhaar number", code="aadhaar_invalid")
    ref, last4 = aadhaar_vault.store(conn, digits)
    digits = ""  # the number is not kept anywhere outside the vault
    consent = record_consent(conn, subject_type="visitor", subject_id=application_id, purpose="aadhaar_ekyc", granted=True,
                             notice_version_id=current_consent_notice_version(conn, app["language"] or "en"),
                             language=app["language"] or "en")
    _add_consent_id(conn, app, consent["id"])
    res = uidai.get(conn).request_otp(ref, last4)
    _note_mock(conn, application_id, res)
    _update_app(conn, application_id, aadhaar_vault_ref=ref, aadhaar_last4=last4, ekyc_txn_id=res["txn_id"])
    audit(conn, "consent", "onboarding.aadhaar_otp_requested", actor_type="visitor", entity_type="onboarding_application",
          entity_id=application_id, details={"aadhaar": mask_aadhaar_last4(last4), "consent_record_id": consent["id"]},
          ip=client_ip(request))
    out = {
        "txn_id": res["txn_id"],
        "aadhaar_last4": last4,
        "aadhaar_masked": mask_aadhaar_last4(last4),
        "masked": mask_aadhaar_last4(last4),
        "otp_sent_to": res.get("otp_sent_to"),
        "consent_record_id": consent["id"],
        "mock": res.get("mock"),
    }
    if res.get("mock") and res.get("mock_otp"):
        out["mock_otp"] = res["mock_otp"]
    return out


@router.post("/onboarding/applications/{application_id}/ekyc/aadhaar-otp/verify")
def aadhaar_verify(application_id: str, body: AadhaarVerifyIn, request: Request,
                   ctx: AuthContext = Depends(require_onboarding), conn: sqlite3.Connection = Depends(get_db)):
    app = _app(conn, application_id)
    _require_pan_first(app)
    if not app["ekyc_txn_id"] or body.txn_id != app["ekyc_txn_id"]:
        raise invalid("txn_id", "This OTP request does not belong to this application. Request a new OTP.", code="txn_mismatch")
    res = uidai.get(conn).verify_otp(body.txn_id, body.otp, name_hint=app["full_name"], dob_hint=app["date_of_birth"])
    _note_mock(conn, application_id, res)
    if not res.get("ok"):
        raise invalid("otp", res.get("message") or "The Aadhaar OTP is incorrect", code="otp_invalid")
    out = _finish_ekyc(conn, app, "aadhaar_otp", res["ekyc"], request)
    out["mock"] = res.get("mock") or out.get("mock")
    return out


@router.post("/onboarding/applications/{application_id}/ekyc/digilocker")
def ekyc_digilocker(application_id: str, body: DigilockerIn, request: Request,
                    ctx: AuthContext = Depends(require_onboarding), conn: sqlite3.Connection = Depends(get_db)):
    app = _app(conn, application_id)
    _require_pan_first(app)
    if not body.auth_code:
        raise invalid("auth_code", "DigiLocker authorisation is missing. Start the DigiLocker step again.")
    res = digilocker.get(conn).exchange(body.auth_code, name_hint=app["full_name"], dob_hint=app["date_of_birth"])
    _note_mock(conn, application_id, res)
    if not res.get("ok"):
        raise invalid("auth_code", res.get("message") or "DigiLocker authorisation failed", code="digilocker_failed")
    consent = record_consent(conn, subject_type="visitor", subject_id=application_id, purpose="aadhaar_ekyc", granted=True,
                             notice_version_id=current_consent_notice_version(conn, app["language"] or "en"),
                             language=app["language"] or "en")
    _add_consent_id(conn, app, consent["id"])
    ekyc = res["ekyc"]
    out = _finish_ekyc(conn, app, "digilocker", ekyc, request, aadhaar_last4=ekyc.get("aadhaar_last4"),
                       ekyc_txn_id=res.get("digilocker_txn"))
    out["mock"] = res.get("mock") or out.get("mock")
    return out


# ------------------------------------------------------------------ V-CIP (customer side)
def _require_ekyc(app: dict) -> None:
    if not app["ekyc_completed"]:
        raise api_error(409, "ekyc_required", "Complete Aadhaar eKYC before video KYC.")


@router.get("/onboarding/applications/{application_id}/vcip")
def get_vcip(application_id: str, ctx: AuthContext = Depends(require_onboarding), conn: sqlite3.Connection = Depends(get_db)):
    app = _app(conn, application_id)
    _require_ekyc(app)
    return {
        "application_id": application_id,
        "status": app["status"],
        "next_step": NEXT_STEP.get(app["status"], "vcip"),
        "session": session_public(conn, _session(conn, app)),
        "slots": available_slots(),
        "business_hours": "09:00 - 18:00 IST, Monday to Saturday",
        "media_server": MEDIA_SERVER,
    }


@router.post("/onboarding/applications/{application_id}/vcip")
def start_vcip(application_id: str, body: VcipIn, request: Request, ctx: AuthContext = Depends(require_onboarding),
               conn: sqlite3.Connection = Depends(get_db)):
    app = _app(conn, application_id)
    _require_ekyc(app)
    if app["status"] in ("vcip_approved", "completed"):
        raise api_error(409, "vcip_already_approved", "Video KYC is already approved for this application.")
    if app["status"] == "vcip_rejected":
        raise api_error(409, "application_rejected", "Video KYC was not approved. Visit a branch to complete your KYC.")
    if not is_number(body.geo_lat) or not is_number(body.geo_lng):
        raise invalid("geo_lat", "Allow location access so we can confirm you are in India", code="location_required")
    if not inside_india(body.geo_lat, body.geo_lng):
        audit(conn, "integration", "vcip.geo_blocked", actor_type="visitor", entity_type="onboarding_application",
              entity_id=application_id, details={"lat": round(body.geo_lat, 1), "lng": round(body.geo_lng, 1)},
              ip=client_ip(request))
        commit_now(conn)
        raise forbidden("Video KYC can only be done while you are in India. Please try again from within India.", "outside_india")
    if body.consent_recording is not True and body.recording_consent is not True:
        raise invalid("consent_recording", "Please consent to the video call being recorded to continue", code="consent_required")

    join = bool(body.join_now)
    slot_utc = None
    if not join:
        if not body.preferred_slot:
            raise invalid("preferred_slot", "Choose a slot or join now")
        slot_utc = parse_iso(body.preferred_slot)
        if not slot_utc:
            raise invalid("preferred_slot", "Choose a valid date and time")
        if slot_utc <= utcnow():
            raise invalid("preferred_slot", "Choose a time in the future")
        if slot_utc > utcnow() + timedelta(days=30):
            raise invalid("preferred_slot", "Choose a slot within the next 30 days")
        if not _slot_ok(slot_utc.astimezone(IST)):
            raise invalid("preferred_slot", "Choose a slot between 09:00 and 18:00 IST, Monday to Saturday",
                          code="outside_business_hours")

    existing = _session(conn, app)
    if existing and existing["decision"] == "pending" and existing["status"] == "in_call":
        raise api_error(409, "vcip_in_progress", "Your video call is already in progress.")
    reuse = existing if existing and existing["decision"] == "pending" and existing["status"] in ("scheduled", "waiting") else None
    now = now_iso()
    if join:
        status, scheduled_at, joined_at = "waiting", (reuse or {}).get("scheduled_at"), now
        code = (reuse or {}).get("random_code") if reuse and reuse["status"] == "waiting" else f"{secrets.randbelow(10000):04d}"
    else:
        status, scheduled_at, joined_at, code = "scheduled", iso(slot_utc), None, None
    if reuse:
        sid = reuse["id"]
        if reuse["status"] == "waiting" and join:
            joined_at = reuse["joined_at"]  # keep the place in the queue
        conn.execute(
            """UPDATE vcip_sessions SET status = ?, scheduled_at = ?, joined_at = ?, geo_lat = ?, geo_lng = ?, random_code = ?
               WHERE id = ?""",
            (status, scheduled_at, joined_at, float(body.geo_lat), float(body.geo_lng), code, sid),
        )
    else:
        sid = new_id()
        conn.execute(
            """INSERT INTO vcip_sessions (id, application_id, scheduled_at, joined_at, geo_lat, geo_lng, face_match_score,
               random_code, status, decision, created_at) VALUES (?,?,?,?,?,?,?,?,?, 'pending', ?)""",
            (sid, application_id, scheduled_at, joined_at, float(body.geo_lat), float(body.geo_lng), face_match_score(sid),
             code, status, now),
        )
    _update_app(conn, application_id, vcip_session_id=sid, status="vcip_waiting" if join else "vcip_scheduled")
    save_details(conn, application_id, recording_consent_at=now)
    audit(conn, "consent", "vcip.recording_consent", actor_type="visitor", entity_type="vcip_session", entity_id=sid,
          details={"application_id": application_id}, ip=client_ip(request))
    audit(conn, "integration", "vcip.joined" if join else "vcip.scheduled", actor_type="visitor", entity_type="vcip_session",
          entity_id=sid, details={"application_id": application_id, "scheduled_at": scheduled_at}, ip=client_ip(request))
    session = one(conn, "SELECT * FROM vcip_sessions WHERE id = ?", (sid,))
    out = {
        "application_id": application_id,
        "status": "vcip_waiting" if join else "vcip_scheduled",
        "next_step": NEXT_STEP["vcip_waiting" if join else "vcip_scheduled"],
        "session": session_public(conn, session),
        "media_server": MEDIA_SERVER,
    }
    if join:
        out["media_token"] = "MOCK-MEDIA-" + secrets.token_urlsafe(18)
        out["instructions"] = [
            "Keep your original PAN card ready to show on camera.",
            "Read the 4-digit code on your screen aloud when the officer asks.",
            "Sit in a well-lit place and do not leave the call.",
        ]
    return out


# ------------------------------------------------------------------ completion
@router.post("/onboarding/applications/{application_id}/complete")
def complete_application(application_id: str, request: Request, ctx: AuthContext = Depends(require_onboarding),
                         conn: sqlite3.Connection = Depends(get_db)):
    app = _app(conn, application_id)
    det = details(conn, application_id)
    if app["status"] == "completed":
        done = jload(det.get("completion"), {}) or {}
        return {**done, "application_id": application_id, "status": "completed", "next_step": "done",
                "already_completed": True, "account_number": None}
    if app["status"] != "vcip_approved":
        raise api_error(409, "vcip_not_approved", "Your video KYC has to be approved before the account can be opened.",
                        application_status=app["status"], next_step=NEXT_STEP.get(app["status"]))
    product = PRODUCTS.get(app["product_code"], PRODUCTS["savings_classic"])
    pan = decrypt_str(det.get("pan_enc"))
    mocks: list[dict] = []

    # 1. CKYC registry: reuse an existing KYC identifier or upload the new record.
    registry = ckycr.get(conn)
    found = registry.search(pan) if pan else {"found": False}
    if found.get("mock"):
        mocks.append(found["mock"])
    if found.get("found"):
        ckyc_result, ckyc_number = "found", found.get("ckyc_number")
    else:
        up = registry.upload(pan=pan or "", full_name=app["full_name"] or "", date_of_birth=app["date_of_birth"],
                             address=app["ekyc_address"], photo_ref=det.get("ekyc_photo_ref"))
        if up.get("mock"):
            mocks.append(up["mock"])
        ckyc_result, ckyc_number = "not_found_uploaded", up.get("ckyc_number")

    # 2. Finacle: customer + account; then the platform customer and linked account.
    core = finacle.get(conn)
    cif = core.create_customer(full_name=app["full_name"] or "Customer", mobile=app["mobile"], email=app["email"],
                               date_of_birth=app["date_of_birth"], pan=pan, address=app["ekyc_address"],
                               segment="nri" if product["residency"] == "nri" else "retail",
                               residency_status=product["residency"])
    opened = core.open_account(cif, product["account_type"])
    if opened.get("mock"):
        mocks.append(opened["mock"])
    acct_no = opened["account_number"]
    ifsc = opened.get("ifsc") or finacle.HOME_IFSC
    customer = upsert_customer_from_finacle(conn, cif)
    conn.execute(
        """UPDATE customers SET aadhaar_vault_ref = ?, aadhaar_last4 = ?, ckyc_number = ?, kyc_status = 'full' WHERE id = ?""",
        (app["aadhaar_vault_ref"], app["aadhaar_last4"], ckyc_number, customer["id"]),
    )
    account = one(conn, "SELECT * FROM accounts WHERE account_ref_hash = ?", (account_hash(acct_no),))

    # 3. Virtual debit card from the card management system (no card number is stored).
    issued = card_management.get(conn).issue_virtual_debit(cif, acct_no)
    if issued.get("mock"):
        mocks.append(issued["mock"])
    card_id = new_id()
    now = now_iso()
    conn.execute(
        """INSERT INTO cards (id, customer_id, account_id, cms_card_ref, card_kind, network, variant, last4, expiry, status,
           created_at, updated_at, last_change_summary) VALUES (?,?,?,?, 'virtual_debit', ?, ?, ?, ?, 'active', ?, ?, ?)""",
        (card_id, customer["id"], account["id"] if account else None, issued["cms_card_ref"], issued.get("network") or "RuPay",
         "Virtual Debit", issued["last4"], issued.get("expiry"), now, now, "Issued at account opening with default controls"),
    )

    masked = account["masked_number"] if account else "XXXX" + acct_no[-4:]
    lang = app["language"] or "en"
    completion = {
        "account_masked_number": masked,
        "account_type": product["account_type"],
        "product_label": product["label"],
        "ifsc": ifsc,
        "virtual_card_last4": issued["last4"],
        "virtual_card_network": issued.get("network") or "RuPay",
        "customer_cif_masked": mask_cif(cif),
        "ckyc_result": ckyc_result,
        "netbanking_registration_link": f"/{lang}/register",
        "completed_at": now,
    }
    _update_app(conn, application_id, status="completed", ckyc_result=ckyc_result, ckyc_number=ckyc_number, finacle_cif=cif,
                account_masked_number=masked, customer_id=customer["id"], virtual_card_id=card_id)
    save_details(conn, application_id, completion=jdump(completion))
    for m in mocks:
        _note_mock(conn, application_id, {"mock": m})
    audit(conn, "financial", "onboarding.completed", actor_type="visitor", entity_type="onboarding_application",
          entity_id=application_id, details={"account": masked, "cif": mask_cif(cif), "ckyc_result": ckyc_result,
                                             "virtual_card_last4": issued["last4"]}, ip=client_ip(request))
    notify_contact(conn, app["mobile"], app["email"], "Account opened",
                   f"Your {product['label']} {masked} is open (IFSC {ifsc}). Your virtual debit card ending {issued['last4']} "
                   "is ready. Register for internet banking on our website. Never share your OTP or PIN.")
    return {
        **completion,
        "application_id": application_id,
        "status": "completed",
        "next_step": "done",
        "already_completed": False,
        # Shown once so the customer can register for internet banking (synthetic data in mock mode).
        "account_number": acct_no,
        "mock": mocks[0] if mocks else None,
        "mock_providers": [m.get("provider") for m in mocks],
    }

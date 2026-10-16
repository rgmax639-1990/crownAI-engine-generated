"""Request-a-call leads: consent-backed lead capture pushed to the CRM adapter."""
from __future__ import annotations

import re
import sqlite3
from datetime import timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from audit import audit
from consent_service import current_consent_notice_version, record_consent
from core import (api_error, client_ip, invalid, iso, jdump, mask_mobile, new_id, now_iso, one, reference_no, require_mobile,
                  sha256_hex, utcnow, valid_email, valid_pin)
from db import get_db
from integrations import crm
from integrations.base import IntegrationError

router = APIRouter(tags=["leads"])

CHANNELS = ("web", "mobile_web", "calculator", "assistant", "branch_page")
MAX_LEADS_PER_MOBILE_PER_HOUR = 5
NAME_RE = re.compile(r"^[A-Za-zऀ-ॿ][A-Za-zऀ-ॿ .'\-]{1,99}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,79}$")
UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "source", "medium", "campaign", "term", "content")


class LeadIn(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    name: str = ""
    mobile: str = ""
    email: Optional[str] = None
    pin_code: Optional[str] = None
    pin: Optional[str] = None  # alias of pin_code
    product_slug: str = ""
    channel: str = "web"
    consent_given: bool = False
    language: Optional[str] = "en"
    utm: Optional[dict[str, Any]] = None
    calculator_share_id: Optional[str] = None


def _clean_utm(raw: dict[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in (raw or {}).items():
        key = str(k).lower()
        if key in UTM_KEYS and v is not None:
            key = key if key.startswith("utm_") else f"utm_{key}"
            out[key] = re.sub(r"[^\w .:/\-]", "", str(v))[:100]
    return out


@router.post("/leads", status_code=201)
def create_lead(body: LeadIn, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    name = re.sub(r"\s+", " ", body.name or "").strip()
    if not NAME_RE.match(name):
        raise invalid("name", "Enter your name (2 to 100 letters)")
    mobile = require_mobile(body.mobile)
    email = (body.email or "").strip().lower() or None
    if email and not valid_email(email):
        raise invalid("email", "Enter a valid email address or leave it blank")
    pin = (body.pin_code or body.pin or "").strip() or None
    if pin and not valid_pin(pin):
        raise invalid("pin_code", "Enter a valid 6-digit PIN code or leave it blank")
    slug = (body.product_slug or "").strip().lower()
    if not SLUG_RE.match(slug):
        raise invalid("product_slug", "Choose the product you are interested in")
    if body.channel not in CHANNELS:
        raise invalid("channel", f"Channel must be one of: {', '.join(CHANNELS)}")
    if body.consent_given is not True:
        raise invalid("consent_given", "Please agree to be contacted about this product so we can call you back")
    share_id = (body.calculator_share_id or "").strip() or None
    if share_id and not one(conn, "SELECT id FROM calculator_shares WHERE id = ?", (share_id[:64],)):
        raise invalid("calculator_share_id", "The shared calculation was not found or has expired")
    language = body.language if body.language in ("en", "hi") else "en"

    since = iso(utcnow() - timedelta(hours=1))
    recent = conn.execute("SELECT COUNT(*) FROM leads WHERE mobile = ? AND created_at >= ?", (mobile, since)).fetchone()[0]
    if recent >= MAX_LEADS_PER_MOBILE_PER_HOUR:
        raise api_error(429, "lead_rate_limited",
                        "We have already received several requests from this mobile number. Please try again in an hour.")

    # DPDP: the call-back consent is recorded against a pseudonymous subject derived from the mobile number,
    # so it can be withdrawn later without storing the number in the consent record.
    subject_id = "lead-" + sha256_hex("lead_contact:" + mobile)[:40]
    consent = record_consent(conn, subject_type="visitor", subject_id=subject_id, purpose="lead_contact", granted=True,
                             notice_version_id=current_consent_notice_version(conn, language), language=language)

    lead_id = new_id()
    ref = reference_no("LD")
    now = now_iso()
    utm = _clean_utm(body.utm)
    conn.execute(
        """INSERT INTO leads (id, reference_no, name, mobile, email, pin_code, product_slug, channel, utm, consent_record_id,
           calculator_share_id, crm_ref, status, language, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,'created',?,?)""",
        (lead_id, ref, name, mobile, email, pin, slug, body.channel, jdump(utm), consent["id"], share_id, language, now),
    )

    crm_ref = None
    mock = None
    status = "created"
    crm_error = None
    try:
        res = crm.get(conn).create_lead({
            "reference_no": ref, "name": name, "mobile": mobile, "email": email, "pin_code": pin, "product_slug": slug,
            "channel": body.channel, "utm": utm, "language": language, "calculator_share_id": share_id,
        })
        crm_ref = res.get("crm_ref")
        mock = res.get("mock")
        status = "synced_to_crm"
        conn.execute("UPDATE leads SET crm_ref = ?, status = 'synced_to_crm' WHERE id = ?", (crm_ref, lead_id))
    except IntegrationError as exc:
        # The lead is kept (status 'created') and retried from the outbox; the customer still gets a reference.
        crm_error = exc.code
        conn.execute("INSERT INTO outbox (id, topic, payload, created_at) VALUES (?,?,?,?)",
                     (new_id(), "crm.lead_retry", jdump({"lead_id": lead_id}), now))

    ip = client_ip(request)
    audit(conn, "consent", "lead.created", actor_type="visitor", entity_type="lead", entity_id=lead_id,
          details={"reference_no": ref, "product_slug": slug, "channel": body.channel, "mobile": mask_mobile(mobile),
                   "consent_record_id": consent["id"], "crm_ref": crm_ref, "crm_error": crm_error}, ip=ip)
    if crm_ref:
        audit(conn, "integration", "crm.lead_pushed", actor_type="system", entity_type="lead", entity_id=lead_id,
              details={"crm_ref": crm_ref, "mock": bool(mock)}, ip=ip)
    return {
        "reference_no": ref,
        "status": status,
        "crm_ref": crm_ref,
        "mock": mock,
        "message": "Thank you. Our team will call you within one working day." if language == "en"
        else "धन्यवाद। हमारी टीम एक कार्य दिवस के भीतर आपको कॉल करेगी।",
    }

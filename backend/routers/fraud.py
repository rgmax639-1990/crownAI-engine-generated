"""24x7 fraud reporting with an immediate card block.

Card blocks are *temporary* (``temporarily_blocked``): the block is instant and fully stops
the card, but stays reversible after the bank verifies the report, so a mistaken or
malicious report (only an OTP on the registered mobile is needed) cannot destroy a card
for good. The bank hot-lists and reissues the card permanently after investigation.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from audit import audit
from core import (IST, client_ip, forbidden, get_reg_value, invalid, mask_mobile, new_id, not_found, now_iso, one, parse_iso,
                  redact_pii, reference_no, require_mobile)
from db import get_db
from integrations import card_management
from integrations.base import IntegrationError
from notify import notify
from security import AuthContext, require_customer, require_otp

router = APIRouter(tags=["fraud"])

CATEGORIES = ("card_misuse", "upi_fraud", "phishing", "account_takeover", "other")
CYBERCRIME_URL = "https://cybercrime.gov.in"
HELPLINE = "1930"
BLOCK_NOTE = {
    "en": "For your safety the card is blocked temporarily at once. It cannot be used for any transaction. After we verify "
          "the report it is hot-listed permanently and a replacement card is issued.",
    "hi": "आपकी सुरक्षा के लिए कार्ड तुरंत अस्थायी रूप से ब्लॉक कर दिया गया है। इससे कोई लेन-देन नहीं हो सकता। रिपोर्ट की जाँच "
          "के बाद इसे स्थायी रूप से हॉट-लिस्ट कर नया कार्ड जारी किया जाएगा।",
}


class Body(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class FraudIn(Body):
    mobile: str = ""
    otp: Optional[str] = None
    category: str = ""
    card_last4: Optional[str] = None
    block_card: bool = False
    description: Optional[str] = None
    language: Optional[str] = "en"


class MyFraudIn(Body):
    category: str = ""
    card_id: Optional[str] = None
    block_card: bool = True
    description: Optional[str] = None
    language: Optional[str] = None


def _lang(v: str | None) -> str:
    return "hi" if v == "hi" else "en"


def _guidance(conn: sqlite3.Connection, lang: str, block_status: str) -> dict:
    days = get_reg_value(conn, "zero_liability_report_days", 3) or 3
    if lang == "hi":
        zl = (f"यदि आप अनधिकृत लेन-देन की सूचना {days} कार्य दिवसों के भीतर देते हैं, तो आरबीआई नियमों के अनुसार आपकी देयता शून्य होती है। "
              "बाद में सूचना देने पर सीमित देयता लागू हो सकती है।")
        steps = ["अपना OTP, PIN, CVV या पासवर्ड किसी के साथ साझा न करें — बैंक कभी नहीं पूछता।",
                 f"तुरंत {HELPLINE} पर कॉल करें या {CYBERCRIME_URL} पर साइबर अपराध की शिकायत दर्ज करें।",
                 "अपना इंटरनेट बैंकिंग पासवर्ड और UPI PIN बदलें।",
                 "यह संदर्भ संख्या सुरक्षित रखें; हम 10 कार्य दिवसों में अपडेट देंगे।"]
        not_found_text = "हमें इस मोबाइल नंबर से जुड़ा इस अंक वाला कार्ड नहीं मिला। कार्ड ब्लॉक करने के लिए तुरंत 1800-000-0000 (24x7) पर कॉल करें।"
    else:
        zl = (f"Report an unauthorised transaction within {days} working days and your liability is zero under the RBI customer "
              "liability framework. Reporting later can mean limited liability.")
        steps = ["Never share your OTP, PIN, CVV or password. The bank never asks for them.",
                 f"Call the national cyber-fraud helpline {HELPLINE} now, or file a complaint at {CYBERCRIME_URL}.",
                 "Change your internet banking password and UPI PIN.",
                 "Keep this reference number; we will update you within 10 working days."]
        not_found_text = ("We could not find a card ending with these digits linked to this mobile number. "
                          "Call our 24x7 helpline 1800-000-0000 to block the card now.")
    return {
        "zero_liability_days": days,
        "zero_liability_text": zl,
        "helpline": HELPLINE,
        "helpline_href": f"tel:{HELPLINE}",
        "cybercrime_url": CYBERCRIME_URL,
        "bank_helpline": "1800-000-0000",
        "bank_helpline_href": "tel:18000000000",
        "next_steps": steps,
        "card_not_found_text": not_found_text if block_status == "not_found" else None,
    }


def _block(conn: sqlite3.Connection, card: dict, reason: str) -> tuple[str, str | None, dict | None]:
    """Temporarily block ``card`` through the card management system. Returns (status, blocked_at, mock)."""
    if card["status"] in ("temporarily_blocked", "permanently_blocked"):
        return "already_blocked", card.get("blocked_at"), None
    try:
        res = card_management.get(conn).block(card["cms_card_ref"], permanent=False, reason=reason)
    except IntegrationError:
        return "failed", None, None
    blocked_at = res.get("blocked_at") or now_iso()
    conn.execute(
        """UPDATE cards SET status = 'temporarily_blocked', blocked_at = ?, updated_at = ?, last_change_summary = ?
           WHERE id = ?""",
        (blocked_at, now_iso(), f"Blocked after fraud report ({reason})", card["id"]),
    )
    return "blocked", blocked_at, res.get("mock")


def _create(conn: sqlite3.Connection, request: Request, *, customer_id: str | None, mobile: str, card: dict | None,
            card_last4: str | None, category: str, description: str | None, block_requested: bool, lang: str,
            actor_type: str, actor_id: str | None) -> dict:
    ref = reference_no("FRD")
    reported_at = now_iso()
    block_status, blocked_at, mock = "not_requested", None, None
    if block_requested:
        if card:
            block_status, blocked_at, mock = _block(conn, card, ref)
        else:
            block_status = "not_found"
    rid = new_id()
    conn.execute(
        """INSERT INTO fraud_reports (id, reference_no, customer_id, card_id, card_last4, mobile_masked, category, description,
           reported_at, card_blocked_at, status) VALUES (?,?,?,?,?,?,?,?,?,?, 'received')""",
        (rid, ref, customer_id, card["id"] if card else None, card_last4, mask_mobile(mobile), category, description,
         reported_at, blocked_at if block_status == "blocked" else None),
    )
    audit(conn, "financial", "fraud.reported", actor_type=actor_type, actor_id=actor_id, entity_type="fraud_report",
          entity_id=rid, details={"reference_no": ref, "category": category, "card_last4": card_last4,
                                  "card_block_status": block_status, "mobile": mask_mobile(mobile)}, ip=client_ip(request))
    if block_status == "blocked" and card:
        audit(conn, "financial", "card.blocked", actor_type=actor_type, actor_id=actor_id, entity_type="card", entity_id=card["id"],
              details={"reason": "fraud_report", "reference_no": ref, "permanent": False}, ip=client_ip(request))
    if customer_id:
        user_ids = [r["id"] for r in conn.execute("SELECT id FROM users WHERE customer_id = ? AND user_type = 'retail'", (customer_id,))]
        when = parse_iso(reported_at).astimezone(IST).strftime("%d/%m/%Y %H:%M IST")  # type: ignore[union-attr]
        for uid in user_ids:
            body = f"Fraud report {ref} received at {when}."
            if block_status == "blocked" and card:
                body += f" Card ending {card['last4']} is blocked."
            body += " Not you? Call 1930 or the bank immediately."
            notify(conn, uid, "security", "Fraud report received", body)
    return {
        "reference_no": ref,
        "reported_at": reported_at,
        "status": "received",
        "category": category,
        "card_block_status": block_status,
        "card": {"id": card["id"], "last4": card["last4"], "card_kind": card["card_kind"],
                 "status": "temporarily_blocked" if block_status == "blocked" else card["status"]} if card else None,
        "card_last4": card_last4,
        "card_blocked_at": blocked_at,
        "block_note": BLOCK_NOTE[lang] if block_status == "blocked" else None,
        "guidance": _guidance(conn, lang, block_status),
        "mock": mock,
    }


def _check_category(category: str) -> None:
    if category not in CATEGORIES:
        raise invalid("category", "Choose the type of fraud")


def _clean_description(v: str | None) -> str | None:
    d = redact_pii((v or "").strip())
    if len(d) > 1000:
        raise invalid("description", "Keep the description within 1000 characters")
    return d or None


@router.post("/fraud-reports", status_code=201)
def report_fraud(body: FraudIn, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    mobile = require_mobile(body.mobile)
    _check_category(body.category)
    last4 = (body.card_last4 or "").strip() or None
    if last4 and not re.fullmatch(r"\d{4}", last4):
        raise invalid("card_last4", "Enter the last 4 digits of the card")
    if body.block_card and not last4:
        raise invalid("card_last4", "Enter the last 4 digits of the card to block")
    description = _clean_description(body.description)
    lang = _lang(body.language)
    require_otp(conn, mobile, "fraud_report", body.otp)

    customer_ids = [r["id"] for r in conn.execute("SELECT id FROM customers WHERE mobile = ?", (mobile,))]
    card = None
    if last4 and customer_ids:
        card = one(conn, f"""SELECT * FROM cards WHERE last4 = ? AND customer_id IN ({','.join('?' * len(customer_ids))})
                             ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, created_at LIMIT 1""",
                   (last4, *customer_ids))
    customer_id = card["customer_id"] if card else (customer_ids[0] if customer_ids else None)
    return _create(conn, request, customer_id=customer_id, mobile=mobile, card=card, card_last4=last4, category=body.category,
                   description=description, block_requested=body.block_card, lang=lang, actor_type="visitor", actor_id=None)


@router.post("/me/fraud-reports", status_code=201)
def report_fraud_signed_in(body: MyFraudIn, request: Request, ctx: AuthContext = Depends(require_customer),
                           conn: sqlite3.Connection = Depends(get_db)):
    _check_category(body.category)
    description = _clean_description(body.description)
    card = None
    if body.card_id:
        if len(body.card_id) > 64:
            raise not_found("Card")
        card = one(conn, "SELECT * FROM cards WHERE id = ?", (body.card_id,))
        if not card:
            raise not_found("Card")
        if card["customer_id"] != ctx.customer_id:
            raise forbidden("This card does not belong to you")
    elif body.block_card:
        raise invalid("card_id", "Choose the card to block")
    lang = _lang(body.language or ctx.user.get("preferred_language"))
    return _create(conn, request, customer_id=ctx.customer_id, mobile=ctx.user["mobile"], card=card,
                   card_last4=card["last4"] if card else None, category=body.category, description=description,
                   block_requested=body.block_card and card is not None, lang=lang, actor_type="customer",
                   actor_id=ctx.user_id)

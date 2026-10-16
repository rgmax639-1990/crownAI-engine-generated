"""DPDP consent: the published consent notice, visitor cookie consent and signed-in users' consents.

Every choice is appended as a ``consent_records`` row (history is never overwritten) and
tied to the published notice version that was shown. Withdrawal takes effect at once:
every check reads the latest row through ``consent_service``.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from audit import audit, audit_ctx
from cms_seed import seed_published
from consent_service import (NON_ESSENTIAL_PURPOSES, current_consent_notice_version, latest_consent, record_consent,
                             withdraw_consent)
from core import client_ip, invalid, jload, many, new_id, not_found, one
from db import get_db, register_seed
from security import AuthContext, require_auth

router = APIRouter(tags=["consent"])

NOTICE_LOCALES = ("en", "hi")
VISITOR_PURPOSES = ("essential",) + NON_ESSENTIAL_PURPOSES  # cookie / site purposes
WITHDRAWABLE = ("analytics", "marketing", "personalisation", "lead_contact")
USER_EDITABLE = ("analytics", "marketing", "personalisation", "lead_contact")
# Purposes recorded by other journeys (loans, onboarding). A signed-in user may withdraw them; granting
# happens only inside those journeys, where the specific notice is shown.
USER_WITHDRAW_ONLY = ("aa_data_fetch", "bureau_pull", "gstn_itr_fetch")
SUBJECT_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")

PURPOSE_LABELS = {
    "en": {
        "essential": "Essential", "analytics": "Analytics", "marketing": "Marketing", "personalisation": "Personalisation",
        "lead_contact": "Call-backs about products", "aa_data_fetch": "Account Aggregator data fetch",
        "bureau_pull": "Credit bureau check", "gstn_itr_fetch": "GST / ITR data fetch", "onboarding_kyc": "Account opening KYC",
        "aadhaar_ekyc": "Aadhaar eKYC",
    },
    "hi": {
        "essential": "आवश्यक", "analytics": "विश्लेषण", "marketing": "मार्केटिंग", "personalisation": "वैयक्तिकरण",
        "lead_contact": "उत्पादों के बारे में कॉल-बैक", "aa_data_fetch": "अकाउंट एग्रीगेटर डेटा",
        "bureau_pull": "क्रेडिट ब्यूरो जाँच", "gstn_itr_fetch": "GST / ITR डेटा", "onboarding_kyc": "खाता खोलने हेतु KYC",
        "aadhaar_ekyc": "आधार eKYC",
    },
}


class Body(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class ConsentIn(Body):
    subject_id: str
    notice_version_id: str
    language: Optional[str] = "en"
    # {"analytics": true, "marketing": false, "personalisation": false}; "essential" is always granted.
    choices: Optional[dict[str, Any]] = None
    purposes: Optional[dict[str, Any]] = None  # accepted alias of ``choices``


class WithdrawIn(Body):
    purpose: str


class MyConsentsIn(Body):
    choices: Optional[dict[str, Any]] = None
    purposes: Optional[dict[str, Any]] = None  # alias
    language: Optional[str] = None


# ------------------------------------------------------------------ helpers
def _check_subject(subject_id: str, conn: sqlite3.Connection | None = None) -> str:
    """Validate an anonymous visitor id. A signed-in user's id can never be used as a visitor id, so nobody
    can read or change a user's consents through the anonymous endpoints."""
    sid = (subject_id or "").strip()
    if not SUBJECT_RE.match(sid):
        raise invalid("subject_id", "The visitor id must be 8 to 64 letters, digits or hyphens")
    if conn is not None and (
        conn.execute("SELECT 1 FROM users WHERE id = ?", (sid,)).fetchone()
        or conn.execute("SELECT 1 FROM consent_records WHERE subject_id = ? AND subject_type = 'user' LIMIT 1", (sid,)).fetchone()
    ):
        raise invalid("subject_id", "This is not an anonymous visitor id")
    return sid


def _notice_row(conn: sqlite3.Connection, locale: str) -> dict | None:
    return one(
        conn,
        """SELECT i.id AS item_id, i.locale, i.needs_compliance_verification, v.id AS version_id, v.body, v.published_at
           FROM content_items i JOIN content_versions v ON v.id = i.published_version_id
           WHERE i.content_type = 'consent_notice' AND i.locale = ? ORDER BY i.created_at LIMIT 1""",
        (locale,),
    )


def _is_notice_version(conn: sqlite3.Connection, version_id: str) -> bool:
    if not version_id or len(version_id) > 64:
        return False
    row = conn.execute(
        """SELECT 1 FROM content_versions v JOIN content_items i ON i.id = v.content_item_id
           WHERE v.id = ? AND i.content_type = 'consent_notice' AND v.status IN ('published','superseded')""",
        (version_id,),
    ).fetchone()
    return bool(row)


def _bool_choice(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.lower() in ("true", "false", "granted", "denied"):
        return value.lower() in ("true", "granted")
    raise invalid(field, "Each choice must be true or false")


def _state(conn: sqlite3.Connection, subject_id: str, purposes: tuple[str, ...]) -> dict:
    items: dict[str, dict] = {}
    latest_at = None
    notice_version = None
    any_record = False
    for p in purposes:
        rec = latest_consent(conn, subject_id, p)
        if rec:
            any_record = True
            if not latest_at or rec["recorded_at"] > latest_at:
                latest_at = rec["recorded_at"]
                notice_version = rec["notice_version_id"] or notice_version
            if rec.get("withdrawn_at") and (not latest_at or rec["withdrawn_at"] > latest_at):
                latest_at = rec["withdrawn_at"]
        granted = bool(rec and rec["granted"] and not rec["withdrawn_at"])
        items[p] = {
            "purpose": p,
            "granted": granted,
            "recorded_at": rec["recorded_at"] if rec else None,
            "withdrawn_at": rec["withdrawn_at"] if rec else None,
        }
    return {"items": items, "recorded_at": latest_at, "notice_version_id": notice_version, "has_choices": any_record}


# ------------------------------------------------------------------ public notice
@router.get("/public/consent-notice/{locale}")
def consent_notice(locale: str, conn: sqlite3.Connection = Depends(get_db)):
    if locale not in NOTICE_LOCALES:
        raise not_found("Consent notice")
    row = _notice_row(conn, locale)
    if not row:
        raise not_found("Consent notice")
    body = jload(row["body"], {}) or {}
    return {
        "locale": locale,
        "version_id": row["version_id"],
        "published_at": row["published_at"],
        "needs_compliance_verification": bool(row["needs_compliance_verification"]),
        "title": body.get("title"),
        "summary": body.get("summary"),
        "purposes": body.get("purposes", []),
        "rights": body.get("rights", []),
        "grievance_officer": body.get("grievance_officer", {}),
        "retention": body.get("retention"),
        "withdrawal": body.get("withdrawal"),
        "full_notice_href": body.get("full_notice_href", "/pages/privacy-notice"),
        "labels": body.get("labels", {}),
    }


# ------------------------------------------------------------------ visitor consents
@router.post("/consents", status_code=201)
def record_choices(body: ConsentIn, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    subject_id = _check_subject(body.subject_id, conn)
    if not _is_notice_version(conn, body.notice_version_id):
        raise invalid("notice_version_id", "The consent notice version is unknown. Reload the page and try again.")
    language = body.language if body.language in NOTICE_LOCALES else "en"
    raw = body.choices if body.choices is not None else (body.purposes or {})
    if not isinstance(raw, dict):
        raise invalid("choices", "Send your choices as {purpose: true/false}")
    for key in raw:
        if key not in VISITOR_PURPOSES:
            raise invalid("choices", f"Unknown purpose '{key}'. Use analytics, marketing or personalisation.")
    if "essential" in raw and not _bool_choice(raw["essential"], "choices"):
        raise invalid("choices", "Essential cookies keep the site secure and cannot be switched off")
    records = []
    for purpose in VISITOR_PURPOSES:
        granted = True if purpose == "essential" else _bool_choice(raw.get(purpose, False), "choices")
        if not granted:
            withdraw_consent(conn, subject_id, purpose)
        rec = record_consent(conn, subject_type="visitor", subject_id=subject_id, purpose=purpose, granted=granted,
                             notice_version_id=body.notice_version_id, language=language)
        records.append({"id": rec["id"], "purpose": purpose, "granted": granted, "recorded_at": rec["recorded_at"]})
    audit(conn, "consent", "consent.recorded", actor_type="visitor", actor_id=subject_id, entity_type="consent_subject",
          entity_id=subject_id, details={"choices": {r["purpose"]: r["granted"] for r in records},
                                         "notice_version_id": body.notice_version_id, "language": language},
          ip=client_ip(request))
    return {
        "subject_id": subject_id,
        "notice_version_id": body.notice_version_id,
        "recorded_at": records[0]["recorded_at"],
        "records": records,
        "state": {r["purpose"]: r["granted"] for r in records},
    }


@router.get("/consents/{subject_id}")
def get_choices(subject_id: str, conn: sqlite3.Connection = Depends(get_db)):
    sid = _check_subject(subject_id, conn)
    st = _state(conn, sid, VISITOR_PURPOSES + ("lead_contact",))
    state = {p: v["granted"] for p, v in st["items"].items()}
    state["essential"] = True
    return {
        "subject_id": sid,
        "has_choices": st["has_choices"],
        "state": state,
        "purposes": list(st["items"].values()),
        "recorded_at": st["recorded_at"],
        "notice_version_id": st["notice_version_id"],
        "current_notice_version_id": current_consent_notice_version(conn, "en"),
    }


@router.post("/consents/{subject_id}/withdraw")
def withdraw(subject_id: str, body: WithdrawIn, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    sid = _check_subject(subject_id, conn)
    if body.purpose == "essential":
        raise invalid("purpose", "Essential cookies keep the site secure and cannot be withdrawn")
    if body.purpose not in WITHDRAWABLE:
        raise invalid("purpose", "Choose analytics, marketing, personalisation or lead_contact")
    prior = latest_consent(conn, sid, body.purpose)
    changed = withdraw_consent(conn, sid, body.purpose)
    rec = record_consent(conn, subject_type="visitor", subject_id=sid,
                         purpose=body.purpose, granted=False, notice_version_id=(prior or {}).get("notice_version_id"),
                         language=(prior or {}).get("language", "en"))
    audit(conn, "consent", "consent.withdrawn", actor_type="visitor", actor_id=sid, entity_type="consent_subject",
          entity_id=sid, details={"purpose": body.purpose, "grants_withdrawn": changed}, ip=client_ip(request))
    return {"subject_id": sid, "purpose": body.purpose, "granted": False, "withdrawn_at": rec["recorded_at"],
            "effective": "immediately"}


# ------------------------------------------------------------------ signed-in user consents
def _my_payload(conn: sqlite3.Connection, ctx: AuthContext) -> dict:
    uid = str(ctx.user_id)
    lang = (ctx.user.get("preferred_language") or "en") if ctx.user else "en"
    labels = PURPOSE_LABELS["hi" if lang == "hi" else "en"]
    extra = tuple(p for p in USER_WITHDRAW_ONLY if latest_consent(conn, uid, p))
    st = _state(conn, uid, ("essential",) + USER_EDITABLE + extra)
    items = []
    for p, v in st["items"].items():
        items.append({
            **v,
            "label": labels.get(p, p),
            "granted": True if p == "essential" else v["granted"],
            "editable": p in USER_EDITABLE,
            "can_withdraw": p != "essential" and v["granted"],
        })
    history = many(
        conn,
        """SELECT purpose, granted, notice_version_id, language, recorded_at, withdrawn_at FROM consent_records
           WHERE subject_id = ? ORDER BY recorded_at DESC, rowid DESC LIMIT 50""",
        (uid,),
    )
    for h in history:
        h["granted"] = bool(h["granted"])
    return {
        "subject_type": "user",
        "subject_id": uid,
        "purposes": items,
        "state": {i["purpose"]: i["granted"] for i in items},
        "recorded_at": st["recorded_at"],
        "notice_version_id": st["notice_version_id"] or current_consent_notice_version(conn, lang),
        "history": history,
    }


@router.get("/me/consents")
def my_consents(ctx: AuthContext = Depends(require_auth), conn: sqlite3.Connection = Depends(get_db)):
    return _my_payload(conn, ctx)


@router.put("/me/consents")
def update_my_consents(body: MyConsentsIn, request: Request, ctx: AuthContext = Depends(require_auth),
                       conn: sqlite3.Connection = Depends(get_db)):
    raw = body.choices if body.choices is not None else (body.purposes or {})
    if not isinstance(raw, dict) or not raw:
        raise invalid("choices", "Send at least one choice as {purpose: true/false}")
    uid = str(ctx.user_id)
    lang = body.language if body.language in NOTICE_LOCALES else (ctx.user.get("preferred_language") or "en")
    notice_version = current_consent_notice_version(conn, lang if lang in NOTICE_LOCALES else "en")
    changes: dict[str, bool] = {}
    for purpose, value in raw.items():
        if purpose == "essential":
            if not _bool_choice(value, "choices"):
                raise invalid("choices", "Essential processing keeps your account secure and cannot be switched off")
            continue
        granted = _bool_choice(value, "choices")
        if purpose in USER_WITHDRAW_ONLY:
            if granted:
                raise invalid("choices", f"Consent for {purpose} is given inside the journey that needs it")
        elif purpose not in USER_EDITABLE:
            raise invalid("choices", f"Unknown purpose '{purpose}'")
        current = latest_consent(conn, uid, purpose)
        active = bool(current and current["granted"] and not current["withdrawn_at"])
        if active == granted:
            continue
        if not granted:
            # Withdrawal takes effect at once: the grant is marked withdrawn and a refusal row is appended,
            # so personalisation (offers, insights) stops on the very next request.
            withdraw_consent(conn, uid, purpose)
        record_consent(conn, subject_type="user", subject_id=uid, purpose=purpose, granted=granted,
                       notice_version_id=notice_version, language=lang if lang in NOTICE_LOCALES else "en")
        changes[purpose] = granted
    if changes:
        audit_ctx(conn, ctx, "consent", "consent.updated", entity_type="user", entity_id=uid,
                  details={"changes": changes, "notice_version_id": notice_version}, request=request)
    out = _my_payload(conn, ctx)
    out["changed"] = changes
    return out


# ------------------------------------------------------------------ seed: DPDP consent notice (en + hi)
NOTICE_BODIES = {
    "en": {
        "title": "Your privacy choices",
        "summary": "We use essential cookies to keep this site secure and working. With your permission we also use "
                   "analytics, marketing and personalisation. You can change or withdraw your choices at any time from "
                   "\"Privacy choices\"; withdrawal is as easy as giving consent and takes effect immediately.",
        "purposes": [
            {"purpose": "essential", "label": "Essential", "required": True, "default": True,
             "description": "Security, fraud prevention, load balancing and remembering your language and privacy choices. Always on."},
            {"purpose": "analytics", "label": "Analytics", "required": False, "default": False,
             "description": "Aggregated, pseudonymous statistics on how pages are used so we can improve them."},
            {"purpose": "marketing", "label": "Marketing", "required": False, "default": False,
             "description": "Showing you relevant offers for our products on this site and in our communications."},
            {"purpose": "personalisation", "label": "Personalisation", "required": False, "default": False,
             "description": "Tailoring content and offers to your interests based on pages you visit."},
        ],
        "rights": [
            "Access a summary of the personal data we process and the processing activities",
            "Correction, completion, updating and erasure of your personal data",
            "Withdraw consent at any time, as easily as you gave it",
            "Grievance redressal through our Grievance Officer, and to approach the Data Protection Board of India",
            "Nominate a person to exercise your rights in the event of death or incapacity",
        ],
        "grievance_officer": {
            "title": "Grievance Officer (DPDP)",
            "name": "Data Protection Grievance Officer (synthetic)",
            "email": "dpo@example.bank.in",
            "phone": "1800-000-0000",
            "address": "Example Bank, Synthetic Towers, Bandra Kurla Complex, Mumbai 400051",
            "response_time": "We respond within 30 days",
        },
        "retention": "Consent records are kept for as long as the purpose lasts and for the period required by law; "
                     "analytics data is kept pseudonymous for at most 13 months.",
        "withdrawal": "Open \"Privacy choices\" at the bottom-left of any page and switch a purpose off, or choose Reject all.",
        "full_notice_href": "/pages/privacy-notice",
        "labels": {"accept_all": "Accept all", "reject_all": "Reject all", "save": "Save choices",
                   "full_notice": "Read the full privacy notice", "always_on": "Always on"},
    },
    "hi": {
        "title": "आपकी गोपनीयता के विकल्प",
        "summary": "इस साइट को सुरक्षित और चालू रखने के लिए हम आवश्यक कुकीज़ का उपयोग करते हैं। आपकी अनुमति से हम विश्लेषण, "
                   "मार्केटिंग और वैयक्तिकरण का भी उपयोग करते हैं। आप \"गोपनीयता विकल्प\" से कभी भी अपने विकल्प बदल या वापस ले "
                   "सकते हैं; सहमति वापस लेना उतना ही आसान है जितना देना, और यह तुरंत लागू होता है।",
        "purposes": [
            {"purpose": "essential", "label": "आवश्यक", "required": True, "default": True,
             "description": "सुरक्षा, धोखाधड़ी की रोकथाम, लोड बैलेंसिंग और आपकी भाषा व गोपनीयता विकल्प याद रखना। हमेशा चालू।"},
            {"purpose": "analytics", "label": "विश्लेषण", "required": False, "default": False,
             "description": "पेजों के उपयोग के समेकित, छद्मनामी आँकड़े ताकि हम उन्हें बेहतर बना सकें।"},
            {"purpose": "marketing", "label": "मार्केटिंग", "required": False, "default": False,
             "description": "इस साइट पर और हमारे संदेशों में आपको हमारे उत्पादों के प्रासंगिक ऑफ़र दिखाना।"},
            {"purpose": "personalisation", "label": "वैयक्तिकरण", "required": False, "default": False,
             "description": "आपके द्वारा देखे गए पेजों के आधार पर सामग्री और ऑफ़र को आपकी रुचि के अनुसार बनाना।"},
        ],
        "rights": [
            "हम जो व्यक्तिगत डेटा संसाधित करते हैं उसका सारांश और प्रसंस्करण गतिविधियों की जानकारी पाने का अधिकार",
            "अपने व्यक्तिगत डेटा में सुधार, पूर्णता, अद्यतन और मिटाने का अधिकार",
            "किसी भी समय सहमति वापस लेने का अधिकार, उतनी ही आसानी से जितनी आसानी से दी थी",
            "हमारे शिकायत अधिकारी के माध्यम से शिकायत निवारण, और भारतीय डेटा संरक्षण बोर्ड से संपर्क करने का अधिकार",
            "मृत्यु या अक्षमता की स्थिति में अपने अधिकारों के प्रयोग हेतु किसी व्यक्ति को नामित करने का अधिकार",
        ],
        "grievance_officer": {
            "title": "शिकायत अधिकारी (DPDP)",
            "name": "डेटा संरक्षण शिकायत अधिकारी (कृत्रिम)",
            "email": "dpo@example.bank.in",
            "phone": "1800-000-0000",
            "address": "एग्ज़ाम्पल बैंक, सिंथेटिक टावर्स, बांद्रा कुर्ला कॉम्प्लेक्स, मुंबई 400051",
            "response_time": "हम 30 दिनों के भीतर उत्तर देते हैं",
        },
        "retention": "सहमति रिकॉर्ड उद्देश्य पूरा होने तक और कानून द्वारा अपेक्षित अवधि तक रखे जाते हैं; विश्लेषण डेटा छद्मनामी "
                     "रूप में अधिकतम 13 महीने रखा जाता है।",
        "withdrawal": "किसी भी पेज के नीचे बाईं ओर \"गोपनीयता विकल्प\" खोलें और किसी उद्देश्य को बंद करें, या \"सभी अस्वीकार करें\" चुनें।",
        "full_notice_href": "/pages/privacy-notice",
        "labels": {"accept_all": "सभी स्वीकार करें", "reject_all": "सभी अस्वीकार करें", "save": "विकल्प सहेजें",
                   "full_notice": "पूरी गोपनीयता सूचना पढ़ें", "always_on": "हमेशा चालू"},
    },
}


def seed_consent_notice(conn: sqlite3.Connection) -> None:
    group = new_id()
    for locale in NOTICE_LOCALES:
        seed_published(conn, content_type="consent_notice", template="generic", slug="consent-notice", locale=locale,
                       body=NOTICE_BODIES[locale], translation_group_id=group, needs_compliance_verification=True,
                       change_note="DPDP consent notice (synthetic seed, to be verified by compliance)")


register_seed(40, "consent_notice", seed_consent_notice)

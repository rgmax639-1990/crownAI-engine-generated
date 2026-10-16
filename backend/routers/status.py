"""Public service status page and planned-maintenance notices (no authentication, cacheable for 60 s).

Per-service state is derived from the integration adapters' health in the current environment, ongoing
maintenance windows and open incidents. Incident text is never the internal title: only a public-safe
summary derived from the CERT-In category.
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta

from fastapi import APIRouter, Depends, Response

from core import get_reg_value, iso, jload, many, utcnow
from db import get_db
from integrations.base import current_environment
from routers.admin import OPEN_INCIDENT_STATUSES, maintenance_state, notice_lead_hours

router = APIRouter(tags=["status"])

CACHE_HEADER = "public, max-age=60"

# (key, English name, Hindi name, adapters, maintenance service key)
SERVICES = [
    ("website", "Website", "वेबसाइट", [], "website"),
    ("internet_banking", "Internet banking", "इंटरनेट बैंकिंग", ["finacle"], "internet_banking"),
    ("upi", "UPI", "UPI", ["npci_upi"], "upi"),
    ("imps_neft", "IMPS / NEFT", "IMPS / NEFT", ["npci_imps", "neft_rtgs"], None),
    ("cards", "Cards", "कार्ड", ["card_management"], "cards"),
]

STATE_RANK = {"operational": 0, "maintenance": 1, "degraded": 2, "partial_outage": 3, "major_outage": 4}
STATE_LABELS = {
    "operational": ("Operational", "सामान्य रूप से चालू"),
    "maintenance": ("Under maintenance", "रखरखाव जारी"),
    "degraded": ("Degraded performance", "धीमी सेवा"),
    "partial_outage": ("Partial outage", "आंशिक बाधा"),
    "major_outage": ("Major outage", "बड़ी बाधा"),
}

# Public-safe incident summaries by CERT-In category (never the internal title).
PUBLIC_SUMMARY = {
    "dos_ddos": ("Slow or intermittent access to online services", "ऑनलाइन सेवाओं तक धीमी या रुक-रुक कर पहुँच"),
    "identity_theft_phishing": ("Phishing messages impersonating the bank: never share OTP, PIN or passwords",
                                "बैंक के नाम से फ़िशिंग संदेश: OTP, PIN या पासवर्ड कभी साझा न करें"),
    "digital_payments": ("Some digital payments may be delayed", "कुछ डिजिटल भुगतानों में देरी हो सकती है"),
    "website_defacement": ("Parts of the website may be unavailable", "वेबसाइट के कुछ हिस्से उपलब्ध नहीं हो सकते"),
    "fake_mobile_app": ("Fake mobile apps using the bank's name: install only from official app stores",
                        "बैंक के नाम से नकली मोबाइल ऐप: केवल आधिकारिक ऐप स्टोर से इंस्टॉल करें"),
    "malicious_mobile_app": ("Fake mobile apps using the bank's name: install only from official app stores",
                             "बैंक के नाम से नकली मोबाइल ऐप: केवल आधिकारिक ऐप स्टोर से इंस्टॉल करें"),
}
DEFAULT_SUMMARY = ("We are investigating a service issue", "हम एक सेवा समस्या की जाँच कर रहे हैं")

PUBLIC_INCIDENT_STATUS = {
    "detected": "investigating",
    "triaged": "investigating",
    "reported_to_cert_in": "identified",
    "contained": "monitoring",
    "resolved": "resolved",
    "closed": "resolved",
}


def _health_state(health: str | None) -> str:
    h = (health or "healthy").lower()
    if h in ("healthy", "ok", "up", "operational"):
        return "operational"
    if h in ("down", "unhealthy", "outage", "failed", "major_outage"):
        return "major_outage"
    if h == "partial_outage":
        return "partial_outage"
    return "degraded"


def _resolved_at(inc: dict) -> str | None:
    for n in reversed(jload(inc["notes"], [])):
        if isinstance(n, dict) and n.get("to_status") in ("resolved", "closed"):
            return n.get("at")
    return inc["updated_at"] if inc["status"] in ("resolved", "closed") else None


def _public_incident(inc: dict) -> dict:
    en, hi = PUBLIC_SUMMARY.get(inc["cert_in_category"] or "", DEFAULT_SUMMARY)
    status = PUBLIC_INCIDENT_STATUS.get(inc["status"], "investigating")
    return {
        "id": inc["id"],
        "summary_en": en,
        "summary_hi": hi,
        "impact": "major" if inc["severity"] == "critical" else "minor" if inc["severity"] in ("low", "medium") else "significant",
        "status": status,
        "started_at": inc["detected_at"],
        "resolved_at": _resolved_at(inc) if status == "resolved" else None,
    }


def _published_notices(conn: sqlite3.Connection) -> list[dict]:
    """Upcoming and ongoing notices that were published at least the minimum notice period ahead."""
    hours = float(get_reg_value(conn, "maintenance_notice_hours", 48) or 48)
    now = utcnow()
    out = []
    for r in many(conn, "SELECT * FROM maintenance_notices WHERE ends_at > ? ORDER BY starts_at ASC", (iso(now),)):
        if notice_lead_hours(r) < hours:
            continue
        out.append({
            "id": r["id"],
            "service": r["service"],
            "starts_at": r["starts_at"],
            "ends_at": r["ends_at"],
            "message_en": r["message_en"],
            "message_hi": r["message_hi"],
            "state": maintenance_state(r, now),
            "published_at": r["created_at"],
        })
    return out


@router.get("/status")
def service_status(response: Response, conn: sqlite3.Connection = Depends(get_db)):
    response.headers["Cache-Control"] = CACHE_HEADER
    env = current_environment()
    now = utcnow()
    adapters = {r["provider"]: r for r in many(
        conn, "SELECT provider, health_status, last_health_check FROM integration_adapters WHERE environment = ?", (env,))}
    notices = _published_notices(conn)
    open_incidents = many(
        conn, f"SELECT * FROM incidents WHERE status IN ({','.join('?' * len(OPEN_INCIDENT_STATUSES))}) ORDER BY detected_at DESC",
        OPEN_INCIDENT_STATUSES)

    services = []
    for key, name_en, name_hi, providers, maint_key in SERVICES:
        state, since = "operational", None
        checks = [adapters[p] for p in providers if p in adapters]
        for a in checks:
            s = _health_state(a["health_status"])
            if STATE_RANK[s] > STATE_RANK[state]:
                state, since = s, a["last_health_check"]
        if state == "operational" and checks:
            since = max((a["last_health_check"] or "") for a in checks) or None
        ongoing = [n for n in notices if n["service"] == maint_key and n["state"] == "ongoing"] if maint_key else []
        if ongoing and STATE_RANK["maintenance"] > STATE_RANK[state]:
            state, since = "maintenance", ongoing[0]["starts_at"]
        upcoming = [n for n in notices if maint_key and n["service"] == maint_key and n["state"] == "upcoming"]
        services.append({
            "key": key,
            "name_en": name_en,
            "name_hi": name_hi,
            "state": state,
            "label_en": STATE_LABELS[state][0],
            "label_hi": STATE_LABELS[state][1],
            "since": since,
            "next_maintenance": upcoming[0] if upcoming else None,
        })

    worst = max(STATE_RANK[s["state"]] for s in services)
    major = sum(1 for s in services if s["state"] == "major_outage")
    if major >= 3:
        overall = "major_outage"
    elif major or worst >= STATE_RANK["partial_outage"]:
        overall = "partial_outage"
    elif worst >= STATE_RANK["maintenance"]:
        overall = "degraded"
    else:
        overall = "operational"
    if overall == "operational" and any(i["severity"] in ("high", "critical") for i in open_incidents):
        overall = "degraded"

    overall_labels = {
        "operational": ("All services are operating normally", "सभी सेवाएँ सामान्य रूप से चल रही हैं"),
        "degraded": ("Some services are degraded or under maintenance", "कुछ सेवाएँ धीमी हैं या रखरखाव में हैं"),
        "partial_outage": ("Some services are unavailable", "कुछ सेवाएँ उपलब्ध नहीं हैं"),
        "major_outage": ("Major service disruption", "बड़ी सेवा बाधा"),
    }
    since_90 = iso(now - timedelta(days=90))
    history = many(conn, "SELECT * FROM incidents WHERE detected_at >= ? ORDER BY detected_at DESC LIMIT 100", (since_90,))
    return {
        "overall": overall,
        "overall_label_en": overall_labels[overall][0],
        "overall_label_hi": overall_labels[overall][1],
        "services": services,
        "active_incidents": [_public_incident(i) for i in open_incidents],
        "incident_history": [_public_incident(i) for i in history],
        "history_days": 90,
        "maintenance": notices,
        "environment": env,
        "generated_at": iso(now),
    }


@router.get("/status/maintenance-notices")
def public_notices(response: Response, conn: sqlite3.Connection = Depends(get_db)):
    response.headers["Cache-Control"] = CACHE_HEADER
    hours = float(get_reg_value(conn, "maintenance_notice_hours", 48) or 48)
    return {"items": _published_notices(conn), "minimum_notice_hours": hours, "generated_at": iso(utcnow())}


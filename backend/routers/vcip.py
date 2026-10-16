"""V-CIP officer console (US-17): queue, accept a waiting customer, and record the decision.

The officer sees masked identifiers only, the eKYC name against the PAN name, a mock
face-match score, the geo-location check and the random code the customer reads aloud.
Approval needs a passed liveness check and a location inside India. Each decision stores
a (mock) recording object, is audited as ``vcip.decision`` and is sent to the applicant
by SMS.
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

import aadhaar_vault
from audit import audit
from consent_service import record_consent
from core import (api_error, client_ip, get_or_404, invalid, iso, jdump, many, mask_aadhaar_last4, mask_email, mask_mobile,
                  mask_pan, new_id, now_iso, one, sample_aadhaar, utcnow)
from crypto import encrypt_str
from db import get_db, register_seed
from notify import notify_contact
from objectstore import put_bytes
from routers.onboarding import (MEDIA_SERVER, PRODUCTS, details, face_match_score, inside_india, queue_position,
                                save_details)
from security import AuthContext, require_staff

router = APIRouter(tags=["vcip"])

officer_only = require_staff("vcip_officer")

DECISIONS = ("approved", "rejected", "reschedule")
QUESTION_PROMPTS = [
    "What is your date of birth?",
    "Please read the 4-digit code shown on your screen.",
    "Please show the front of your original PAN card to the camera.",
    "What is your current address, including PIN code?",
    "Please turn your head slowly to the left and then to the right.",
]


class Body(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class DecisionIn(Body):
    decision: str
    liveness_passed: Optional[bool] = None
    remarks: Optional[str] = None


def _officer_view(conn: sqlite3.Connection, s: dict, ctx: AuthContext) -> dict:
    app = one(conn, "SELECT * FROM onboarding_applications WHERE id = ?", (s["application_id"],)) or {}
    det = details(conn, s["application_id"])
    pan_name = det.get("pan_name") or app.get("full_name")
    ekyc_name = det.get("ekyc_name")
    name_match = det.get("name_match")
    geo_ok = inside_india(s["geo_lat"], s["geo_lng"])
    has_geo = s["geo_lat"] is not None and s["geo_lng"] is not None
    return {
        "session_id": s["id"],
        "application_id": s["application_id"],
        "status": s["status"],
        "decision": s["decision"],
        "scheduled_at": s["scheduled_at"],
        "joined_at": s["joined_at"],
        "created_at": s["created_at"],
        "queue_position": queue_position(conn, s),
        "random_code": s["random_code"],
        "officer_id": s["officer_id"],
        "assigned_to_me": bool(s["officer_id"]) and s["officer_id"] == ctx.user_id,
        "applicant": {
            "full_name": app.get("full_name"),
            "date_of_birth": app.get("date_of_birth"),
            "mobile_masked": mask_mobile(app.get("mobile")),
            "email_masked": mask_email(app.get("email")),
            "pan_masked": app.get("pan_masked"),
            "aadhaar_masked": mask_aadhaar_last4(app.get("aadhaar_last4")) or None,
            "aadhaar_in_vault": aadhaar_vault.exists(conn, app.get("aadhaar_vault_ref")),
            "product_code": app.get("product_code"),
            "product_label": PRODUCTS.get(app.get("product_code") or "", {}).get("label", app.get("product_code")),
            "ekyc_method": app.get("ekyc_method"),
            "ekyc_address": app.get("ekyc_address"),
            "application_status": app.get("status"),
        },
        "pan_name": pan_name,
        "ekyc_name": ekyc_name,
        "name_match": None if name_match is None else bool(name_match),
        "dob_match": None if det.get("dob_match") is None else bool(det.get("dob_match")),
        "ekyc_dob": det.get("ekyc_dob"),
        "ekyc_gender": det.get("ekyc_gender"),
        "ekyc_photo_ref": det.get("ekyc_photo_ref"),
        "flags": det.get("flags") or [],
        "face_match_score": s["face_match_score"] if s["face_match_score"] is not None else face_match_score(s["id"]),
        "face_match_mock": True,
        "geo": {
            "lat": s["geo_lat"],
            "lng": s["geo_lng"],
            "inside_india": geo_ok,
            "result": "inside_india" if geo_ok else ("outside_india" if has_geo else "unknown"),
        },
        "liveness_passed": None if s["liveness_passed"] is None else bool(s["liveness_passed"]),
        "recording": {
            "consented": bool(det.get("recording_consent_at")),
            "consented_at": det.get("recording_consent_at"),
            "state": "recording" if s["status"] == "in_call" else ("stored" if s["recording_storage_key"] else "not_started"),
            "storage_region": "India (mock object store)",
        },
        "question_prompts": QUESTION_PROMPTS,
        "media_server": MEDIA_SERVER,
        "mock": True,
    }


@router.get("/staff/vcip/queue")
def vcip_queue(ctx: AuthContext = Depends(officer_only), conn: sqlite3.Connection = Depends(get_db)):
    rows = many(
        conn,
        """SELECT * FROM vcip_sessions WHERE decision = 'pending' AND status IN ('in_call', 'waiting', 'scheduled')
           ORDER BY CASE status WHEN 'in_call' THEN 0 WHEN 'waiting' THEN 1 ELSE 2 END,
                    COALESCE(joined_at, scheduled_at, created_at)""",
    )
    items = [_officer_view(conn, s, ctx) for s in rows]
    counts = {k: sum(1 for s in rows if s["status"] == k) for k in ("waiting", "scheduled", "in_call")}
    return {"items": items, "counts": counts, "total": len(items), "server_time": now_iso()}


@router.post("/staff/vcip/{session_id}/accept")
def vcip_accept(session_id: str, request: Request, ctx: AuthContext = Depends(officer_only),
                conn: sqlite3.Connection = Depends(get_db)):
    s = get_or_404(conn, "vcip_sessions", session_id, "V-CIP session")
    if s["decision"] != "pending":
        raise api_error(409, "already_decided", "A decision has already been recorded for this session.")
    if s["status"] == "in_call":
        if s["officer_id"] == ctx.user_id:
            return {"session": _officer_view(conn, s, ctx)}
        raise api_error(409, "taken_by_other_officer", "Another officer is already handling this session.")
    if s["status"] == "scheduled":
        raise api_error(409, "customer_not_joined", "The customer has not joined yet. Wait until they are in the waiting room.")
    if s["status"] != "waiting":
        raise api_error(409, "not_waiting", "This session is not waiting for an officer.")
    conn.execute("UPDATE vcip_sessions SET status = 'in_call', officer_id = ? WHERE id = ?", (ctx.user_id, session_id))
    conn.execute("UPDATE onboarding_applications SET status = 'vcip_in_call', updated_at = ? WHERE id = ?",
                 (now_iso(), s["application_id"]))
    audit(conn, "admin", "vcip.accepted", actor_type="staff", actor_id=ctx.user_id, entity_type="vcip_session",
          entity_id=session_id, details={"application_id": s["application_id"]}, ip=client_ip(request))
    fresh = one(conn, "SELECT * FROM vcip_sessions WHERE id = ?", (session_id,))
    return {"session": _officer_view(conn, fresh, ctx)}  # type: ignore[arg-type]


@router.post("/staff/vcip/{session_id}/decision")
def vcip_decision(session_id: str, body: DecisionIn, request: Request, ctx: AuthContext = Depends(officer_only),
                  conn: sqlite3.Connection = Depends(get_db)):
    s = get_or_404(conn, "vcip_sessions", session_id, "V-CIP session")
    decision = (body.decision or "").strip().lower()
    if decision not in DECISIONS:
        raise invalid("decision", "Choose approved, rejected or reschedule")
    if s["decision"] != "pending":
        raise api_error(409, "already_decided", "A decision has already been recorded for this session.")
    remarks = (body.remarks or "").strip()
    if len(remarks) > 1000:
        raise invalid("remarks", "Keep remarks under 1000 characters")
    if decision in ("rejected", "reschedule") and len(remarks) < 3:
        raise invalid("remarks", "Remarks are required when rejecting or rescheduling")
    if decision == "approved":
        if s["status"] not in ("waiting", "in_call"):
            raise api_error(409, "customer_not_joined", "The customer must be on the video call to approve.")
        if body.liveness_passed is not True:
            raise invalid("liveness_passed", "Confirm that the liveness check passed before approving", code="liveness_required")
        if not inside_india(s["geo_lat"], s["geo_lng"]):
            raise invalid("geo", "The customer's location is not inside India, so the video KYC cannot be approved",
                          code="outside_india")

    app = one(conn, "SELECT * FROM onboarding_applications WHERE id = ?", (s["application_id"],))
    now = now_iso()
    payload = (
        "MOCK V-CIP RECORDING PLACEHOLDER\n"
        f"session={session_id}\napplication={s['application_id']}\nofficer={ctx.user_id}\n"
        f"decision={decision}\nrecorded_at={now}\n"
        "No real audio/video is captured in the mock environment.\n"
    ).encode("utf-8")
    storage_key, _digest = put_bytes(payload, "vcip/recordings")
    liveness = None if body.liveness_passed is None else int(bool(body.liveness_passed))
    conn.execute(
        """UPDATE vcip_sessions SET decision = ?, remarks = ?, decided_at = ?, officer_id = ?, liveness_passed = ?,
           recording_storage_key = ?, status = 'completed',
           face_match_score = COALESCE(face_match_score, ?) WHERE id = ?""",
        (decision, remarks or None, now, ctx.user_id, liveness, storage_key, face_match_score(session_id), session_id),
    )
    new_status = {"approved": "vcip_approved", "rejected": "vcip_rejected", "reschedule": "ekyc_completed"}[decision]
    if app:
        conn.execute("UPDATE onboarding_applications SET status = ?, updated_at = ? WHERE id = ?", (new_status, now, app["id"]))
    audit(conn, "admin", "vcip.decision", actor_type="staff", actor_id=ctx.user_id, entity_type="vcip_session",
          entity_id=session_id,
          details={"application_id": s["application_id"], "decision": decision, "liveness_passed": body.liveness_passed,
                   "geo_inside_india": inside_india(s["geo_lat"], s["geo_lng"]), "remarks": remarks[:200] or None},
          ip=client_ip(request))
    if app:
        title, text = {
            "approved": ("Video KYC approved",
                         "Your video KYC is approved. Return to your application to finish opening your account."),
            "rejected": ("Video KYC not approved",
                         f"Your video KYC could not be approved. Reason: {remarks}. Visit a branch or call us for help."),
            "reschedule": ("Video KYC to be rescheduled",
                           f"Your video KYC needs a new slot. Note from the officer: {remarks}. Open your application to book again."),
        }[decision]
        notify_contact(conn, app["mobile"], None, title, text)
    fresh = one(conn, "SELECT * FROM vcip_sessions WHERE id = ?", (session_id,))
    return {
        "session": _officer_view(conn, fresh, ctx),  # type: ignore[arg-type]
        "decision": decision,
        "application_status": new_status,
        "recording_storage_key": storage_key,
        "officer_id": ctx.user_id,
        "decided_at": now,
    }


# ------------------------------------------------------------------ synthetic seed: two customers waiting
def seed_vcip(conn: sqlite3.Connection) -> None:
    samples = [
        # name on PAN, name on eKYC, dob, mobile, email, PAN, lat, lng, product, address
        ("Aarav Kumar", "Aarav Kumar", "1992-03-14", "+919812300001", "aarav.synthetic@example.com", "ABKPK2468L",
         19.0760, 72.8777, "savings_premium", "12, Synthetic Lane, Andheri West, Mumbai, Maharashtra 400053"),
        ("Diya Iyer", "Diya Lakshmi Iyer", "1996-08-22", "+919812300002", "diya.sample@example.com", "CDIPI1357M",
         12.9716, 77.5946, "savings_classic", "45, Sample Nagar, Koramangala, Bengaluru, Karnataka 560034"),
    ]
    for i, (pan_name, ekyc_name, dob, mobile, email, pan, lat, lng, product, address) in enumerate(samples):
        app_id = new_id()
        sid = new_id()
        ref, last4 = aadhaar_vault.store(conn, sample_aadhaar())
        created = iso(utcnow() - timedelta(minutes=40 - i * 10))
        joined = iso(utcnow() - timedelta(minutes=6 - i * 3))
        consents = [
            record_consent(conn, subject_type="visitor", subject_id=app_id, purpose=p, granted=True)["id"]
            for p in ("onboarding_kyc", "aadhaar_ekyc")
        ]
        conn.execute(
            """INSERT INTO onboarding_applications (id, product_code, mobile, email, full_name, date_of_birth, pan_masked,
               pan_verified, ekyc_method, ekyc_completed, ekyc_txn_id, aadhaar_vault_ref, aadhaar_last4, ekyc_address,
               vcip_session_id, consent_record_ids, language, status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,1,'aadhaar_otp',1,?,?,?,?,?,?,'en','vcip_waiting',?,?)""",
            (app_id, product, mobile, email, pan_name, dob, mask_pan(pan), f"UIDAI-MOCK-SEED{i}", ref, last4, address, sid,
             jdump(consents), created, joined),
        )
        match = pan_name.upper() == ekyc_name.upper()
        save_details(conn, app_id, pan_enc=encrypt_str(pan), pan_name=pan_name, ekyc_name=ekyc_name, ekyc_dob=dob,
                     ekyc_gender="M" if i == 0 else "F", ekyc_photo_ref=f"mock-photo://seed-{i}", name_match=int(match),
                     dob_match=1, flags=jdump([] if match else ["name_mismatch"]),
                     mock_providers=jdump(["sms_gateway", "nsdl_pan", "uidai"]), recording_consent_at=joined)
        conn.execute(
            """INSERT INTO vcip_sessions (id, application_id, joined_at, geo_lat, geo_lng, face_match_score, random_code,
               status, decision, created_at) VALUES (?,?,?,?,?,?,?, 'waiting', 'pending', ?)""",
            (sid, app_id, joined, lat, lng, face_match_score(sid), f"{4821 + i * 1373:04d}", joined),
        )


register_seed(50, "vcip_queue", seed_vcip)

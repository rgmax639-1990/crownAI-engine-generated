"""Environment banner (mock/live providers) and file uploads."""
from __future__ import annotations

import os
import sqlite3

from fastapi import APIRouter, Depends, File, UploadFile

from core import MOCK_OTP_CODE, api_error, new_id, now_iso
from db import get_db
from integrations.base import PROVIDERS, current_environment
from objectstore import put_bytes
from security import AuthContext, require_auth

router = APIRouter(tags=["system"])

DEMO_MODE = os.getenv("DEMO_MODE", "true").lower() == "true"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_UPLOAD_TYPES = {
    "application/pdf", "image/png", "image/jpeg", "text/csv", "text/plain", "application/json",
    "application/vnd.ms-excel", "application/octet-stream",
}

DEMO_ACCOUNTS = [
    {"role": "Retail customer", "email": "customer@example.bank.in"},
    {"role": "NRI / wealth customer", "email": "nri@example.bank.in"},
    {"role": "Corporate maker", "email": "corp.maker@example.bank.in"},
    {"role": "Corporate checker", "email": "corp.checker@example.bank.in"},
    {"role": "Corporate authoriser", "email": "corp.authoriser@example.bank.in"},
    {"role": "Corporate admin", "email": "corp.admin@example.bank.in"},
    {"role": "Content maker", "email": "maker@example.bank.in"},
    {"role": "Content checker", "email": "checker@example.bank.in"},
    {"role": "Legal approver", "email": "legal@example.bank.in"},
    {"role": "Grievance officer", "email": "grievance@example.bank.in"},
    {"role": "Nodal officer", "email": "nodal@example.bank.in"},
    {"role": "V-CIP officer", "email": "vcip@example.bank.in"},
    {"role": "Operations admin", "email": "ops@example.bank.in"},
    {"role": "SOC analyst", "email": "soc@example.bank.in"},
    {"role": "Integration approver", "email": "approver@example.bank.in"},
]


@router.get("/public/environment")
def environment(conn: sqlite3.Connection = Depends(get_db)):
    env = current_environment()
    rows = {r["provider"]: dict(r) for r in conn.execute(
        "SELECT provider, mode, last_health_check, health_status FROM integration_adapters WHERE environment = ?", (env,))}
    providers = []
    for key, meta in PROVIDERS.items():
        mode = rows.get(key, {}).get("mode", "mock")
        providers.append({"provider": key, "label": meta["label"], "mode": mode,
                          "badge": "LIVE" if mode == "live" else "MOCK"})
    mock_count = sum(1 for p in providers if p["badge"] == "MOCK")
    sms_mock = next((p for p in providers if p["provider"] == "sms_gateway"), {}).get("badge") == "MOCK"
    out = {
        "environment": env,
        "all_mock": mock_count == len(providers),
        "mock_count": mock_count,
        "live_count": len(providers) - mock_count,
        "providers": providers,
        "synthetic_data_only": env != "production",
        "notice": "Integrations run on MOCK providers with synthetic data. No real money moves and no real institution is contacted."
        if mock_count else None,
        "mock_otp_hint": MOCK_OTP_CODE if sms_mock else None,
    }
    if DEMO_MODE and env != "production":
        out["demo_accounts"] = DEMO_ACCOUNTS
        out["demo_password"] = os.getenv("SEED_PASSWORD", "Demo@12345")
    return out


@router.post("/uploads", status_code=201)
async def upload(file: UploadFile = File(...), ctx: AuthContext = Depends(require_auth), conn: sqlite3.Connection = Depends(get_db)):
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise api_error(422, "empty_file", "The file is empty", field="file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise api_error(413, "file_too_large", "Files must be 10 MB or smaller", field="file")
    ctype = (file.content_type or "application/octet-stream").split(";")[0].strip()
    if ctype not in ALLOWED_UPLOAD_TYPES:
        raise api_error(415, "unsupported_type", "Upload a PDF, PNG, JPEG, CSV, TXT or JSON file", field="file")
    key, digest = put_bytes(data, "uploads")
    uid = new_id()
    name = (file.filename or "upload").replace("/", "_").replace("\\", "_")[:120]
    conn.execute(
        """INSERT INTO uploads (id, owner_user_id, storage_key, file_name, content_type, size_bytes, sha256, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (uid, ctx.user_id, key, name, ctype, len(data), digest, now_iso()),
    )
    return {"upload_ref": uid, "file_name": name, "content_type": ctype, "size_bytes": len(data), "sha256": digest}

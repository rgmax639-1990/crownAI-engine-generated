"""UIDAI AUA/KUA adapter (Aadhaar OTP eKYC).

The mock never sees an Aadhaar number: it is called with the Aadhaar Data Vault
reference key. In live mode the vault supplies the number to the certified ASA call.
Mock OTP: ``MOCK_OTP_CODE`` (123456). The eKYC payload is synthetic.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3

from core import MOCK_OTP_CODE

from .base import mock_meta, resolve

SYNTHETIC_ADDRESSES = [
    "12, Synthetic Lane, Andheri West, Mumbai, Maharashtra 400053",
    "45, Sample Nagar, Koramangala, Bengaluru, Karnataka 560034",
    "7, Demo Colony, Salt Lake, Kolkata, West Bengal 700091",
    "221, Test Enclave, Dwarka Sector 10, New Delhi, Delhi 110075",
    "9, Mock Street, Anna Nagar, Chennai, Tamil Nadu 600040",
]


def synthetic_ekyc(seed: str, name_hint: str | None, dob_hint: str | None) -> dict:
    h = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return {
        "name": name_hint or "Synthetic Applicant",
        "dob": dob_hint or "1990-01-01",
        "gender": "F" if int(h[0], 16) % 2 == 0 else "M",
        "address": SYNTHETIC_ADDRESSES[int(h[1:3], 16) % len(SYNTHETIC_ADDRESSES)],
        "photo_ref": "mock-photo://" + h[:24],
    }


class MockUidai:
    is_mock = True

    def request_otp(self, aadhaar_ref: str, last4: str | None = None) -> dict:
        return {
            "txn_id": "UIDAI-MOCK-" + secrets.token_hex(8).upper(),
            "otp_sent_to": "mobile number linked with Aadhaar" + (f" XXXX XXXX {last4}" if last4 else ""),
            "mock_otp": MOCK_OTP_CODE,
            "mock": mock_meta("uidai"),
        }

    def verify_otp(self, txn_id: str, otp: str, *, name_hint: str | None = None, dob_hint: str | None = None) -> dict:
        meta = mock_meta("uidai")
        if not txn_id or not hmac.compare_digest(str(otp or "").strip(), MOCK_OTP_CODE):
            return {"ok": False, "error": "otp_invalid", "message": "The Aadhaar OTP is incorrect or has expired", "mock": meta}
        return {"ok": True, "txn_id": txn_id, "ekyc": synthetic_ekyc(txn_id, name_hint, dob_hint), "mock": meta}


def get(conn: sqlite3.Connection):
    return resolve(conn, "uidai", MockUidai)

"""DigiLocker adapter: exchanges the OAuth authorisation code for the issued Aadhaar eKYC.

Mock: the code ``invalid`` (or an empty code) is rejected; any other code returns a
synthetic eKYC with the last four Aadhaar digits only.
"""
from __future__ import annotations

import hashlib
import sqlite3

from .base import mock_meta, resolve
from .uidai import synthetic_ekyc


class MockDigilocker:
    is_mock = True

    def exchange(self, auth_code: str, *, name_hint: str | None = None, dob_hint: str | None = None) -> dict:
        meta = mock_meta("digilocker")
        code = (auth_code or "").strip()
        if not code or code.lower() == "invalid":
            return {"ok": False, "error": "invalid_auth_code", "message": "DigiLocker did not accept the authorisation. Try again.",
                    "mock": meta}
        h = hashlib.sha256(code.encode("utf-8")).hexdigest()
        ekyc = synthetic_ekyc(code, name_hint, dob_hint)
        ekyc["aadhaar_last4"] = f"{int(h[:6], 16) % 10000:04d}"
        ekyc["document"] = "Aadhaar (issued document via DigiLocker)"
        return {"ok": True, "digilocker_txn": "DL-MOCK-" + h[:12].upper(), "ekyc": ekyc, "mock": meta}


def get(conn: sqlite3.Connection):
    return resolve(conn, "digilocker", MockDigilocker)

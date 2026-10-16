"""CKYC Registry (CERSAI) adapter: search by PAN, upload a new KYC record.

Mock: a PAN whose numeric part is even is "found" with a deterministic 14-digit CKYC
number; otherwise "not_found", and an upload returns a new CKYC number.
"""
from __future__ import annotations

import hashlib
import sqlite3

from .base import mock_meta, resolve


def _ckyc_number(seed: str) -> str:
    h = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return "3" + str(int(h[:16], 16))[-13:].rjust(13, "0")


class MockCkycr:
    is_mock = True

    def search(self, pan: str) -> dict:
        pan = (pan or "").strip().upper()
        meta = mock_meta("ckycr")
        digits = pan[5:9]
        if len(pan) == 10 and digits.isdigit() and int(digits) % 2 == 0:
            return {"result": "found", "found": True, "ckyc_number": _ckyc_number("search:" + pan), "mock": meta}
        return {"result": "not_found", "found": False, "ckyc_number": None, "mock": meta}

    def upload(self, *, pan: str, full_name: str, date_of_birth: str | None, address: str | None,
               photo_ref: str | None = None) -> dict:
        return {"uploaded": True, "ckyc_number": _ckyc_number("upload:" + (pan or "") + (full_name or "")),
                "mock": mock_meta("ckycr")}


def get(conn: sqlite3.Connection):
    return resolve(conn, "ckycr", MockCkycr)

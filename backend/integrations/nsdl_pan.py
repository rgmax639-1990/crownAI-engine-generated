"""NSDL / Protean PAN verification adapter.

Mock rules (synthetic test data):
  * a PAN starting with ``XXXXX`` or ending with ``0000X`` does not exist (status ``N``);
  * the 4th character gives the holder category (``P`` = individual);
  * every other well-formed PAN is "E" (existing and valid) and the name/DOB match.
"""
from __future__ import annotations

import sqlite3

from core import PAN_RE

from .base import mock_meta, resolve

CATEGORIES = {
    "P": "individual", "C": "company", "H": "huf", "F": "firm", "A": "association_of_persons", "T": "trust",
    "B": "body_of_individuals", "L": "local_authority", "J": "artificial_juridical_person", "G": "government",
}


class MockPan:
    is_mock = True

    def verify(self, pan: str, name: str | None = None, dob: str | None = None) -> dict:
        pan = (pan or "").strip().upper()
        meta = mock_meta("nsdl_pan")
        if not PAN_RE.match(pan) or pan.startswith("XXXXX") or pan.endswith("0000X"):
            return {"valid": False, "status": "N", "status_text": "PAN not found in the Income Tax Department database",
                    "category": None, "individual": False, "name_match": False, "dob_match": False, "mock": meta}
        category = CATEGORIES.get(pan[3], "other")
        return {
            "valid": True,
            "status": "E",
            "status_text": "Existing and valid",
            "category": category,
            "individual": pan[3] == "P",
            "name_match": bool(name),
            "dob_match": bool(dob),
            "aadhaar_seeding_status": "Y",
            "mock": meta,
        }


def get(conn: sqlite3.Connection):
    return resolve(conn, "nsdl_pan", MockPan)

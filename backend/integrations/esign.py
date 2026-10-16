"""Aadhaar eSign (ESP) adapter. The mock completes the signature without contacting UIDAI or an ESP."""
from __future__ import annotations

import sqlite3

from core import new_id, now_iso, sha256_hex

from .base import mock_meta, resolve


class MockEsign:
    is_mock = True

    def start(self, document: dict, return_path: str = "/portal/loans") -> dict:
        """``document``: {document_id, sha256, signer_name}. Returns the ESP reference and redirect URL."""
        ref = "MOCK-ESIGN-" + new_id().replace("-", "")[:12].upper()
        sep = "&" if "?" in return_path else "?"
        return {
            "esign_ref": ref,
            "status": "initiated",
            "document_id": document.get("document_id"),
            "document_sha256": document.get("sha256"),
            "redirect_url": f"{return_path}{sep}esign_ref={ref}&esign_status=signed",
            "initiated_at": now_iso(),
            "mock": mock_meta("esign"),
        }

    def complete(self, esign_ref: str, document_sha256: str | None = None) -> dict:
        return {
            "esign_ref": esign_ref,
            "status": "signed",
            "signed_at": now_iso(),
            "signature_type": "Aadhaar eSign (OTP) - MOCK",
            "certificate_issuer": "MOCK ESP CA (not a real certificate)",
            "signature_hash": sha256_hex(f"{esign_ref}:{document_sha256 or ''}"),
            "mock": mock_meta("esign"),
        }


def get(conn: sqlite3.Connection):
    return resolve(conn, "esign", MockEsign)

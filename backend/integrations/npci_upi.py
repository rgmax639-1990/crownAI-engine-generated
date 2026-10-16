"""NPCI UPI adapter: pay to a VPA, status enquiry and VPA (payee name) lookup.

Mock behaviour mirrors the IMPS mock (see ``npci_imps``): settles immediately with a
12-digit RRN; remarks containing FAIL / TIMEOUT / RETURN trigger a rejection, a timeout
after acceptance, or a later return.
"""
from __future__ import annotations

import sqlite3

from core import VPA_RE

from .base import mock_meta, resolve
from .npci_imps import MockRail, RailTimeout, new_rrn, synthetic_name

KNOWN_HANDLES = {"oksynth", "okexmb", "exmb", "upi", "ybl", "paytm", "okaxis", "okicici", "oksbi", "okhdfcbank", "ibl", "axl"}


class MockUpi(MockRail):
    provider = "npci_upi"

    def initiate(self, *, payment_id: str, amount_paise: int, remarks: str | None = None, payee: str | None = None,
                 rail: str = "UPI") -> dict:
        """Collect-free push payment to ``payee`` (a VPA). Returns {accepted, rail_reference (RRN), status, reason}."""
        return self._initiate("UPI", payment_id=payment_id, amount_paise=amount_paise, remarks=remarks, payee=payee)

    def status(self, rail_reference: str | None = None, *, payment_id: str | None = None) -> dict:
        return self._status("UPI", rail_reference, payment_id)

    def vpa_lookup(self, vpa: str) -> dict:
        """Validate a VPA and return the registered payee name."""
        vpa = (vpa or "").strip()
        if not VPA_RE.match(vpa):
            return {"valid": False, "reason": "invalid_vpa", "message": "UPI ID is not valid", "name_at_bank": None,
                    "mock": mock_meta(self.provider)}
        handle = vpa.split("@", 1)[1].lower()
        if handle not in KNOWN_HANDLES and not handle.startswith("ok"):
            return {"valid": False, "reason": "unknown_handle", "message": f"No UPI app uses the handle @{handle}",
                    "name_at_bank": None, "mock": mock_meta(self.provider)}
        return {"valid": True, "vpa": vpa.lower(), "name_at_bank": synthetic_name("vpa:" + vpa.lower()).upper(),
                "reference": new_rrn(), "mock": mock_meta(self.provider)}


def get(conn: sqlite3.Connection):
    return resolve(conn, "npci_upi", lambda: MockUpi(conn))


__all__ = ["MockUpi", "RailTimeout", "get"]

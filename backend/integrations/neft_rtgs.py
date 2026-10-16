"""RBI NEFT / RTGS adapter (through SFMS).

NEFT settles in half-hourly batches and RTGS in real time, both 24x7. The mock therefore
answers "pending" on initiation; a status enquiry after ``MOCK_NEFT_SETTLE_SECONDS``
(default 60) / ``MOCK_RTGS_SETTLE_SECONDS`` (default 20) reports the final status.
UTRs: NEFT ``EXMBN`` + YYMMDD + 6-digit sequence, RTGS ``EXMBR`` + YYYYMMDD + 9 digits.
Remarks hooks FAIL / TIMEOUT / RETURN behave as in the IMPS mock.
"""
from __future__ import annotations

import os
import secrets
import sqlite3

from core import IST, utcnow

from .base import resolve
from .npci_imps import MockRail, RailTimeout

BANK_UTR_PREFIX = "EXMB"


def new_utr(rail: str) -> str:
    today = utcnow().astimezone(IST)
    if rail == "RTGS":
        return f"{BANK_UTR_PREFIX}R{today.strftime('%Y%m%d')}" + "".join(secrets.choice("0123456789") for _ in range(9))
    return f"{BANK_UTR_PREFIX}N{today.strftime('%y%m%d')}" + "".join(secrets.choice("0123456789") for _ in range(6))


class MockNeftRtgs(MockRail):
    provider = "neft_rtgs"

    def _reference(self, rail: str) -> str:
        return new_utr(rail)

    def _settle_seconds(self, rail: str) -> int:
        if rail == "RTGS":
            return int(os.getenv("MOCK_RTGS_SETTLE_SECONDS", "20"))
        return int(os.getenv("MOCK_NEFT_SETTLE_SECONDS", "60"))

    def initiate(self, *, payment_id: str, amount_paise: int, remarks: str | None = None, payee: str | None = None,
                 rail: str = "NEFT") -> dict:
        """Send a NEFT/RTGS credit. Returns {accepted, rail_reference (UTR), status "pending" | "failed", reason}."""
        rail = "RTGS" if rail == "RTGS" else "NEFT"
        return self._initiate(rail, payment_id=payment_id, amount_paise=amount_paise, remarks=remarks, payee=payee)

    def status(self, rail_reference: str | None = None, *, payment_id: str | None = None) -> dict:
        return self._status(None, rail_reference, payment_id)


def get(conn: sqlite3.Connection):
    return resolve(conn, "neft_rtgs", lambda: MockNeftRtgs(conn))


__all__ = ["MockNeftRtgs", "RailTimeout", "get", "new_utr"]

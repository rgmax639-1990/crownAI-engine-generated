"""NPCI e-NACH (API / e-mandate) adapter: registers a debit mandate and returns the UMRN."""
from __future__ import annotations

import secrets
import sqlite3

from core import IFSC_RE, mask_account, now_iso

from .base import mock_meta, resolve

AUTH_MODES = ("netbanking", "debit_card", "aadhaar")


class NachRejected(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class MockNpciNach:
    is_mock = True

    def register_mandate(self, *, account_number: str, ifsc: str, auth_mode: str, max_amount_paise: int,
                         frequency: str = "monthly", start_date: str | None = None, end_date: str | None = None,
                         debtor_name: str | None = None) -> dict:
        if auth_mode not in AUTH_MODES:
            raise NachRejected("invalid_auth_mode", "Authorise with net banking, debit card or Aadhaar")
        if not IFSC_RE.match((ifsc or "").upper()):
            raise NachRejected("invalid_ifsc", "Destination bank IFSC is invalid")
        if max_amount_paise <= 0:
            raise NachRejected("invalid_amount", "Mandate amount must be positive")
        # UMRN: 20 characters (sponsor bank code + utility + sequence). Synthetic.
        umrn = "EXMB7" + "".join(secrets.choice("0123456789") for _ in range(15))
        return {
            "umrn": umrn,
            "status": "active",
            "auth_mode": auth_mode,
            "account_masked": mask_account(account_number),
            "ifsc": ifsc.upper(),
            "max_amount_paise": max_amount_paise,
            "frequency": frequency,
            "start_date": start_date,
            "end_date": end_date,
            "registered_at": now_iso(),
            "mock": mock_meta("npci_nach"),
        }


def get(conn: sqlite3.Connection):
    return resolve(conn, "npci_nach", MockNpciNach)

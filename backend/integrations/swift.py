"""SWIFT gpi adapter: outward customer credit transfers (pacs.008 / MT103) and gpi tracking.

The mock returns a UETR (UUID v4) and a tracker timeline that advances with the time
elapsed since submission: sent -> intermediary -> beneficiary bank -> credited.
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import timedelta

from core import iso, now_iso, parse_iso, utcnow

from .base import mock_meta, resolve

OUR_BIC = "EXMBINBBXXX"
CORRESPONDENTS = {
    "USD": "SYNTUS33XXX", "GBP": "SYNTGB2LXXX", "EUR": "SYNTDEFFXXX", "AED": "SYNTAEADXXX",
    "SGD": "SYNTSGSGXXX", "AUD": "SYNTAU2SXXX", "CAD": "SYNTCATTXXX",
}

# (minutes after submission, status, reason, description, agent)
_TIMELINE = [
    (0, "ACSP", "G000", "Payment sent by {our}", "our"),
    (2, "ACSP", "G000", "Processed by correspondent bank {corr}", "corr"),
    (6, "ACSP", "G002", "Received by beneficiary bank {ben}; credit pending", "ben"),
    (12, "ACCC", None, "Credited to the beneficiary account", "ben"),
]


class MockSwift:
    is_mock = True

    def send(self, remittance: dict) -> dict:
        return {
            "uetr": str(uuid.uuid4()),
            "message_type": "pacs.008 (MT103)",
            "sender_bic": OUR_BIC,
            "receiver_bic": (remittance.get("swift_bic") or "").upper(),
            "currency": remittance.get("currency"),
            "sent_at": now_iso(),
            "mock": mock_meta("swift"),
        }

    def track(self, uetr: str, *, submitted_at: str | None = None, currency: str | None = None,
              beneficiary_bic: str | None = None) -> dict:
        start = parse_iso(submitted_at) or utcnow()
        elapsed = (utcnow() - start).total_seconds() / 60.0
        names = {"our": OUR_BIC, "corr": CORRESPONDENTS.get((currency or "").upper(), "SYNTUS33XXX"),
                 "ben": (beneficiary_bic or "BENEFICIARY BANK").upper()}
        events = []
        for minutes, status, reason, text, agent in _TIMELINE:
            if elapsed >= minutes:
                events.append({
                    "status": status,
                    "reason": reason,
                    "description": text.format(our=names["our"], corr=names["corr"], ben=names["ben"]),
                    "agent_bic": names[agent],
                    "occurred_at": iso(start + timedelta(minutes=minutes)),
                })
        last = events[-1] if events else None
        pending = [{"description": text.format(our=names["our"], corr=names["corr"], ben=names["ben"]),
                    "expected_after_minutes": minutes} for minutes, _s, _r, text, _a in _TIMELINE if elapsed < minutes]
        return {
            "uetr": uetr,
            "transaction_status": last["status"] if last else "ACSP",
            "completed": bool(last and last["status"] == "ACCC"),
            "events": events,
            "pending_steps": pending,
            "checked_at": now_iso(),
            "mock": mock_meta("swift"),
        }


def get(conn: sqlite3.Connection):
    return resolve(conn, "swift", MockSwift)

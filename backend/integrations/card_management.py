"""Card management system (CMS) adapter: controls, limits, block/unblock. No PAN is ever stored."""
from __future__ import annotations

import sqlite3

from core import new_id, now_iso

from .base import mock_meta, resolve


class MockCardManagement:
    is_mock = True

    def set_controls(self, cms_card_ref: str, controls: dict) -> dict:
        return {"cms_ack_ref": "MOCK-CMS-" + new_id()[:10].upper(), "applied_at": now_iso(), "applied": controls,
                "mock": mock_meta("card_management")}

    def block(self, cms_card_ref: str, permanent: bool, reason: str | None = None) -> dict:
        return {"cms_ack_ref": "MOCK-BLK-" + new_id()[:10].upper(), "blocked_at": now_iso(),
                "status": "permanently_blocked" if permanent else "temporarily_blocked", "mock": mock_meta("card_management")}

    def unblock(self, cms_card_ref: str) -> dict:
        return {"cms_ack_ref": "MOCK-UBL-" + new_id()[:10].upper(), "unblocked_at": now_iso(), "status": "active",
                "mock": mock_meta("card_management")}

    def issue_virtual_debit(self, customer_ref: str, account_number: str) -> dict:
        import secrets

        last4 = f"{secrets.randbelow(10000):04d}"
        return {"cms_card_ref": "MOCK-CARD-" + new_id(), "last4": last4, "network": "RuPay", "expiry": "10/31",
                "mock": mock_meta("card_management")}


def get(conn: sqlite3.Connection):
    return resolve(conn, "card_management", MockCardManagement)

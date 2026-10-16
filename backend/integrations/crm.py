"""CRM (lead management) adapter. Leads are pushed to the bank's CRM for a call-back.

The mock returns a ``MOCK-CRM-...`` reference and never contacts anyone. The live
adapter is only selected after an approved integration request (see integrations.base).
"""
from __future__ import annotations

import sqlite3

from core import new_id, now_iso

from .base import mock_meta, resolve


class MockCrm:
    is_mock = True

    def create_lead(self, lead: dict) -> dict:
        """Accepts the lead payload (reference, product, channel, masked contact) and returns the CRM reference."""
        return {
            "crm_ref": "MOCK-CRM-" + new_id().replace("-", "")[:10].upper(),
            "accepted_at": now_iso(),
            "queue": "callback_" + str(lead.get("product_slug") or "general")[:40],
            "mock": mock_meta("crm"),
        }


def get(conn: sqlite3.Connection):
    return resolve(conn, "crm", MockCrm)

"""GST (GSTN CPIN) and income-tax (OLTAS / e-Pay Tax) challan payment adapter.

The mock validates a 14-digit CPIN and answers with a deterministic synthetic amount due,
and returns synthetic CINs (Challan Identification Numbers). Nothing reaches a real tax
portal. Sandbox behaviour: a CPIN ending in ``9999`` fails at the portal so the reversal
path can be tested.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from datetime import timedelta

from core import IST, now_iso, today_ist, utcnow

from .base import IntegrationError, mock_meta, resolve

CPIN_RE = re.compile(r"^[0-9]{14}$")
BANK_CODE = "EXMB"
BSR_CODE = "0510001"  # synthetic 7-digit BSR code of the collecting branch
TAX_TYPES = {
    "advance_tax": {"label": "Advance tax (100)", "minor_head": "100"},
    "self_assessment": {"label": "Self-assessment tax (300)", "minor_head": "300"},
    "regular_assessment": {"label": "Tax on regular assessment (400)", "minor_head": "400"},
}


class MockTaxPortal:
    is_mock = True

    def validate_cpin(self, cpin: str) -> dict | None:
        """Amount due for a CPIN, or None when the CPIN is not valid/known."""
        if not CPIN_RE.match(cpin or ""):
            return None
        seed = int(hashlib.sha256(cpin.encode()).hexdigest()[:10], 16)
        return {
            "cpin": cpin,
            "gstin": f"27AAACS{seed % 10000:04d}F1Z5",
            "amount_due_paise": 500000 + (seed % 9000) * 1000,
            "breakup": {"cgst": "50%", "sgst": "50%", "igst": "0%", "cess": "0%"},
            "valid_until": (today_ist() + timedelta(days=15)).isoformat(),
            "mock": mock_meta("gst_tax_portal"),
        }

    def pay_gst(self, *, cpin: str, amount_paise: int, payment_ref: str) -> dict:
        if cpin.endswith("9999"):
            raise IntegrationError("gst_tax_portal", "GSTN did not acknowledge the challan (sandbox failure for CPINs ending 9999)")
        return {"cin": cpin + BANK_CODE, "bank_ref": "GST" + secrets.token_hex(5).upper(), "payment_ref": payment_ref,
                "paid_at": now_iso(), "mock": mock_meta("gst_tax_portal")}

    def pay_income_tax(self, *, pan: str, assessment_year: str, tax_type: str, amount_paise: int, payment_ref: str) -> dict:
        tender = utcnow().astimezone(IST).strftime("%d%m%y")
        challan = f"{secrets.randbelow(100000):05d}"
        return {
            "cin": BSR_CODE + tender + challan,
            "bsr_code": BSR_CODE,
            "tender_date": tender,
            "challan_serial": challan,
            "minor_head": TAX_TYPES.get(tax_type, {}).get("minor_head"),
            "payment_ref": payment_ref,
            "paid_at": now_iso(),
            "mock": mock_meta("gst_tax_portal"),
        }


def get(conn: sqlite3.Connection):
    return resolve(conn, "gst_tax_portal", MockTaxPortal)

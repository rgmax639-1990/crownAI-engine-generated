"""GSTN (via a GST Suvidha Provider) adapter: GST return filings and turnover for MSME underwriting."""
from __future__ import annotations

import hashlib
import random
import sqlite3

from core import now_iso, today_ist

from .base import mock_meta, resolve


class MockGstn:
    is_mock = True

    def fetch_returns(self, gstin: str) -> dict:
        gstin = (gstin or "").upper()
        rng = random.Random(int(hashlib.sha256(("gstn:" + gstin).encode("utf-8")).hexdigest()[:12], 16))
        annual = rng.randrange(5_000_000_00, 100_000_000_00, 1_000_000)  # Rs. 50 lakh - Rs. 10 crore
        first = today_ist().replace(day=1)
        periods = []
        for i in range(12, 0, -1):
            y, m = first.year, first.month - i
            while m <= 0:
                m += 12
                y -= 1
            filed = rng.random() > 0.06
            periods.append({"period": f"{m:02d}{y:04d}", "return_type": "GSTR-3B", "filed": filed,
                            "taxable_turnover_paise": int(annual / 12 * rng.uniform(0.75, 1.25)) if filed else 0})
        return {
            "gstin": gstin,
            "registration_status": "Active",
            "taxpayer_type": "Regular",
            "legal_name": "Synthetic MSME " + gstin[2:7],
            "periods": periods,
            "returns_filed_12m": sum(1 for p in periods if p["filed"]),
            "annual_turnover_paise": sum(p["taxable_turnover_paise"] for p in periods),
            "fetched_at": now_iso(),
            "mock": mock_meta("gstn"),
        }


def get(conn: sqlite3.Connection):
    return resolve(conn, "gstn", MockGstn)

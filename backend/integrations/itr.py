"""Income-tax return (ITR) fetch adapter, used with consent for income verification."""
from __future__ import annotations

import hashlib
import random
import sqlite3

from core import now_iso, today_ist

from .base import mock_meta, resolve


class MockItr:
    is_mock = True

    def fetch(self, pan_ref: str) -> dict:
        rng = random.Random(int(hashlib.sha256(("itr:" + (pan_ref or "")).encode("utf-8")).hexdigest()[:12], 16))
        d = today_ist()
        fy_start = d.year if d.month >= 4 else d.year - 1
        base = rng.randrange(8_00_000_00, 60_00_000_00, 10_000_00)  # Rs. 8 lakh - Rs. 60 lakh a year
        years = []
        for k in range(1, 3):
            ay_start = fy_start - k + 1
            income = int(base * (1 - 0.08 * (k - 1)) * rng.uniform(0.95, 1.05))
            years.append({
                "assessment_year": f"{ay_start}-{str(ay_start + 1)[-2:]}",
                "gross_total_income_paise": income,
                "tax_paid_paise": int(income * rng.uniform(0.08, 0.2)),
                "itr_form": rng.choice(["ITR-3", "ITR-4"]),
                "filed": True,
            })
        return {
            "pan_masked": pan_ref if pan_ref and "X" in pan_ref else None,
            "returns": years,
            "latest_income_paise": years[0]["gross_total_income_paise"],
            "fetched_at": now_iso(),
            "mock": mock_meta("itr"),
        }


def get(conn: sqlite3.Connection):
    return resolve(conn, "itr", MockItr)

"""Account Aggregator (FIU) adapter: consent artefacts and financial-information fetch.

The mock approves the consent at once (it returns an in-app redirect URL instead of an AA
app hand-off) and synthesises a deterministic six-month bank-statement summary for the
subject, so the same customer always gets the same underwriting inputs.
"""
from __future__ import annotations

import hashlib
import random
import sqlite3
from datetime import timedelta

from core import iso, new_id, now_iso, today_ist, utcnow

from .base import mock_meta, resolve


def _rng(seed: str) -> random.Random:
    return random.Random(int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16], 16))


class MockAccountAggregator:
    is_mock = True

    def create_consent(self, *, subject_ref: str, purpose: str, fi_types: list[str] | None = None,
                       duration_months: int = 12, fetch_months: int = 6, return_path: str = "/portal/loans") -> dict:
        handle = "MOCK-AA-" + new_id().replace("-", "")[:16].upper()
        sep = "&" if "?" in return_path else "?"
        return {
            "consent_handle": handle,
            "status": "APPROVED",
            "purpose": purpose,
            "fi_types": fi_types or ["DEPOSIT"],
            "fetch_months": fetch_months,
            "consent_expires_at": iso(utcnow() + timedelta(days=30 * duration_months)),
            # A real AA returns its own consent-manager URL; the mock returns an in-app URL.
            "redirect_url": f"{return_path}{sep}aa_consent={handle}&aa_status=approved",
            "subject_seed": subject_ref,
            "created_at": now_iso(),
            "mock": mock_meta("account_aggregator"),
        }

    def fetch_statements(self, handle: str, *, subject_ref: str | None = None, months: int = 6) -> dict:
        rng = _rng("aa:" + (subject_ref or handle))
        monthly_income = rng.randrange(6_000_000, 30_000_000, 50_000)  # Rs. 60,000 - Rs. 3 lakh
        spend_ratio = rng.uniform(0.35, 0.7)
        rows = []
        first = today_ist().replace(day=1)
        for i in range(months, 0, -1):
            y, m = first.year, first.month - i
            while m <= 0:
                m += 12
                y -= 1
            inflow = int(monthly_income * rng.uniform(0.95, 1.08))
            outflow = int(inflow * spend_ratio * rng.uniform(0.9, 1.1))
            rows.append({"month": f"{y:04d}-{m:02d}", "inflow_paise": inflow, "outflow_paise": outflow,
                         "closing_balance_paise": int(inflow * rng.uniform(0.4, 1.6))})
        avg_in = sum(r["inflow_paise"] for r in rows) // len(rows)
        avg_out = sum(r["outflow_paise"] for r in rows) // len(rows)
        return {
            "consent_handle": handle,
            "fip_count": rng.choice([1, 1, 2]),
            "months": rows,
            "avg_monthly_inflow_paise": avg_in,
            "avg_monthly_outflow_paise": avg_out,
            "salary_detected": rng.random() < 0.8,
            "cheque_bounces_6m": rng.choice([0, 0, 0, 0, 1, 3]),
            "fetched_at": now_iso(),
            "mock": mock_meta("account_aggregator"),
        }


def get(conn: sqlite3.Connection):
    return resolve(conn, "account_aggregator", MockAccountAggregator)

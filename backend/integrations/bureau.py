"""Credit bureau adapter. The mock returns a deterministic score (300-900) and active loans per subject."""
from __future__ import annotations

import hashlib
import random
import sqlite3

from core import new_id, now_iso

from .base import mock_meta, resolve


class MockBureau:
    is_mock = True

    def pull(self, subject_ref: str, pan_masked: str | None = None) -> dict:
        h = int(hashlib.sha256(("bureau:" + subject_ref).encode("utf-8")).hexdigest()[:12], 16)
        bucket = h % 1000
        # Mostly prime borrowers, with a thin tail of low scores so decline/refer paths are reachable.
        score = 300 + bucket * 4 if bucket < 80 else 640 + (bucket % 241)
        score = max(300, min(900, score))
        rng = random.Random(h)
        loans = []
        for _ in range(rng.choice([0, 0, 1, 1, 2])):
            kind = rng.choice(["Credit card", "Vehicle loan", "Consumer durable loan", "Home loan"])
            emi = 0 if kind == "Credit card" else rng.randrange(300_000, 4_500_000, 10_000)
            loans.append({
                "account_type": kind,
                "lender": "Synthetic Lender " + rng.choice("ABCDE"),
                "outstanding_paise": rng.randrange(5_000_000, 150_000_000, 100_000),
                "emi_paise": emi,
                "dpd_max_12m": rng.choice([0, 0, 0, 0, 30]),
            })
        return {
            "report_ref": "MOCK-BUR-" + new_id().replace("-", "")[:10].upper(),
            "pan_masked": pan_masked,
            "score": score,
            "score_range": [300, 900],
            "active_loans": loans,
            "total_emi_paise": sum(lo["emi_paise"] for lo in loans),
            "enquiries_6m": rng.choice([0, 1, 1, 2, 4]),
            "pulled_at": now_iso(),
            "mock": mock_meta("bureau"),
        }


def get(conn: sqlite3.Connection):
    return resolve(conn, "bureau", MockBureau)

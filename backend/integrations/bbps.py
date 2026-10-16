"""Bharat Connect (BBPS) adapter: biller catalogue, bill fetch and bill payment.

The mock serves a synthetic biller catalogue and deterministic synthetic bills (the same
biller + consumer number always returns the same bill). Sandbox behaviour: a consumer
number ending in ``9999`` makes the payment fail at the BBPS rail, so the reversal path
can be exercised end to end. Nothing is ever sent to a real biller.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from datetime import timedelta

from core import now_iso, today_ist, utcnow

from .base import IntegrationError, mock_meta, resolve

CATEGORIES: list[dict] = [
    {"category": "electricity", "label": "Electricity", "label_hi": "बिजली"},
    {"category": "water", "label": "Water", "label_hi": "पानी"},
    {"category": "gas", "label": "Piped gas / LPG", "label_hi": "गैस"},
    {"category": "broadband", "label": "Broadband & landline", "label_hi": "ब्रॉडबैंड"},
    {"category": "mobile_postpaid", "label": "Mobile postpaid", "label_hi": "मोबाइल पोस्टपेड"},
    {"category": "dth", "label": "DTH", "label_hi": "डीटीएच"},
    {"category": "insurance", "label": "Insurance premium", "label_hi": "बीमा प्रीमियम"},
    {"category": "fastag", "label": "FASTag recharge", "label_hi": "फ़ास्टैग रिचार्ज"},
    {"category": "credit_card", "label": "Credit card bill", "label_hi": "क्रेडिट कार्ड बिल"},
    {"category": "municipal_tax", "label": "Municipal / property tax", "label_hi": "नगर निगम / संपत्ति कर"},
]

# (biller_id, name, category, consumer label, regex, hint, amount_editable)
_BILLERS: list[tuple[str, str, str, str, str, str, bool]] = [
    ("MSEDCL000MAH01", "Maharashtra State Electricity Distribution (Synthetic)", "electricity", "Consumer number", r"^[0-9]{12}$", "12 digits", False),
    ("BESCOM000KAR01", "Bangalore Electricity Supply (Synthetic)", "electricity", "Account ID", r"^[0-9]{10}$", "10 digits", False),
    ("TPDDL0000DEL01", "North Delhi Power (Synthetic)", "electricity", "CA number", r"^[0-9]{11}$", "11 digits", False),
    ("MCGMWATER0MH01", "Mumbai Municipal Water (Synthetic)", "water", "Connection number", r"^[A-Z]{2}[0-9]{8}$", "2 letters + 8 digits, e.g. MW12345678", False),
    ("DJBWATER00DL01", "Delhi Jal Board (Synthetic)", "water", "K number", r"^[0-9]{10}$", "10 digits", False),
    ("MGLGAS0000MH01", "Mahanagar Gas (Synthetic)", "gas", "BP number", r"^[0-9]{10}$", "10 digits", False),
    ("IGLGAS0000DL01", "Indraprastha Gas (Synthetic)", "gas", "BP number", r"^[0-9]{10}$", "10 digits", False),
    ("ACTFIBER00KA01", "ACT Fibernet (Synthetic)", "broadband", "Account number", r"^[0-9]{6,12}$", "6 to 12 digits", False),
    ("AIRTELBB00IN01", "Airtel Broadband (Synthetic)", "broadband", "Landline number with STD code", r"^0[0-9]{9,10}$", "e.g. 02212345678", False),
    ("AIRTELPOST0IN1", "Airtel Postpaid (Synthetic)", "mobile_postpaid", "Mobile number", r"^[6-9][0-9]{9}$", "10-digit mobile number", False),
    ("JIOPOST000IN01", "Jio Postpaid (Synthetic)", "mobile_postpaid", "Mobile number", r"^[6-9][0-9]{9}$", "10-digit mobile number", False),
    ("TATAPLAY00IN01", "Tata Play DTH (Synthetic)", "dth", "Subscriber ID", r"^[0-9]{10}$", "10 digits", True),
    ("DISHTV0000IN01", "Dish TV (Synthetic)", "dth", "Registered mobile or VC number", r"^[0-9]{10,11}$", "10 or 11 digits", True),
    ("LICINDIA00IN01", "Life insurance premium (Synthetic)", "insurance", "Policy number", r"^[0-9]{9}$", "9 digits", False),
    ("HEALTHINS0IN01", "Health insurance premium (Synthetic)", "insurance", "Policy number", r"^[A-Z0-9]{8,16}$", "8 to 16 letters/digits", False),
    ("FASTAGNHAI0IN1", "FASTag - Example Bank (Synthetic)", "fastag", "Vehicle registration number", r"^[A-Z]{2}[0-9]{1,2}[A-Z]{0,3}[0-9]{4}$", "e.g. MH01AB1234", True),
    ("FASTAGOTH00IN1", "FASTag - other issuer (Synthetic)", "fastag", "Vehicle registration number", r"^[A-Z]{2}[0-9]{1,2}[A-Z]{0,3}[0-9]{4}$", "e.g. KA05MN4321", True),
    ("CCBILLOTHR0IN1", "Credit card - any bank (Synthetic)", "credit_card", "Card number (last 4) + registered mobile", r"^[0-9]{4}[6-9][0-9]{9}$", "last 4 card digits followed by 10-digit mobile", True),
    ("MCGMPTAX00MH01", "Mumbai property tax (Synthetic)", "municipal_tax", "Property account number", r"^[A-Z]{2}[0-9]{10}$", "2 letters + 10 digits", False),
    ("BBMPPTAX00KA01", "Bengaluru property tax (Synthetic)", "municipal_tax", "Application number", r"^[0-9]{10}$", "10 digits", False),
]

BILLERS: dict[str, dict] = {
    b[0]: {"biller_id": b[0], "name": b[1], "category": b[2], "consumer_number_label": b[3], "consumer_number_regex": b[4],
           "consumer_number_hint": b[5], "amount_editable": b[6]}
    for b in _BILLERS
}

_NAMES = ["A. Sharma", "R. Iyer", "S. Khan", "P. Reddy", "M. Das", "K. Singh", "N. Patel", "V. Menon"]


def find_biller(biller_id: str | None) -> dict | None:
    return BILLERS.get(str(biller_id or "").strip().upper())


def normalize_consumer_number(value: str | None) -> str:
    return re.sub(r"[\s\-]", "", str(value or "")).upper()


def valid_consumer_number(biller: dict, consumer_number: str) -> bool:
    return bool(re.fullmatch(biller["consumer_number_regex"], consumer_number))


def catalogue() -> list[dict]:
    out = []
    for cat in CATEGORIES:
        billers = [dict(b) for b in BILLERS.values() if b["category"] == cat["category"]]
        out.append({**cat, "billers": billers})
    return out


class MockBbps:
    is_mock = True

    def billers(self) -> list[dict]:
        return catalogue()

    def fetch_bill(self, biller_id: str, consumer_number: str) -> dict:
        """Deterministic synthetic bill for (biller, consumer number)."""
        biller = find_biller(biller_id)
        if not biller:
            raise IntegrationError("bbps", "Unknown biller")
        seed = int(hashlib.sha256(f"{biller['biller_id']}|{consumer_number}".encode()).hexdigest()[:12], 16)
        base = {"electricity": 180000, "water": 45000, "gas": 90000, "broadband": 99900, "mobile_postpaid": 59900,
                "dth": 35000, "insurance": 1250000, "fastag": 50000, "credit_card": 1875000, "municipal_tax": 1450000}
        amount = base.get(biller["category"], 100000) + (seed % 250) * 100
        due = today_ist() + timedelta(days=3 + seed % 18)
        return {
            "biller_id": biller["biller_id"],
            "biller_name": biller["name"],
            "consumer_number": consumer_number,
            "customer_name": _NAMES[seed % len(_NAMES)] + " (synthetic)",
            "bill_amount_paise": amount,
            "due_date": due.isoformat(),
            "bill_date": (due - timedelta(days=15)).isoformat(),
            "bill_number": f"BILL{seed % 10**10:010d}",
            "amount_editable": biller["amount_editable"],
            "fetched_at": now_iso(),
            "mock": mock_meta("bbps"),
        }

    def pay(self, *, biller_id: str, consumer_number: str, amount_paise: int, payment_ref: str) -> dict:
        if consumer_number.endswith("9999"):
            raise IntegrationError("bbps", "Biller system did not confirm the payment (sandbox failure for consumer numbers ending 9999)")
        return {
            "bbps_txn_id": "BBPS" + utcnow().strftime("%y%m%d") + secrets.token_hex(5).upper(),
            "payment_ref": payment_ref,
            "status": "success",
            "paid_at": now_iso(),
            "mock": mock_meta("bbps"),
        }


def get(conn: sqlite3.Connection):
    return resolve(conn, "bbps", MockBbps)

"""Mutual fund RTA / exchange adapter: scheme catalogue, NAVs, orders and holdings.

The mock keeps a simulated RTA register in ``mock_mf_rta_*`` tables. Scheme names are
synthetic (no real AMC). NAVs follow a deterministic path per scheme (trend plus a
seasonal wiggle), so historical purchase NAVs and today's NAV are always consistent.
Orders are allotted at today's NAV immediately (a real RTA allots at the applicable NAV
after the cut-off time).
"""
from __future__ import annotations

import hashlib
import math
import sqlite3
from datetime import date

from core import new_id, now_iso, today_ist

from db import register_ddl

from .base import mock_meta, resolve

register_ddl(
    """
CREATE TABLE IF NOT EXISTS mock_mf_rta_transactions (
  id TEXT PRIMARY KEY, investor_ref TEXT NOT NULL, folio TEXT NOT NULL, scheme_code TEXT NOT NULL,
  txn_type TEXT NOT NULL CHECK (txn_type IN ('purchase','redeem','sip_instalment')),
  txn_date TEXT NOT NULL, amount_paise INTEGER NOT NULL, nav REAL NOT NULL, units REAL NOT NULL,
  rta_ref TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_mock_mf_txn_investor ON mock_mf_rta_transactions(investor_ref, scheme_code, txn_date);
CREATE TABLE IF NOT EXISTS mock_mf_rta_sips (
  id TEXT PRIMARY KEY, investor_ref TEXT NOT NULL, folio TEXT NOT NULL, scheme_code TEXT NOT NULL,
  amount_paise INTEGER NOT NULL, sip_day INTEGER NOT NULL, start_date TEXT NOT NULL, status TEXT NOT NULL,
  rta_ref TEXT NOT NULL, created_at TEXT NOT NULL
);
"""
)

EPOCH = date(2018, 1, 1)
AMC_NAME = "Example Asset Management (synthetic)"

# code, name, category, sub_category, risk, base NAV at EPOCH, annual trend, wiggle amplitude, expense ratio %
_SCHEMES = [
    ("EXF001", "Example Bluechip Equity Fund - Direct Growth", "equity", "Large Cap", "Very High", 28.40, 0.12, 0.05, 0.85),
    ("EXF002", "Example Flexi Cap Fund - Direct Growth", "equity", "Flexi Cap", "Very High", 35.10, 0.14, 0.06, 0.72),
    ("EXF003", "Example Midcap Opportunities Fund - Direct Growth", "equity", "Mid Cap", "Very High", 41.75, 0.16, 0.08, 0.68),
    ("EXF004", "Example ELSS Tax Saver Fund - Direct Growth", "equity", "ELSS", "Very High", 52.30, 0.13, 0.06, 0.79),
    ("EXF005", "Example Large Cap Index Fund - Direct Growth", "equity", "Index", "Very High", 18.90, 0.115, 0.05, 0.20),
    ("EXF006", "Example Short Duration Debt Fund - Direct Growth", "debt", "Short Duration", "Moderate", 21.60, 0.072, 0.003, 0.35),
    ("EXF007", "Example Liquid Fund - Direct Growth", "debt", "Liquid", "Low to Moderate", 2450.00, 0.068, 0.0005, 0.18),
    ("EXF008", "Example Corporate Bond Fund - Direct Growth", "debt", "Corporate Bond", "Moderate", 24.80, 0.075, 0.004, 0.33),
    ("EXF009", "Example Balanced Advantage Fund - Direct Growth", "hybrid", "Balanced Advantage", "Very High", 26.20, 0.10, 0.03, 0.70),
    ("EXF010", "Example Gold Fund of Fund - Direct Growth", "gold", "Gold FoF", "High", 11.40, 0.09, 0.04, 0.40),
]
MIN_PURCHASE_PAISE = 50_000  # Rs. 500
MIN_SIP_PAISE = 50_000


def _scheme_row(t: tuple) -> dict:
    code, name, cat, sub, risk, base, trend, wiggle, ter = t
    return {"scheme_code": code, "scheme_name": name, "category": cat, "sub_category": sub, "riskometer": risk,
            "base_nav": base, "trend": trend, "wiggle": wiggle, "expense_ratio_percent": ter, "amc": AMC_NAME,
            "plan": "Direct", "option": "Growth", "min_purchase_paise": MIN_PURCHASE_PAISE, "min_sip_paise": MIN_SIP_PAISE}


SCHEMES: dict[str, dict] = {t[0]: _scheme_row(t) for t in _SCHEMES}


def nav_on(scheme_code: str, d: date | None = None) -> float:
    s = SCHEMES[scheme_code]
    d = d or today_ist()
    days = (d - EPOCH).days
    phase = int(hashlib.sha256(scheme_code.encode()).hexdigest()[:4], 16) % 628 / 100.0
    value = s["base_nav"] * (1 + s["trend"]) ** (days / 365.0) * (1 + s["wiggle"] * math.sin(days / 37.0 + phase))
    return round(value, 4)


def default_folio(investor_ref: str) -> str:
    return "EX" + str(int(hashlib.sha256(("folio:" + investor_ref).encode()).hexdigest()[:10], 16) % 10**8).zfill(8) + "/01"


class RtaRejected(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class MockMfRta:
    is_mock = True

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # -------------------------------------------------------------- catalogue
    def schemes(self) -> list[dict]:
        today = today_ist()
        out = []
        for code, s in SCHEMES.items():
            pub = {k: v for k, v in s.items() if k not in ("base_nav", "trend", "wiggle")}
            nav = nav_on(code, today)
            prev = nav_on(code, date.fromordinal(today.toordinal() - 365))
            pub.update({"nav": nav, "nav_date": today.isoformat(), "return_1y_percent": round((nav / prev - 1) * 100, 2)})
            out.append(pub)
        return out

    def scheme(self, scheme_code: str) -> dict | None:
        return next((s for s in self.schemes() if s["scheme_code"] == scheme_code), None)

    def nav(self, scheme_code: str, d: date | None = None) -> float:
        if scheme_code not in SCHEMES:
            raise RtaRejected("unknown_scheme", "Scheme not found")
        return nav_on(scheme_code, d)

    # -------------------------------------------------------------- register
    def record_transaction(self, *, investor_ref: str, folio: str, scheme_code: str, txn_type: str, txn_date: date,
                           amount_paise: int | None = None, units: float | None = None) -> dict:
        nav = nav_on(scheme_code, txn_date)
        if txn_type == "redeem":
            units = round(float(units or 0), 3)
            amount_paise = int(round(units * nav * 100))
        else:
            units = round((amount_paise or 0) / 100.0 / nav, 3)
        row = {
            "id": new_id(), "investor_ref": investor_ref, "folio": folio, "scheme_code": scheme_code, "txn_type": txn_type,
            "txn_date": txn_date.isoformat(), "amount_paise": int(amount_paise or 0), "nav": nav, "units": units,
            "rta_ref": "MOCK-RTA-" + new_id().replace("-", "")[:10].upper(), "created_at": now_iso(),
        }
        self.conn.execute(
            """INSERT INTO mock_mf_rta_transactions (id, investor_ref, folio, scheme_code, txn_type, txn_date, amount_paise, nav,
               units, rta_ref, created_at) VALUES (:id, :investor_ref, :folio, :scheme_code, :txn_type, :txn_date, :amount_paise,
               :nav, :units, :rta_ref, :created_at)""",
            row,
        )
        return row

    def units_held(self, investor_ref: str, folio: str, scheme_code: str) -> float:
        rows = self.conn.execute(
            "SELECT txn_type, units FROM mock_mf_rta_transactions WHERE investor_ref = ? AND folio = ? AND scheme_code = ?",
            (investor_ref, folio, scheme_code)).fetchall()
        return round(sum(-r["units"] if r["txn_type"] == "redeem" else r["units"] for r in rows), 3)

    def place_order(self, *, investor_ref: str, scheme_code: str, order_type: str, amount_paise: int | None = None,
                    units: float | None = None, folio: str | None = None, sip_day: int | None = None) -> dict:
        if scheme_code not in SCHEMES:
            raise RtaRejected("unknown_scheme", "Scheme not found")
        folio = folio or default_folio(investor_ref)
        today = today_ist()
        if order_type == "purchase":
            txn = self.record_transaction(investor_ref=investor_ref, folio=folio, scheme_code=scheme_code, txn_type="purchase",
                                          txn_date=today, amount_paise=amount_paise)
            return {"rta_ref": txn["rta_ref"], "folio": folio, "units": txn["units"], "nav": txn["nav"],
                    "amount_paise": txn["amount_paise"], "status": "allotted", "mock": mock_meta("mf_rta")}
        if order_type == "redeem":
            held = self.units_held(investor_ref, folio, scheme_code)
            if units is None or units <= 0 or units - held > 1e-6:
                raise RtaRejected("insufficient_units", f"Only {held:.3f} units are available in this folio")
            txn = self.record_transaction(investor_ref=investor_ref, folio=folio, scheme_code=scheme_code, txn_type="redeem",
                                          txn_date=today, units=units)
            return {"rta_ref": txn["rta_ref"], "folio": folio, "units": txn["units"], "nav": txn["nav"],
                    "amount_paise": txn["amount_paise"], "status": "redemption_accepted", "mock": mock_meta("mf_rta")}
        if order_type == "sip":
            ref = "MOCK-SIP-" + new_id().replace("-", "")[:10].upper()
            self.conn.execute(
                """INSERT INTO mock_mf_rta_sips (id, investor_ref, folio, scheme_code, amount_paise, sip_day, start_date, status,
                   rta_ref, created_at) VALUES (?,?,?,?,?,?,?,'registered',?,?)""",
                (new_id(), investor_ref, folio, scheme_code, amount_paise, sip_day or 5, today.isoformat(), ref, now_iso()),
            )
            return {"rta_ref": ref, "folio": folio, "units": None, "nav": nav_on(scheme_code, today),
                    "amount_paise": amount_paise, "sip_day": sip_day or 5, "status": "sip_registered", "mock": mock_meta("mf_rta")}
        raise RtaRejected("invalid_order_type", "Order type must be purchase, redeem or sip")

    def holdings(self, investor_ref: str) -> list[dict]:
        """Per (folio, scheme): units, cost of units still held (average-cost method) and the cash-flow history."""
        rows = self.conn.execute(
            """SELECT * FROM mock_mf_rta_transactions WHERE investor_ref = ? ORDER BY txn_date, created_at""",
            (investor_ref,)).fetchall()
        book: dict[tuple[str, str], dict] = {}
        for r in rows:
            key = (r["folio"], r["scheme_code"])
            h = book.setdefault(key, {"folio": r["folio"], "scheme_code": r["scheme_code"], "units": 0.0, "cost_paise": 0.0,
                                      "cashflows": []})
            if r["txn_type"] == "redeem":
                avg = h["cost_paise"] / h["units"] if h["units"] > 0 else 0
                h["cost_paise"] -= avg * r["units"]
                h["units"] -= r["units"]
                h["cashflows"].append({"date": r["txn_date"], "amount_paise": r["amount_paise"]})
            else:
                h["units"] += r["units"]
                h["cost_paise"] += r["amount_paise"]
                h["cashflows"].append({"date": r["txn_date"], "amount_paise": -r["amount_paise"]})
        out = []
        for h in book.values():
            h["units"] = round(h["units"], 3)
            h["cost_paise"] = int(round(max(0.0, h["cost_paise"])))
            if h["units"] > 0.0005:
                out.append(h)
        return out

    def sips(self, investor_ref: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM mock_mf_rta_sips WHERE investor_ref = ? ORDER BY created_at DESC", (investor_ref,))]


def get(conn: sqlite3.Connection):
    return resolve(conn, "mf_rta", lambda: MockMfRta(conn))

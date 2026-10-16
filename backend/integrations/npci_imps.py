"""NPCI IMPS adapter (immediate payment, 24x7) and the shared mock payment-rail engine.

Mock behaviour (deterministic test hooks driven by the transfer remarks, US-19):
  * normal               -> accepted, settles immediately ("success"), 12-digit RRN
  * remarks contain FAIL -> the rail rejects the payment (accepted False, status "failed")
  * remarks contain TIMEOUT -> the rail *accepts* the payment, then the call times out
    (``RailTimeout``). The caller must not retry blindly; a status enquiry by
    ``payment_id`` (or a later callback) tells what really happened.
  * remarks contain RETURN -> accepted as pending, later returned by the beneficiary bank
    (status enquiry answers "failed").

The mock rail keeps its own record of every payment in ``mock_rail_payments`` (one row per
rail + payment id, so a repeated initiate for the same payment is answered from the
record instead of paying twice), which is what ``status()`` answers from.

Penny-drop / name check: ``name_check(account_number, ifsc)`` returns the name held by the
beneficiary bank. The mock answers with the Finacle name for accounts of this bank and a
plausible synthetic name for other banks; an IFSC that is not well formed is invalid.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import timedelta

from core import IFSC_RE, iso, now_iso, parse_iso, utcnow
from db import register_ddl

from .base import IntegrationError, mock_meta, resolve
from .finacle import BANK_IFSC_PREFIX

register_ddl(
    """
CREATE TABLE IF NOT EXISTS mock_rail_payments (
  id TEXT PRIMARY KEY, rail TEXT NOT NULL, payment_id TEXT NOT NULL, rail_reference TEXT NOT NULL UNIQUE,
  amount_paise INTEGER NOT NULL, payee TEXT, status TEXT NOT NULL, final_status TEXT NOT NULL, reason TEXT,
  created_at TEXT NOT NULL, settle_at TEXT, settled_at TEXT,
  UNIQUE (rail, payment_id)
);
"""
)


class RailTimeout(IntegrationError):
    """The rail did not answer in time. The payment may or may not have been accepted."""

    status_code = 504
    code = "rail_timeout"


FIRST_NAMES = ["Aarav", "Vivaan", "Aditya", "Ishaan", "Kavya", "Diya", "Meera", "Saanvi", "Arjun", "Rohit", "Neha", "Pooja",
               "Karthik", "Lakshmi", "Suresh", "Anjali", "Farhan", "Zoya", "Gurpreet", "Harpreet"]
LAST_NAMES = ["Sharma", "Verma", "Iyer", "Nair", "Reddy", "Patel", "Shah", "Gupta", "Khan", "Singh", "Das", "Menon", "Joshi",
              "Kulkarni", "Banerjee", "Rao"]


def synthetic_name(seed: str) -> str:
    """A stable, plausible synthetic account-holder name for a mock lookup."""
    h = hashlib.sha256(seed.encode("utf-8")).digest()
    return f"{FIRST_NAMES[h[0] % len(FIRST_NAMES)]} {LAST_NAMES[h[1] % len(LAST_NAMES)]}"


def new_rrn() -> str:
    """12-digit retrieval reference number: Y + DDD (julian day) + HH + 6 digits."""
    now = utcnow()
    return f"{now.year % 10}{now.timetuple().tm_yday:03d}{now.hour:02d}" + "".join(secrets.choice("0123456789") for _ in range(6))


def payee_label(payee: dict | str | None) -> str | None:
    """Masked payee for the mock's own records: a VPA, or XXXX1234@IFSC."""
    if isinstance(payee, dict):
        if payee.get("vpa"):
            return str(payee["vpa"])
        acct = str(payee.get("account_number") or "")
        return f"XXXX{acct[-4:]}@{payee.get('ifsc') or ''}"
    return payee


def remarks_hook(remarks: str | None) -> str | None:
    text = (remarks or "").upper()
    for hook in ("TIMEOUT", "FAIL", "RETURN"):
        if hook in text:
            return hook
    return None


class MockRail:
    """Mock behaviour shared by the IMPS, UPI and NEFT/RTGS adapters."""

    is_mock = True
    provider = "npci_imps"

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # -------------------------------------------------------------- overridable per rail
    def _reference(self, rail: str) -> str:
        return new_rrn()

    def _settle_seconds(self, rail: str) -> int:
        """0 means the rail settles immediately on acceptance."""
        return 0

    # -------------------------------------------------------------- engine
    def _existing(self, rail: str, payment_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM mock_rail_payments WHERE rail = ? AND payment_id = ?", (rail, payment_id)).fetchone()
        return dict(row) if row else None

    def _answer(self, rec: dict) -> dict:
        return {
            "accepted": rec["status"] != "rejected",
            "rail_reference": rec["rail_reference"],
            "status": "failed" if rec["status"] == "rejected" else rec["status"],
            "reason": rec["reason"],
            "settled_at": rec["settled_at"],
            "rail": rec["rail"],
            "mock": mock_meta(self.provider),
        }

    def record(self, rail: str, payment_id: str, amount_paise: int, *, status: str, final_status: str,
               reason: str | None = None, payee: str | None = None, created_at: str | None = None,
               settle_at: str | None = None, settled_at: str | None = None, rail_reference: str | None = None) -> dict:
        """Write a rail-side record (also used by the synthetic seed)."""
        rec = {
            "id": secrets.token_hex(8), "rail": rail, "payment_id": payment_id, "rail_reference": rail_reference or self._reference(rail),
            "amount_paise": amount_paise, "payee": payee, "status": status, "final_status": final_status, "reason": reason,
            "created_at": created_at or now_iso(), "settle_at": settle_at, "settled_at": settled_at,
        }
        self.conn.execute(
            """INSERT INTO mock_rail_payments (id, rail, payment_id, rail_reference, amount_paise, payee, status, final_status, reason,
               created_at, settle_at, settled_at) VALUES (:id, :rail, :payment_id, :rail_reference, :amount_paise, :payee, :status,
               :final_status, :reason, :created_at, :settle_at, :settled_at)""",
            rec,
        )
        return rec

    def _initiate(self, rail: str, *, payment_id: str, amount_paise: int, remarks: str | None, payee: dict | str | None) -> dict:
        payee = payee_label(payee)  # the mock rail never keeps a full account number
        existing = self._existing(rail, payment_id)
        if existing:  # the rail de-duplicates on the payment id: never pays twice
            return {**self._answer(self._refresh(existing)), "duplicate": True}
        hook = remarks_hook(remarks)
        now = utcnow()
        if hook == "FAIL":
            rec = self.record(rail, payment_id, amount_paise, status="rejected", final_status="rejected", payee=payee,
                              reason="Beneficiary bank rejected the payment (MOCK test hook FAIL)")
            return self._answer(rec)
        delay = self._settle_seconds(rail)
        if hook == "RETURN":
            rec = self.record(rail, payment_id, amount_paise, status="pending", final_status="failed", payee=payee,
                              reason="Returned by beneficiary bank: account closed (MOCK test hook RETURN)",
                              settle_at=iso(now + timedelta(seconds=max(delay, 30))))
        elif delay:
            rec = self.record(rail, payment_id, amount_paise, status="pending", final_status="success", payee=payee,
                              settle_at=iso(now + timedelta(seconds=delay)))
        else:
            rec = self.record(rail, payment_id, amount_paise, status="success", final_status="success", payee=payee,
                              settled_at=iso(now))
        if hook == "TIMEOUT":
            # Accepted on the rail side, but the answer never reaches us.
            raise RailTimeout(self.provider, f"{rail} switch did not respond in time; the payment may have been accepted. "
                                             "Its status will be confirmed by status enquiry or callback.")
        return self._answer(rec)

    def _refresh(self, rec: dict) -> dict:
        if rec["status"] == "pending" and rec.get("settle_at"):
            due = parse_iso(rec["settle_at"])
            if due and due <= utcnow():
                final = rec["final_status"]
                settled = now_iso()
                self.conn.execute("UPDATE mock_rail_payments SET status = ?, settled_at = ? WHERE id = ?",
                                  (final, settled, rec["id"]))
                rec = {**rec, "status": final, "settled_at": settled}
        return rec

    def _status(self, rail: str | None, rail_reference: str | None = None, payment_id: str | None = None) -> dict:
        row = None
        if rail_reference:
            row = self.conn.execute("SELECT * FROM mock_rail_payments WHERE rail_reference = ?", (rail_reference,)).fetchone()
        if row is None and payment_id:
            if rail:
                row = self.conn.execute("SELECT * FROM mock_rail_payments WHERE rail = ? AND payment_id = ?", (rail, payment_id)).fetchone()
            else:
                row = self.conn.execute("SELECT * FROM mock_rail_payments WHERE payment_id = ?", (payment_id,)).fetchone()
        if row is None:
            return {"found": False, "status": "not_found", "rail_reference": rail_reference, "reason": "No such payment on the rail",
                    "settled_at": None, "mock": mock_meta(self.provider)}
        rec = self._refresh(dict(row))
        return {**self._answer(rec), "found": True}


class MockImps(MockRail):
    provider = "npci_imps"

    def initiate(self, *, payment_id: str, amount_paise: int, remarks: str | None = None, payee: str | None = None,
                 rail: str = "IMPS") -> dict:
        """Push an IMPS credit. Returns {accepted, rail_reference (RRN), status, reason}."""
        return self._initiate("IMPS", payment_id=payment_id, amount_paise=amount_paise, remarks=remarks, payee=payee)

    def status(self, rail_reference: str | None = None, *, payment_id: str | None = None) -> dict:
        """IMPS status enquiry (by RRN, or by our payment id after a timeout)."""
        return self._status("IMPS", rail_reference, payment_id)

    def name_check(self, account_number: str, ifsc: str) -> dict:
        """Penny-drop style beneficiary name enquiry."""
        ifsc = (ifsc or "").strip().upper()
        if not IFSC_RE.match(ifsc):
            return {"valid": False, "reason": "invalid_ifsc", "message": "IFSC is not valid", "name_at_bank": None,
                    "mock": mock_meta(self.provider)}
        name = None
        if ifsc.startswith(BANK_IFSC_PREFIX[:4]):
            row = self.conn.execute(
                """SELECT c.full_name FROM mock_finacle_accounts a JOIN mock_finacle_customers c ON c.cif = a.cif
                   WHERE a.account_number = ?""", (account_number,)).fetchone()
            name = row["full_name"] if row else None
        return {
            "valid": True,
            "name_at_bank": (name or synthetic_name(f"{ifsc}:{account_number}")).upper(),
            "ifsc": ifsc,
            "reference": new_rrn(),
            "mock": mock_meta(self.provider),
        }


def get(conn: sqlite3.Connection):
    return resolve(conn, "npci_imps", lambda: MockImps(conn))


__all__ = ["MockRail", "MockImps", "RailTimeout", "get", "new_rrn", "synthetic_name", "remarks_hook"]

"""Finacle core banking adapter (via the bank's ESB over mTLS in live mode).

Finacle is the only source of balances and postings. The platform never stores or
changes a balance itself: it asks Finacle to post, and reads balances and transactions
from Finacle on demand.

The mock keeps its own simulated ledger in ``mock_finacle_*`` tables, which stand in for
the real core banking system. Postings are idempotent on ``idempotency_ref``, so a retried
call can never post twice.
"""
from __future__ import annotations

import random
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone

from core import IST, iso, jdump, new_id, now_iso, parse_date, today_ist, utcnow
from db import register_ddl

from .base import mock_meta, resolve

BANK_IFSC_PREFIX = "EXMB0"
HOME_IFSC = "EXMB0000001"

register_ddl(
    """
CREATE TABLE IF NOT EXISTS mock_finacle_customers (
  cif TEXT PRIMARY KEY, full_name TEXT NOT NULL, mobile TEXT, email TEXT, date_of_birth TEXT,
  pan TEXT, address TEXT, segment TEXT, residency_status TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mock_finacle_accounts (
  account_number TEXT PRIMARY KEY, cif TEXT NOT NULL, account_type TEXT NOT NULL, currency TEXT NOT NULL,
  ifsc TEXT NOT NULL, balance_paise INTEGER NOT NULL DEFAULT 0, hold_paise INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active', opened_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mock_finacle_postings (
  id TEXT PRIMARY KEY, posting_ref TEXT NOT NULL UNIQUE, idempotency_ref TEXT UNIQUE, account_number TEXT NOT NULL,
  posted_at TEXT NOT NULL, value_date TEXT NOT NULL, narration TEXT NOT NULL, dr_cr TEXT NOT NULL CHECK (dr_cr IN ('DR','CR')),
  amount_paise INTEGER NOT NULL, balance_after_paise INTEGER NOT NULL, channel_ref TEXT
);
CREATE INDEX IF NOT EXISTS ix_mock_postings_acct ON mock_finacle_postings(account_number, posted_at);
CREATE TABLE IF NOT EXISTS mock_finacle_deposits (
  deposit_ref TEXT PRIMARY KEY, cif TEXT NOT NULL, account_number TEXT NOT NULL, deposit_type TEXT NOT NULL,
  principal_paise INTEGER NOT NULL, rate_percent REAL NOT NULL, tenure_days INTEGER NOT NULL, start_date TEXT NOT NULL,
  maturity_date TEXT NOT NULL, maturity_amount_paise INTEGER NOT NULL, payout TEXT NOT NULL, maturity_instruction TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active'
);
"""
)


class FinacleBusinessError(Exception):
    """A business rejection from the core (insufficient funds, frozen account...). Mapped to 422."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _posting_ref() -> str:
    return "FIN" + utcnow().strftime("%y%m%d") + secrets.token_hex(5).upper()


def new_account_number() -> str:
    return "5" + "".join(secrets.choice("0123456789") for _ in range(13))


def maturity_amount(principal_paise: int, rate_percent: float, tenure_days: int, payout: str = "cumulative") -> int:
    """Quarterly compounding for cumulative deposits; simple interest for payout deposits."""
    years = tenure_days / 365.0
    if payout == "cumulative" and tenure_days >= 180:
        amount = principal_paise * (1 + rate_percent / 400.0) ** (4 * years)
    else:
        amount = principal_paise * (1 + rate_percent / 100.0 * years)
    return int(round(amount))


class MockFinacle:
    is_mock = True

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # -------------------------------------------------------------- customers
    def create_customer(self, *, full_name: str, mobile: str | None, email: str | None, date_of_birth: str | None,
                        pan: str | None, address: str | None, segment: str = "retail", residency_status: str = "resident") -> str:
        cif = "CIF" + "".join(secrets.choice("0123456789") for _ in range(9))
        self.conn.execute(
            """INSERT INTO mock_finacle_customers (cif, full_name, mobile, email, date_of_birth, pan, address, segment,
               residency_status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (cif, full_name, mobile, email, date_of_birth, pan, address, segment, residency_status, now_iso()),
        )
        return cif

    def get_customer(self, cif: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM mock_finacle_customers WHERE cif = ?", (cif,)).fetchone()
        return dict(row) if row else None

    def lookup_by_account(self, account_number: str, mobile: str) -> dict:
        """Registration check: does this account exist and is ``mobile`` its registered mobile?

        Sandbox behaviour: an unknown but well-formed account number is answered with a
        newly synthesised customer whose registered mobile is the one supplied, so test
        journeys can run end to end without real customer data.
        """
        acct = self.conn.execute("SELECT * FROM mock_finacle_accounts WHERE account_number = ?", (account_number,)).fetchone()
        if not acct:
            cif = self.create_customer(full_name="Synthetic Customer " + account_number[-4:], mobile=mobile, email=None,
                                       date_of_birth="1990-01-15", pan="ABCPS" + account_number[-4:].rjust(4, "0") + "K",
                                       address="Synthetic address, Mumbai, Maharashtra 400001")
            self.open_account(cif, "savings", account_number=account_number, opening_balance_paise=2500000, seed_history=True)
            acct = self.conn.execute("SELECT * FROM mock_finacle_accounts WHERE account_number = ?", (account_number,)).fetchone()
        cust = self.get_customer(acct["cif"])
        return {
            "cif": acct["cif"],
            "mobile_matches": bool(cust and cust["mobile"] == mobile),
            "customer": cust,
            "accounts": self.accounts_for_cif(acct["cif"]),
            "mock": mock_meta("finacle"),
        }

    # -------------------------------------------------------------- accounts
    def open_account(self, cif: str, account_type: str, *, currency: str = "INR", ifsc: str = HOME_IFSC,
                     account_number: str | None = None, opening_balance_paise: int = 0, seed_history: bool = False) -> dict:
        number = account_number or new_account_number()
        opened = utcnow() - timedelta(days=200 if seed_history else 0)
        self.conn.execute(
            """INSERT INTO mock_finacle_accounts (account_number, cif, account_type, currency, ifsc, balance_paise, opened_at)
               VALUES (?,?,?,?,?,0,?)""",
            (number, cif, account_type, currency, ifsc, iso(opened)),
        )
        if seed_history:
            self._seed_history(number, opening_balance_paise)
        elif opening_balance_paise:
            self.post(number, "CR", opening_balance_paise, "Opening deposit", idempotency_ref=f"OPEN-{number}")
        return {"account_number": number, "cif": cif, "ifsc": ifsc, "currency": currency, "mock": mock_meta("finacle")}

    def accounts_for_cif(self, cif: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT account_number, account_type, currency, ifsc, status FROM mock_finacle_accounts WHERE cif = ?", (cif,))]

    def balance(self, account_number: str) -> dict:
        row = self.conn.execute("SELECT * FROM mock_finacle_accounts WHERE account_number = ?", (account_number,)).fetchone()
        if not row:
            raise FinacleBusinessError("account_not_found", "Account not found in core banking")
        return {
            "ledger_balance_paise": row["balance_paise"],
            "available_balance_paise": row["balance_paise"] - row["hold_paise"],
            "currency": row["currency"],
            "status": row["status"],
            "as_of": now_iso(),
            "source": "finacle",
            "mock": mock_meta("finacle"),
        }

    def transactions(self, account_number: str, from_date: date | None = None, to_date: date | None = None,
                     limit: int = 50, offset: int = 0, direction: str | None = None, search: str | None = None) -> dict:
        where = ["account_number = ?"]
        params: list = [account_number]
        if from_date:
            where.append("value_date >= ?")
            params.append(from_date.isoformat())
        if to_date:
            where.append("value_date <= ?")
            params.append(to_date.isoformat())
        if direction in ("DR", "CR"):
            where.append("dr_cr = ?")
            params.append(direction)
        if search:
            where.append("narration LIKE ?")
            params.append(f"%{search}%")
        clause = " AND ".join(where)
        total = self.conn.execute(f"SELECT COUNT(*) FROM mock_finacle_postings WHERE {clause}", params).fetchone()[0]
        rows = self.conn.execute(
            f"""SELECT posting_ref, posted_at, value_date, narration, dr_cr, amount_paise, balance_after_paise, channel_ref
                FROM mock_finacle_postings WHERE {clause} ORDER BY posted_at DESC, rowid DESC LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()
        return {"items": [dict(r) for r in rows], "total": total, "as_of": now_iso(), "source": "finacle",
                "mock": mock_meta("finacle")}

    # -------------------------------------------------------------- postings
    def post(self, account_number: str, dr_cr: str, amount_paise: int, narration: str, *, idempotency_ref: str,
             channel_ref: str | None = None, allow_overdraft: bool = False, posted_at: datetime | None = None) -> dict:
        """Post a debit or credit. Repeating the same ``idempotency_ref`` returns the original posting."""
        existing = self.conn.execute("SELECT * FROM mock_finacle_postings WHERE idempotency_ref = ?", (idempotency_ref,)).fetchone()
        if existing:
            return {**dict(existing), "duplicate": True, "mock": mock_meta("finacle")}
        acct = self.conn.execute("SELECT * FROM mock_finacle_accounts WHERE account_number = ?", (account_number,)).fetchone()
        if not acct:
            raise FinacleBusinessError("account_not_found", "Account not found in core banking")
        if acct["status"] != "active":
            raise FinacleBusinessError("account_inactive", "Account is not active for postings")
        if amount_paise <= 0:
            raise FinacleBusinessError("invalid_amount", "Amount must be positive")
        if dr_cr == "DR" and not allow_overdraft and acct["balance_paise"] - acct["hold_paise"] < amount_paise:
            raise FinacleBusinessError("insufficient_funds", "Insufficient available balance")
        new_balance = acct["balance_paise"] + (amount_paise if dr_cr == "CR" else -amount_paise)
        when = posted_at or utcnow()
        posting = {
            "id": new_id(),
            "posting_ref": _posting_ref(),
            "idempotency_ref": idempotency_ref,
            "account_number": account_number,
            "posted_at": iso(when),
            "value_date": when.astimezone(IST).date().isoformat(),
            "narration": narration[:140],
            "dr_cr": dr_cr,
            "amount_paise": amount_paise,
            "balance_after_paise": new_balance,
            "channel_ref": channel_ref,
        }
        self.conn.execute(
            """INSERT INTO mock_finacle_postings (id, posting_ref, idempotency_ref, account_number, posted_at, value_date,
               narration, dr_cr, amount_paise, balance_after_paise, channel_ref) VALUES (:id, :posting_ref, :idempotency_ref,
               :account_number, :posted_at, :value_date, :narration, :dr_cr, :amount_paise, :balance_after_paise, :channel_ref)""",
            posting,
        )
        self.conn.execute("UPDATE mock_finacle_accounts SET balance_paise = ? WHERE account_number = ?", (new_balance, account_number))
        return {**posting, "duplicate": False, "mock": mock_meta("finacle")}

    def reverse(self, posting_ref: str, narration: str) -> dict:
        orig = self.conn.execute("SELECT * FROM mock_finacle_postings WHERE posting_ref = ?", (posting_ref,)).fetchone()
        if not orig:
            raise FinacleBusinessError("posting_not_found", "Original posting not found")
        return self.post(orig["account_number"], "CR" if orig["dr_cr"] == "DR" else "DR", orig["amount_paise"],
                         narration, idempotency_ref=f"REV-{posting_ref}", allow_overdraft=True)

    def find_posting(self, idempotency_ref: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM mock_finacle_postings WHERE idempotency_ref = ?", (idempotency_ref,)).fetchone()
        return dict(row) if row else None

    # -------------------------------------------------------------- deposits
    def book_fd(self, *, cif: str, account_number: str, principal_paise: int, rate_percent: float, tenure_days: int,
                payout: str, maturity_instruction: str, idempotency_ref: str) -> dict:
        existing = self.conn.execute("SELECT * FROM mock_finacle_deposits WHERE deposit_ref = ?", (idempotency_ref,)).fetchone()
        if existing:
            return {**dict(existing), "mock": mock_meta("finacle")}
        posting = self.post(account_number, "DR", principal_paise, f"FD booking {tenure_days} days",
                            idempotency_ref=f"FD-{idempotency_ref}")
        start = today_ist()
        maturity = start + timedelta(days=tenure_days)
        deposit = {
            "deposit_ref": idempotency_ref,
            "cif": cif,
            "account_number": account_number,
            "deposit_type": "fd",
            "principal_paise": principal_paise,
            "rate_percent": rate_percent,
            "tenure_days": tenure_days,
            "start_date": start.isoformat(),
            "maturity_date": maturity.isoformat(),
            "maturity_amount_paise": maturity_amount(principal_paise, rate_percent, tenure_days, payout),
            "payout": payout,
            "maturity_instruction": maturity_instruction,
            "status": "active",
        }
        self.conn.execute(
            """INSERT INTO mock_finacle_deposits (deposit_ref, cif, account_number, deposit_type, principal_paise, rate_percent,
               tenure_days, start_date, maturity_date, maturity_amount_paise, payout, maturity_instruction, status)
               VALUES (:deposit_ref, :cif, :account_number, :deposit_type, :principal_paise, :rate_percent, :tenure_days,
               :start_date, :maturity_date, :maturity_amount_paise, :payout, :maturity_instruction, :status)""",
            deposit,
        )
        return {**deposit, "posting_ref": posting["posting_ref"], "mock": mock_meta("finacle")}

    def deposits_for_cif(self, cif: str) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM mock_finacle_deposits WHERE cif = ? ORDER BY start_date DESC", (cif,))]

    # -------------------------------------------------------------- synthetic history
    def _seed_history(self, account_number: str, opening_balance_paise: int, days: int = 120) -> None:
        rng = random.Random(account_number)
        start = utcnow() - timedelta(days=days)
        self.post(account_number, "CR", opening_balance_paise, "Opening balance b/f", idempotency_ref=f"SEED-OPEN-{account_number}",
                  posted_at=start)
        merchants = [
            ("SWIGGY", "food", 25000, 90000), ("ZOMATO", "food", 30000, 120000), ("BIGBASKET", "food", 80000, 350000),
            ("UBER INDIA", "travel", 18000, 65000), ("IRCTC", "travel", 60000, 350000), ("AMAZON PAY", "shopping", 50000, 600000),
            ("MYNTRA", "shopping", 90000, 450000), ("BESCOM ELECTRICITY", "bills", 120000, 380000), ("AIRTEL POSTPAID", "bills", 59900, 119900),
            ("INDIANOIL FUEL", "fuel", 100000, 300000), ("BOOKMYSHOW", "entertainment", 40000, 150000), ("APOLLO PHARMACY", "health", 30000, 250000),
        ]
        balance_floor = 500000
        for day in range(days):
            when = start + timedelta(days=day, hours=rng.randint(8, 21), minutes=rng.randint(0, 59))
            d = when.astimezone(IST)
            if d.day == 1:
                self.post(account_number, "CR", 18500000, "NEFT CR SALARY ACME TECH PVT LTD",
                          idempotency_ref=f"SEED-{account_number}-{day}-sal", posted_at=when)
            for k in range(rng.choice([0, 1, 1, 2])):
                name, _cat, lo, hi = rng.choice(merchants)
                amount = rng.randint(lo, hi) // 100 * 100
                bal = self.conn.execute("SELECT balance_paise FROM mock_finacle_accounts WHERE account_number = ?",
                                        (account_number,)).fetchone()[0]
                if bal - amount < balance_floor:
                    continue
                channel = rng.choice(["UPI", "UPI", "POS"])
                ref = "".join(rng.choice("0123456789") for _ in range(12))
                narration = f"UPI/DR/{ref}/{name}" if channel == "UPI" else f"POS {name} CARD XX{account_number[-4:]}"
                self.post(account_number, "DR", amount, narration, idempotency_ref=f"SEED-{account_number}-{day}-{k}",
                          posted_at=when + timedelta(minutes=k * 7))


def get(conn: sqlite3.Connection):
    return resolve(conn, "finacle", lambda: MockFinacle(conn))

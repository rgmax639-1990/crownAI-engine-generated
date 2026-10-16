"""Accounts, transactions and statements (US-18).

Balances and transactions are read live from Finacle on every request; nothing is
stored on the platform. Statements are PDF (password protected) or CSV.

PDF password rule: the first 4 letters of the customer's name in capitals followed by
the date of birth as DDMM, e.g. ANAN1204 for Ananya Sharma born 12/04/1988.
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request, Response

from audit import audit_ctx
from banking import account_number, customer_for, owned_account, public_account
from core import fmt_date, fmt_ist, format_inr, invalid, now_iso, parse_date, today_ist
from db import get_db
from integrations import finacle
from integrations.base import mock_meta
from integrations.finacle import FinacleBusinessError
from pdfgen import Section, make_pdf
from security import AuthContext, require_customer

router = APIRouter(tags=["accounts"])

MAX_RANGE_DAYS = 366
DEFAULT_RANGE_DAYS = 90
STATEMENT_MAX_ROWS = 5000
PASSWORD_HINT = ("First 4 letters of your name in capital letters followed by your date of birth as DDMM "
                 "(for example ANAN1204 for Ananya, born 12 April).")

TYPE_LABELS = {"savings": "Savings account", "current": "Current account", "fd": "Fixed deposit", "rd": "Recurring deposit",
               "loan": "Loan account", "nre": "NRE savings", "nro": "NRO savings", "fcnr": "FCNR deposit"}


def _mock(core) -> dict | None:
    return mock_meta("finacle") if getattr(core, "is_mock", False) else None


def _date_range(from_: Optional[str], to: Optional[str], default_days: int = DEFAULT_RANGE_DAYS) -> tuple[date, date]:
    d_to = parse_date(to) if to else today_ist()
    if to and not d_to:
        raise invalid("to", "Enter the end date as DD/MM/YYYY or YYYY-MM-DD")
    d_from = parse_date(from_) if from_ else d_to - timedelta(days=default_days)  # type: ignore[operator]
    if from_ and not d_from:
        raise invalid("from", "Enter the start date as DD/MM/YYYY or YYYY-MM-DD")
    if d_to < d_from:  # type: ignore[operator]
        raise invalid("to", "The end date must be on or after the start date")
    if (d_to - d_from).days > MAX_RANGE_DAYS:  # type: ignore[operator]
        raise invalid("from", "Choose a period of at most 1 year")
    return d_from, d_to  # type: ignore[return-value]


def statement_password(customer: dict) -> str:
    letters = re.sub(r"[^A-Za-z]", "", customer.get("full_name") or "").upper()[:4]
    dob = parse_date(customer.get("date_of_birth"))
    return letters + (dob.strftime("%d%m") if dob else "0101")


def _balance(core, acct: dict) -> dict:
    try:
        b = core.balance(account_number(acct))
        return {"ledger_balance_paise": b["ledger_balance_paise"], "available_balance_paise": b["available_balance_paise"],
                "account_status": b.get("status"), "as_of": b.get("as_of") or now_iso(), "balance_error": None}
    except FinacleBusinessError as exc:
        return {"ledger_balance_paise": None, "available_balance_paise": None, "account_status": None, "as_of": now_iso(),
                "balance_error": exc.message}


@router.get("/me/accounts")
def list_accounts(ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    core = finacle.get(conn)
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM accounts WHERE customer_id = ? AND owner_type = 'customer' ORDER BY created_at, masked_number", (ctx.customer_id,))]
    items = []
    for acct in rows:
        bal = _balance(core, acct)
        items.append({
            **public_account(acct),
            "type_label": TYPE_LABELS.get(acct["account_type"], acct["account_type"]),
            **bal,
            "balance_paise": bal["available_balance_paise"],
            "source": "finacle",
            "mock": _mock(core),
        })
    return {"items": items, "total": len(items), "as_of": now_iso(), "source": "finacle", "mock": _mock(core)}


@router.get("/me/accounts/{account_id}/transactions")
def account_transactions(account_id: str, from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None,
                         type: Optional[str] = None, q: Optional[str] = None, page: int = 1, page_size: int = 25,
                         ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    acct = owned_account(conn, ctx.customer_id, account_id)
    d_from, d_to = _date_range(from_, to)
    direction = (type or "").strip().upper() or None
    if direction and direction not in ("DR", "CR"):
        raise invalid("type", "Use DR for debits or CR for credits")
    if page < 1:
        raise invalid("page", "Page must be 1 or more")
    if not (1 <= page_size <= 100):
        raise invalid("page_size", "Page size must be between 1 and 100")
    search = (q or "").strip()[:60] or None
    core = finacle.get(conn)
    res = core.transactions(account_number(acct), d_from, d_to, limit=page_size, offset=(page - 1) * page_size,
                            direction=direction, search=search)
    items = [{
        "posting_ref": r["posting_ref"],
        "posted_at": r.get("posted_at"),
        "value_date": r["value_date"],
        "narration": r["narration"],
        "dr_cr": r["dr_cr"],
        "amount_paise": r["amount_paise"],
        "balance_after_paise": r["balance_after_paise"],
    } for r in res["items"]]
    return {
        "account": public_account(acct),
        "items": items,
        "total": res["total"],
        "page": page,
        "page_size": page_size,
        "from": d_from.isoformat(),
        "to": d_to.isoformat(),
        "as_of": res.get("as_of") or now_iso(),
        "source": "finacle",
        "mock": _mock(core),
    }


def _csv_cell(value: object) -> str:
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s  # spreadsheet formula injection guard


def _rupees(paise: int | None) -> str:
    return "" if paise is None else f"{paise / 100:.2f}"


@router.get("/me/accounts/{account_id}/statements")
def account_statement(account_id: str, request: Request, format: str = "pdf", from_: Optional[str] = Query(None, alias="from"),
                      to: Optional[str] = None, ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    acct = owned_account(conn, ctx.customer_id, account_id)
    fmt = (format or "pdf").strip().lower()
    if fmt not in ("pdf", "csv"):
        raise invalid("format", "Choose PDF or CSV")
    d_from, d_to = _date_range(from_, to, default_days=30)
    core = finacle.get(conn)
    number = account_number(acct)
    res = core.transactions(number, d_from, d_to, limit=STATEMENT_MAX_ROWS, offset=0)
    rows = list(reversed(res["items"]))  # oldest first
    customer = customer_for(conn, ctx.customer_id)
    opening = closing = None
    if rows:
        first = rows[0]
        opening = first["balance_after_paise"] + (first["amount_paise"] if first["dr_cr"] == "DR" else -first["amount_paise"])
        closing = rows[-1]["balance_after_paise"]
    total_dr = sum(r["amount_paise"] for r in rows if r["dr_cr"] == "DR")
    total_cr = sum(r["amount_paise"] for r in rows if r["dr_cr"] == "CR")
    base_name = f"statement_{acct['masked_number']}_{d_from.strftime('%Y%m%d')}_{d_to.strftime('%Y%m%d')}"
    mock = getattr(core, "is_mock", False)

    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Account", acct["masked_number"], "Period", fmt_date(d_from), fmt_date(d_to)])
        if mock:
            w.writerow(["MOCK / SYNTHETIC DATA - not a real banking document"])
        w.writerow(["Date", "Value date", "Narration", "Reference", "Debit (INR)", "Credit (INR)", "Balance (INR)"])
        for r in rows:
            posted = fmt_ist(r["posted_at"])[:10] if r.get("posted_at") else fmt_date(r["value_date"])
            w.writerow([posted, fmt_date(r["value_date"]), _csv_cell(r["narration"]),
                        _csv_cell(r["posting_ref"]), _rupees(r["amount_paise"]) if r["dr_cr"] == "DR" else "",
                        _rupees(r["amount_paise"]) if r["dr_cr"] == "CR" else "", _rupees(r["balance_after_paise"])])
        data = buf.getvalue().encode("utf-8-sig")
        media = "text/csv; charset=utf-8"
        file_name = base_name + ".csv"
        headers = {"Content-Disposition": f'attachment; filename="{file_name}"'}
    else:
        password = statement_password(customer)
        table_rows = [[fmt_date(r["value_date"]), r["narration"][:48], format_inr(r["amount_paise"]) if r["dr_cr"] == "DR" else "",
                       format_inr(r["amount_paise"]) if r["dr_cr"] == "CR" else "", format_inr(r["balance_after_paise"])] for r in rows]
        sections = [
            Section("Account details", [
                f"Account holder: {customer['full_name']}",
                f"Account: {acct['masked_number']} ({TYPE_LABELS.get(acct['account_type'], acct['account_type'])}), IFSC {acct['ifsc']}",
                f"Statement period: {fmt_date(d_from)} to {fmt_date(d_to)}",
                f"Opening balance: {format_inr(opening)}    Closing balance: {format_inr(closing)}",
                f"Total debits: {format_inr(total_dr)}    Total credits: {format_inr(total_cr)}    Transactions: {len(rows)}",
            ]),
            Section("Transactions", [] if rows else ["No transactions in this period."],
                    table=(["Date", "Narration", "Debit", "Credit", "Balance"], table_rows) if rows else None,
                    widths=[22, 74, 30, 30, 34]),
        ]
        data = make_pdf("Account statement", sections, password=password, mock=mock,
                        footer_note="Balances are as per the bank's core banking system (Finacle). Report any discrepancy within "
                                    "30 days. Never share your OTP, PIN or password.")
        media = "application/pdf"
        file_name = base_name + ".pdf"
        headers = {"Content-Disposition": f'attachment; filename="{file_name}"', "X-Statement-Password-Hint": PASSWORD_HINT}
    audit_ctx(conn, ctx, "financial", "statement.downloaded", entity_type="account", entity_id=acct["id"],
              details={"format": fmt, "from": d_from.isoformat(), "to": d_to.isoformat(), "rows": len(rows)}, request=request)
    return Response(content=data, media_type=media, headers=headers)


@router.get("/me/accounts/statement-password-rule")
def statement_password_rule(ctx: AuthContext = Depends(require_customer)):
    """The PDF password rule (shown in the UI; the password itself is never returned)."""
    return {"rule": PASSWORD_HINT, "example": "ANAN1204"}

"""Cheque services: Positive Pay confirmations, cheque book requests and stop-payment instructions.

Positive Pay details are shared with CTS clearing (mock) so a presented cheque can be
matched against what the drawer confirmed. A cheque is valid for 3 months from its date
(CTS), so confirmations for older or future-dated cheques are refused.
"""
from __future__ import annotations

import calendar
import re
import sqlite3
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from audit import audit_ctx
from banking import customer_for, owned_account
from core import (api_error, fmt_date, format_inr, get_reg_value, invalid, new_id, now_iso, parse_date, reference_no,
                  today_ist)
from db import get_db
from integrations.base import is_mock, mock_meta
from notify import notify
from security import AuthContext, consume_mfa_token, require_customer

router = APIRouter(tags=["cheques"])

CHEQUE_RE = re.compile(r"^[0-9]{6}$")
CHEQUE_ACCOUNT_TYPES = ("savings", "current", "nro", "nre")
LEAVES = (10, 25, 50)
MAX_STOP_RANGE = 100


class Body(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class PositivePayIn(Body):
    account_id: Optional[str] = None
    cheque_number: Optional[str] = None
    cheque_date: Optional[str] = None
    amount_paise: Optional[int] = None
    payee_name: Optional[str] = None
    mfa_token: Optional[str] = None


class ChequeRequestIn(Body):
    request_type: Optional[str] = None
    account_id: Optional[str] = None
    leaves: Optional[int] = None
    delivery_address: Optional[str] = None
    cheque_from: Optional[str] = None
    cheque_to: Optional[str] = None
    reason: Optional[str] = None
    mfa_token: Optional[str] = None


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _cheque_account(conn: sqlite3.Connection, ctx: AuthContext, account_id: str | None) -> dict:
    if not account_id:
        raise invalid("account_id", "Choose the account the cheque is drawn on")
    acct = owned_account(conn, ctx.customer_id, account_id)
    if acct["account_type"] not in CHEQUE_ACCOUNT_TYPES:
        raise invalid("account_id", "Cheque services are available on savings and current accounts")
    return acct


def _masked(conn: sqlite3.Connection, account_id: str) -> str | None:
    row = conn.execute("SELECT masked_number FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return row["masked_number"] if row else None


def _public_pp(conn: sqlite3.Connection, r: dict) -> dict:
    return {**{k: r[k] for k in ("id", "account_id", "reference_no", "cheque_number", "cheque_date", "amount_paise", "payee_name",
                                 "status", "created_at")},
            "account_masked": _masked(conn, r["account_id"])}


def _public_req(conn: sqlite3.Connection, r: dict) -> dict:
    return {**{k: r[k] for k in ("id", "account_id", "reference_no", "request_type", "leaves", "cheque_from", "cheque_to", "reason",
                                 "delivery_address", "status", "created_at")},
            "account_masked": _masked(conn, r["account_id"])}


# ------------------------------------------------------------------ Positive Pay
@router.post("/me/positive-pay", status_code=201)
def positive_pay(body: PositivePayIn, request: Request, ctx: AuthContext = Depends(require_customer),
                 conn: sqlite3.Connection = Depends(get_db)):
    acct = _cheque_account(conn, ctx, body.account_id)
    cheque_no = (body.cheque_number or "").strip()
    if not CHEQUE_RE.match(cheque_no):
        raise invalid("cheque_number", "Enter the 6-digit cheque number printed at the bottom of the cheque")
    cdate = parse_date(body.cheque_date)
    if not cdate:
        raise invalid("cheque_date", "Enter the cheque date")
    today = today_ist()
    if cdate > today:
        raise invalid("cheque_date", "Positive Pay is for cheques already issued. The cheque date cannot be in the future.")
    if cdate < add_months(today, -3):
        raise invalid("cheque_date", f"This cheque is older than 3 months (dated before {fmt_date(add_months(today, -3))}) "
                                     "and is no longer valid for clearing.")
    threshold = int(get_reg_value(conn, "positive_pay_threshold_paise", 5000000) or 5000000)
    amount = body.amount_paise
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise invalid("amount_paise", "Enter the cheque amount")
    if amount < threshold:
        raise api_error(422, "below_threshold", f"Positive Pay applies to cheques of {format_inr(threshold)} and above",
                        field="amount_paise", threshold_paise=threshold)
    payee = re.sub(r"\s+", " ", body.payee_name or "").strip()
    if not (2 <= len(payee) <= 100):
        raise invalid("payee_name", "Enter the payee name exactly as written on the cheque (2 to 100 characters)")
    if conn.execute("SELECT 1 FROM positive_pay_entries WHERE account_id = ? AND cheque_number = ?", (acct["id"], cheque_no)).fetchone():
        raise api_error(409, "duplicate_cheque", f"Cheque {cheque_no} is already registered for Positive Pay on this account",
                        field="cheque_number")
    consume_mfa_token(conn, ctx, body.mfa_token, "positive_pay")
    row = {"id": new_id(), "account_id": acct["id"], "customer_id": ctx.customer_id, "reference_no": reference_no("PPY"),
           "cheque_number": cheque_no, "cheque_date": cdate.isoformat(), "amount_paise": amount, "payee_name": payee,
           "status": "sent_to_clearing", "created_at": now_iso()}
    conn.execute(
        """INSERT INTO positive_pay_entries (id, account_id, customer_id, reference_no, cheque_number, cheque_date, amount_paise,
           payee_name, status, created_at) VALUES (:id, :account_id, :customer_id, :reference_no, :cheque_number, :cheque_date,
           :amount_paise, :payee_name, :status, :created_at)""",
        row,
    )
    notify(conn, ctx.user_id, "transaction", "Positive Pay registered",
           f"Cheque {cheque_no} for {format_inr(amount)} on {acct['masked_number']} confirmed for clearing. Ref {row['reference_no']}.")
    audit_ctx(conn, ctx, "financial", "positive_pay.submitted", entity_type="positive_pay_entry", entity_id=row["id"],
              request=request, details={"account": acct["masked_number"], "cheque_number": cheque_no, "amount_paise": amount,
                                        "reference_no": row["reference_no"]})
    return {**_public_pp(conn, row), "mock": mock_meta("finacle") if is_mock(conn, "finacle") else None}


@router.get("/me/positive-pay")
def list_positive_pay(ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM positive_pay_entries WHERE customer_id = ? ORDER BY created_at DESC, rowid DESC", (ctx.customer_id,))]
    threshold = int(get_reg_value(conn, "positive_pay_threshold_paise", 5000000) or 5000000)
    return {"items": [_public_pp(conn, r) for r in rows], "threshold_paise": threshold}


# ------------------------------------------------------------------ cheque book & stop payment
@router.post("/me/cheque-requests", status_code=201)
def cheque_request(body: ChequeRequestIn, request: Request, ctx: AuthContext = Depends(require_customer),
                   conn: sqlite3.Connection = Depends(get_db)):
    if body.request_type not in ("cheque_book", "stop_payment"):
        raise invalid("request_type", "Choose a cheque book request or a stop-payment instruction")
    acct = _cheque_account(conn, ctx, body.account_id)
    row = {"id": new_id(), "account_id": acct["id"], "customer_id": ctx.customer_id, "request_type": body.request_type,
           "leaves": None, "cheque_from": None, "cheque_to": None, "reason": None, "delivery_address": None, "created_at": now_iso()}
    if body.request_type == "cheque_book":
        if body.leaves not in LEAVES:
            raise invalid("leaves", "Choose 10, 25 or 50 leaves")
        address = re.sub(r"\s+", " ", body.delivery_address or "").strip()
        if not address:
            address = customer_for(conn, ctx.customer_id).get("address") or ""  # type: ignore[arg-type]
            if not address:
                raise invalid("delivery_address", "Enter the delivery address")
        elif not (10 <= len(address) <= 300):
            raise invalid("delivery_address", "Enter the full delivery address (10 to 300 characters)")
        row.update(reference_no=reference_no("CHQ"), leaves=body.leaves, delivery_address=address, status="submitted")
        detail = f"{body.leaves}-leaf cheque book requested for {acct['masked_number']}. Ref {row['reference_no']}."
        action = "cheque_book.requested"
    else:
        cfrom, cto = (body.cheque_from or "").strip(), (body.cheque_to or body.cheque_from or "").strip()
        if not CHEQUE_RE.match(cfrom):
            raise invalid("cheque_from", "Enter the 6-digit cheque number")
        if not CHEQUE_RE.match(cto):
            raise invalid("cheque_to", "Enter the 6-digit cheque number")
        if int(cto) < int(cfrom):
            raise invalid("cheque_to", "The last cheque number must be the same as or after the first")
        if int(cto) - int(cfrom) + 1 > MAX_STOP_RANGE:
            raise invalid("cheque_to", f"Stop payment covers at most {MAX_STOP_RANGE} leaves per request")
        reason = re.sub(r"\s+", " ", body.reason or "").strip()
        if not (3 <= len(reason) <= 200):
            raise invalid("reason", "Tell us why you are stopping payment (3 to 200 characters)")
        consume_mfa_token(conn, ctx, body.mfa_token, "stop_payment")
        row.update(reference_no=reference_no("CHQ"), cheque_from=cfrom, cheque_to=cto, reason=reason, status="processed")
        rng = cfrom if cfrom == cto else f"{cfrom}-{cto}"
        detail = f"Stop payment recorded for cheque(s) {rng} on {acct['masked_number']}. Ref {row['reference_no']}."
        action = "stop_payment.recorded"
    conn.execute(
        """INSERT INTO cheque_service_requests (id, account_id, customer_id, reference_no, request_type, leaves, cheque_from,
           cheque_to, reason, delivery_address, status, created_at) VALUES (:id, :account_id, :customer_id, :reference_no,
           :request_type, :leaves, :cheque_from, :cheque_to, :reason, :delivery_address, :status, :created_at)""",
        row,
    )
    notify(conn, ctx.user_id, "security" if body.request_type == "stop_payment" else "transaction",
           "Stop payment recorded" if body.request_type == "stop_payment" else "Cheque book requested", detail)
    audit_ctx(conn, ctx, "financial", action, entity_type="cheque_service_request", entity_id=row["id"], request=request,
              details={"account": acct["masked_number"], "reference_no": row["reference_no"], "leaves": row["leaves"],
                       "cheque_from": row["cheque_from"], "cheque_to": row["cheque_to"]})
    return {**_public_req(conn, row), "mock": mock_meta("finacle") if is_mock(conn, "finacle") else None}


@router.get("/me/cheque-requests")
def list_cheque_requests(ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM cheque_service_requests WHERE customer_id = ? ORDER BY created_at DESC, rowid DESC", (ctx.customer_id,))]
    return {"items": [_public_req(conn, r) for r in rows]}

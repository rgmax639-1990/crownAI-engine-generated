"""Fund transfers (US-19): idempotent, Finacle-debited, sent on IMPS / NEFT / RTGS / UPI.

Flow for POST /me/transfers (transactional outbox pattern):
  1. Idempotency: the same (customer, idempotency key) with the same request returns the
     original transfer (``replayed: true``); a different request with that key is 409.
  2. The transfer row (status ``initiated``) and an outbox row are committed first.
  3. Finacle debit with ``idempotency_ref = "TRF-" + transfer id``: a retry can never post twice.
     The debit is committed before the rail is called.
  4. The rail is called once. success -> ``success``; accepted but not yet settled ->
     ``pending``; rejected -> the debit is reversed and the transfer is ``failed``; timeout ->
     ``pending`` with ``rail_timeout_awaiting_confirmation`` and **no blind retry**: a status
     enquiry (GET /me/transfers/{id}) or a signed rail callback settles it later.

Rail callbacks (POST /internal/rail-callbacks/{rail}) must carry ``X-ESB-Signature`` =
hex HMAC-SHA256 of the raw body with ``RAIL_CALLBACK_SECRET``. Reconciliation
(``run_reconciliation``) compares transfers with Finacle postings and records exceptions.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
from datetime import timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict

import seed_transfers  # noqa: F401  (registers the synthetic payments seed, order 60)
from audit import audit, audit_ctx
from banking import account_number, customer_for, in_cooling_period, owned_account
from core import (APP_ENV, api_error, fmt_ist, format_inr, get_reg_value, invalid, iso, jdump, jload, new_id, not_found,
                  now_iso, one, parse_date, parse_iso, positive_paise, request_fingerprint, utcnow)
from db import commit_now, get_db
from integrations import finacle, neft_rtgs, npci_imps, npci_upi
from integrations.base import IntegrationError, IntegrationNotConfigured, IntegrationNotImplemented, mock_meta
from integrations.finacle import FinacleBusinessError
from integrations.npci_imps import RailTimeout
from notify import notify, outbox
from routers.beneficiaries import (beneficiary_account_number, cooling_config, cooling_remaining_paise, mask_vpa,
                                   owned_beneficiary)
from security import AuthContext, consume_mfa_token, require_customer

router = APIRouter(tags=["transfers"])

RAILS = ("IMPS", "NEFT", "RTGS", "UPI")
DEBIT_ACCOUNT_TYPES = ("savings", "current", "nre", "nro")
FINAL_STATUSES = ("success", "failed", "reversed")
OPEN_STATUSES = ("initiated", "pending")
TIMEOUT_REASON = "rail_timeout_awaiting_confirmation"
IDEM_RE = re.compile(r"^[A-Za-z0-9_\-:.]{8,100}$")
DEV_CALLBACK_SECRET = "dev-rail-callback-secret"

RAIL_INFO = {
    "IMPS": {"label": "IMPS", "timing": "Instant, 24x7 including holidays.", "settlement": "instant",
             "payee_types": ["bank_account"]},
    "NEFT": {"label": "NEFT", "timing": "24x7 in half-hourly batches; the credit is confirmed when the batch settles.",
             "settlement": "batch", "payee_types": ["bank_account"]},
    "RTGS": {"label": "RTGS", "timing": "Real time, 24x7, for ₹2 lakh and above.", "settlement": "real_time",
             "payee_types": ["bank_account"]},
    "UPI": {"label": "UPI", "timing": "Instant, 24x7, to a UPI ID (VPA).", "settlement": "instant", "payee_types": ["vpa"]},
}


class Body(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class TransferIn(Body):
    from_account_id: Optional[str] = None
    beneficiary_id: Optional[str] = None
    rail: Optional[str] = None
    amount_paise: Any = None
    remarks: Optional[str] = None
    idempotency_key: Optional[str] = None
    mfa_token: Optional[str] = None


# ------------------------------------------------------------------ helpers
def rail_adapter(conn: sqlite3.Connection, rail: str):
    if rail == "IMPS":
        return npci_imps.get(conn)
    if rail == "UPI":
        return npci_upi.get(conn)
    return neft_rtgs.get(conn)


def rail_limits(conn: sqlite3.Connection) -> dict:
    def num(key: str, default: int) -> int:
        try:
            return int(get_reg_value(conn, key, default))
        except (TypeError, ValueError):
            return default

    return {
        "imps_max_paise": num("imps_max_paise", 50_000_000),
        "upi_max_paise": num("upi_max_paise", 10_000_000),
        "rtgs_min_paise": num("rtgs_min_paise", 20_000_000),
    }


def user_id_for_customer(conn: sqlite3.Connection, customer_id: str | None) -> str | None:
    if not customer_id:
        return None
    row = conn.execute("SELECT id FROM users WHERE customer_id = ? AND user_type = 'retail' ORDER BY created_at LIMIT 1",
                       (customer_id,)).fetchone()
    return row["id"] if row else None


def _transfer_row(conn: sqlite3.Connection, transfer_id: str) -> dict | None:
    return one(
        conn,
        """SELECT t.*, a.masked_number AS from_masked, a.nickname AS from_nickname, b.name AS ben_name,
                  b.account_masked AS ben_account_masked, b.vpa AS ben_vpa, b.ifsc AS ben_ifsc, b.payee_type AS ben_payee_type
           FROM transfers t LEFT JOIN accounts a ON a.id = t.from_account_id LEFT JOIN beneficiaries b ON b.id = t.beneficiary_id
           WHERE t.id = ?""",
        (transfer_id,),
    )


def status_message(t: dict) -> str:
    st, rail = t["status"], t["rail"]
    if st == "initiated":
        return "Your transfer is being processed."
    if st == "pending":
        if t.get("failure_reason") == TIMEOUT_REASON:
            return (f"The {rail} network did not confirm in time. Your account has been debited and we will not send the money "
                    "again automatically. The payment is confirmed or reversed after a status check with the network. "
                    "Please do not pay again.")
        if rail in ("NEFT", "RTGS"):
            return (f"Sent on {rail}. The credit to the beneficiary is confirmed when the "
                    f"{'NEFT batch' if rail == 'NEFT' else 'RTGS settlement'} completes. Use the UTR to track it.")
        return f"Sent on {rail}; waiting for confirmation from the beneficiary bank."
    if st == "success":
        return "Credited to the beneficiary."
    if st == "reversed":
        return "This payment was reversed after reconciliation. The amount is back in your account."
    return f"The transfer failed: {t.get('failure_reason') or 'rejected'}. Any debit has been reversed to your account."


def public_transfer(t: dict) -> dict:
    payee_type = t.get("ben_payee_type")
    return {
        "id": t["id"],
        "rail": t["rail"],
        "amount_paise": t["amount_paise"],
        "remarks": t.get("remarks"),
        "status": t["status"],
        "status_message": status_message(t),
        "failure_reason": t.get("failure_reason"),
        "awaiting_confirmation": t["status"] == "pending",
        "rail_reference": t.get("rail_reference"),
        "reference_label": "UTR" if t["rail"] in ("NEFT", "RTGS") else "RRN",
        "finacle_posting_ref": t.get("finacle_posting_ref"),
        "from_account": {"id": t["from_account_id"], "masked_number": t.get("from_masked"), "nickname": t.get("from_nickname")},
        "beneficiary": {
            "id": t.get("beneficiary_id"),
            "name": t.get("ben_name"),
            "payee_type": payee_type,
            "display_id": t.get("ben_account_masked") if payee_type == "bank_account" else mask_vpa(t.get("ben_vpa")),
            "ifsc": t.get("ben_ifsc"),
        },
        "timing_note": RAIL_INFO.get(t["rail"], {}).get("timing"),
        "idempotency_key": t.get("idempotency_key"),
        "created_at": t["created_at"],
        "updated_at": t["updated_at"],
        "settled_at": t.get("settled_at"),
    }


def _notify_transfer(conn: sqlite3.Connection, t: dict) -> None:
    uid = user_id_for_customer(conn, t["customer_id"])
    amount = format_inr(t["amount_paise"])
    acct = t.get("from_masked") or "your account"
    ref = t.get("rail_reference") or t["id"][:8].upper()
    ben = t.get("ben_name") or "the beneficiary"
    st = t["status"]
    if st == "success":
        title, body = "Amount debited", f"{amount} sent from A/c {acct} to {ben} via {t['rail']}. Ref {ref}."
    elif st == "pending":
        title, body = "Transfer in progress", f"{amount} debited from A/c {acct} to {ben} via {t['rail']}; awaiting confirmation. Ref {ref}."
    elif st == "reversed":
        title, body = "Transfer reversed", f"{amount} for {ben} via {t['rail']} was reversed to A/c {acct}. Ref {ref}."
    else:
        title, body = "Transfer failed", f"{amount} to {ben} via {t['rail']} failed. Any debit is reversed to A/c {acct}. Ref {ref}."
    notify(conn, uid, "transaction", title, body + " Not you? Report fraud immediately or call 1930.")


def _reverse_debit(conn: sqlite3.Connection, t: dict, narration: str) -> dict | None:
    """Reverse the Finacle debit of a transfer if there is one (idempotent: REV-<posting_ref>)."""
    core = finacle.get(conn)
    posting_ref = t.get("finacle_posting_ref")
    if not posting_ref:
        posting = core.find_posting(f"TRF-{t['id']}")
        posting_ref = posting["posting_ref"] if posting else None
    if not posting_ref:
        return None
    try:
        return core.reverse(posting_ref, narration)
    except FinacleBusinessError as exc:
        if exc.code == "posting_not_found":
            return None
        raise


def settle_transfer(conn: sqlite3.Connection, transfer_id: str, outcome: str, *, reason: str | None = None,
                    rail_reference: str | None = None, source: str = "rail", final_status: str | None = None) -> dict:
    """Apply a rail outcome (success / failed / pending) to an open transfer. Final transfers are left alone."""
    t = _transfer_row(conn, transfer_id)
    if not t:
        raise not_found("Transfer")
    if t["status"] in FINAL_STATUSES and source != "reconciliation":
        return t
    now = now_iso()
    if rail_reference and not t.get("rail_reference"):
        clash = one(conn, "SELECT id FROM transfers WHERE rail_reference = ? AND id != ?", (rail_reference, t["id"]))
        if not clash:
            conn.execute("UPDATE transfers SET rail_reference = ? WHERE id = ?", (rail_reference, t["id"]))
    if outcome == "success":
        conn.execute("UPDATE transfers SET status = 'success', settled_at = COALESCE(settled_at, ?), failure_reason = NULL, "
                     "updated_at = ? WHERE id = ?", (now, now, t["id"]))
    elif outcome == "failed":
        _reverse_debit(conn, t, f"REV {t['rail']} {t['id'][:8].upper()} {(reason or 'failed')[:60]}")
        conn.execute("UPDATE transfers SET status = ?, failure_reason = ?, updated_at = ? WHERE id = ?",
                     (final_status or "failed", (reason or "rejected_by_rail")[:200], now, t["id"]))
    elif outcome == "pending":
        if reason:
            conn.execute("UPDATE transfers SET status = 'pending', failure_reason = ?, updated_at = ? WHERE id = ?",
                         (reason, now, t["id"]))
        else:
            conn.execute("UPDATE transfers SET status = 'pending', updated_at = ? WHERE id = ?", (now, t["id"]))
    else:
        return t
    t = _transfer_row(conn, t["id"])
    outbox(conn, f"transfer.{t['status']}", {"transfer_id": t["id"], "status": t["status"], "source": source})
    if t["status"] in FINAL_STATUSES and source not in ("initiation", "reconciliation"):  # those callers alert themselves
        _notify_transfer(conn, t)
        audit(conn, "financial", f"transfer.{t['status']}", actor_type="system", entity_type="transfer", entity_id=t["id"],
              details={"source": source, "rail": t["rail"], "rail_reference": t.get("rail_reference"),
                       "amount_paise": t["amount_paise"], "reason": reason})
    return t  # type: ignore[return-value]


def enquire_status(conn: sqlite3.Connection, t: dict) -> dict:
    """Ask the rail what happened to an open transfer and apply the answer."""
    if t["status"] not in OPEN_STATUSES:
        return t
    try:
        res = rail_adapter(conn, t["rail"]).status(t.get("rail_reference"), payment_id=t["id"])
    except IntegrationError:
        return t  # try again later; nothing changes
    st = res.get("status")
    if st == "success":
        return settle_transfer(conn, t["id"], "success", rail_reference=res.get("rail_reference"), source="status_enquiry")
    if st == "failed":
        return settle_transfer(conn, t["id"], "failed", reason=res.get("reason") or "rejected_by_rail",
                               rail_reference=res.get("rail_reference"), source="status_enquiry")
    if st == "not_found" and t["status"] == "pending" and t.get("failure_reason") == TIMEOUT_REASON:
        return settle_transfer(conn, t["id"], "failed", reason="not_received_by_rail", source="status_enquiry")
    if st == "not_found" and t["status"] == "initiated":
        started = parse_iso(t["created_at"])
        if started and started < utcnow() - timedelta(minutes=5):
            # Interrupted before the rail was called: the money never left, so the debit is reversed.
            return settle_transfer(conn, t["id"], "failed", reason="not_sent_to_rail", source="status_enquiry")
    if res.get("rail_reference") and not t.get("rail_reference"):
        return settle_transfer(conn, t["id"], "pending", rail_reference=res.get("rail_reference"), source="status_enquiry")
    return t


# ------------------------------------------------------------------ options
@router.get("/me/transfers/options")
def transfer_options(ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    """Rails with limits and timing hints, and the beneficiary cooling rule (for the transfer form)."""
    limits = rail_limits(conn)
    cfg = cooling_config(conn)
    customer = customer_for(conn, ctx.customer_id)
    rails = []
    for code, info in RAIL_INFO.items():
        rails.append({
            "rail": code, **info,
            "min_paise": limits["rtgs_min_paise"] if code == "RTGS" else 100,
            "max_paise": limits["imps_max_paise"] if code == "IMPS" else limits["upi_max_paise"] if code == "UPI" else None,
        })
    return {
        "rails": rails,
        "limits": limits,
        "cooling_hours": cfg["cooling_hours"],
        "cooling_limit_paise": cfg["cooling_limit_paise"],
        "customer_cooling_until": customer.get("high_risk_cooling_until") if in_cooling_period(customer) else None,
        "debit_account_types": list(DEBIT_ACCOUNT_TYPES),
    }


# ------------------------------------------------------------------ create
@router.post("/me/transfers", status_code=202)
def create_transfer(body: TransferIn, request: Request, response: Response,
                    idempotency_header: Optional[str] = Header(None, alias="Idempotency-Key"),
                    ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    key = (body.idempotency_key or idempotency_header or "").strip()
    if not IDEM_RE.match(key):
        raise invalid("idempotency_key", "A unique idempotency key (8 to 100 letters, digits, - _ : .) is required")
    rail = (body.rail or "").strip().upper()
    remarks = re.sub(r"\s+", " ", body.remarks or "").strip()
    fingerprint = request_fingerprint({
        "from_account_id": body.from_account_id, "beneficiary_id": body.beneficiary_id, "rail": rail,
        "amount_paise": body.amount_paise, "remarks": remarks,
    })
    existing = one(conn, "SELECT id, request_hash FROM transfers WHERE customer_id = ? AND idempotency_key = ?", (ctx.customer_id, key))
    if existing:
        if existing["request_hash"] != fingerprint:
            raise api_error(409, "idempotency_conflict",
                            "This idempotency key was already used for a different transfer. Start a new transfer.")
        response.status_code = 200
        return {**public_transfer(_transfer_row(conn, existing["id"])), "replayed": True}  # type: ignore[arg-type]

    # ---------------------------------------------------------------- validation
    if rail not in RAILS:
        raise invalid("rail", "Choose IMPS, NEFT, RTGS or UPI")
    if not body.from_account_id:
        raise invalid("from_account_id", "Choose the account to pay from")
    acct = owned_account(conn, ctx.customer_id, body.from_account_id)
    if acct["account_type"] not in DEBIT_ACCOUNT_TYPES or acct["currency"] != "INR":
        raise invalid("from_account_id", "Transfers can be made only from a savings, current, NRE or NRO account in rupees")
    if not body.beneficiary_id:
        raise invalid("beneficiary_id", "Choose a beneficiary")
    ben = owned_beneficiary(conn, ctx.customer_id, body.beneficiary_id)
    if ben["status"] == "pending_otp":
        raise api_error(422, "beneficiary_not_confirmed", "Confirm this beneficiary with the OTP before sending money.",
                        field="beneficiary_id")
    if rail == "UPI" and ben["payee_type"] != "vpa":
        raise invalid("rail", "UPI can be used only for a UPI ID beneficiary. Choose IMPS, NEFT or RTGS.")
    if rail != "UPI" and ben["payee_type"] != "bank_account":
        raise invalid("rail", "This beneficiary is a UPI ID. Choose UPI.")
    amount = positive_paise(body.amount_paise, "amount_paise")
    if len(remarks) > 50:
        raise invalid("remarks", "Remarks can be at most 50 characters")
    if remarks and not re.fullmatch(r"[A-Za-z0-9 .,/\-_&()']*", remarks):
        raise invalid("remarks", "Remarks can contain letters, digits, spaces and . , / - _ & ( ) '")
    limits = rail_limits(conn)
    if rail == "IMPS" and amount > limits["imps_max_paise"]:
        raise invalid("amount_paise", f"IMPS allows up to {format_inr(limits['imps_max_paise'])} per transfer. Use NEFT or RTGS.")
    if rail == "UPI" and amount > limits["upi_max_paise"]:
        raise invalid("amount_paise", f"UPI allows up to {format_inr(limits['upi_max_paise'])} per transfer.")
    if rail == "RTGS" and amount < limits["rtgs_min_paise"]:
        raise invalid("amount_paise", f"RTGS is for {format_inr(limits['rtgs_min_paise'])} and above. Use IMPS or NEFT.")
    if ben["status"] == "cooling":
        remaining = cooling_remaining_paise(conn, ben) or 0
        if amount > remaining:
            raise api_error(422, "cooling_limit_exceeded",
                            f"This beneficiary was added recently. Until {fmt_ist(ben['cooling_until'])} you can send at most "
                            f"{format_inr(remaining)} more to them.",
                            field="amount_paise", remaining_paise=remaining, cooling_until=ben["cooling_until"])
    customer = customer_for(conn, ctx.customer_id)
    if in_cooling_period(customer):
        cap = cooling_config(conn)["cooling_limit_paise"]
        since = iso(utcnow() - timedelta(hours=24))
        used = conn.execute(
            "SELECT COALESCE(SUM(amount_paise), 0) FROM transfers WHERE customer_id = ? AND status IN ('initiated','pending','success') "
            "AND created_at >= ?", (ctx.customer_id, since)).fetchone()[0]
        remaining = max(0, cap - int(used or 0))
        if amount > remaining:
            raise api_error(422, "cooling_limit_exceeded",
                            f"Your contact details were changed recently, so transfers are limited to {format_inr(cap)} in 24 hours "
                            f"until {fmt_ist(customer['high_risk_cooling_until'])}. You can send up to {format_inr(remaining)} now.",
                            field="amount_paise", remaining_paise=remaining, cooling_until=customer["high_risk_cooling_until"],
                            reason="profile_change_cooling")

    consume_mfa_token(conn, ctx, body.mfa_token, f"transfer:{key}")

    # ---------------------------------------------------------------- 1. intent + outbox (committed)
    tid = new_id()
    now = now_iso()
    conn.execute(
        """INSERT INTO transfers (id, customer_id, from_account_id, beneficiary_id, rail, amount_paise, remarks, idempotency_key,
           request_hash, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,'initiated',?,?)""",
        (tid, ctx.customer_id, acct["id"], ben["id"], rail, amount, remarks or None, key, fingerprint, now, now),
    )
    outbox(conn, "transfer.initiated", {"transfer_id": tid, "rail": rail, "amount_paise": amount})
    audit_ctx(conn, ctx, "financial", "transfer.initiated", entity_type="transfer", entity_id=tid,
              details={"rail": rail, "amount_paise": amount, "beneficiary_id": ben["id"], "from_account": acct["masked_number"]},
              request=request)
    commit_now(conn)

    # ---------------------------------------------------------------- 2. Finacle debit (idempotent)
    core = finacle.get(conn)
    payee_label = ben["account_masked"] if ben["payee_type"] == "bank_account" else mask_vpa(ben["vpa"])
    narration = f"{rail}/DR/{tid[:8].upper()}/{ben['name'][:30]}/{remarks[:30]}"
    try:
        posting = core.post(account_number(acct), "DR", amount, narration, idempotency_ref=f"TRF-{tid}", channel_ref=tid)
    except FinacleBusinessError as exc:
        conn.execute("UPDATE transfers SET status = 'failed', failure_reason = ?, updated_at = ? WHERE id = ?",
                     (exc.code, now_iso(), tid))
        outbox(conn, "transfer.failed", {"transfer_id": tid, "reason": exc.code})
        audit_ctx(conn, ctx, "financial", "transfer.debit_rejected", entity_type="transfer", entity_id=tid,
                  details={"code": exc.code}, request=request)
        commit_now(conn)
        raise
    conn.execute("UPDATE transfers SET finacle_posting_ref = ?, updated_at = ? WHERE id = ?", (posting["posting_ref"], now_iso(), tid))
    commit_now(conn)

    # ---------------------------------------------------------------- 3. rail (called once, never blindly retried)
    if ben["payee_type"] == "vpa":
        payee: dict = {"vpa": ben["vpa"], "name": ben["name"]}
    else:
        payee = {"account_number": beneficiary_account_number(ben), "ifsc": ben["ifsc"], "name": ben["name"]}
    try:
        res = rail_adapter(conn, rail).initiate(payment_id=tid, amount_paise=amount, remarks=remarks, payee=payee, rail=rail)
    except RailTimeout:
        t = settle_transfer(conn, tid, "pending", reason=TIMEOUT_REASON, source="initiation")
    except (IntegrationNotConfigured, IntegrationNotImplemented) as exc:
        # The request never left the bank: safe to reverse.
        t = settle_transfer(conn, tid, "failed", reason=f"rail_unavailable: {exc.message[:120]}", source="initiation")
    except IntegrationError:
        # Outcome unknown: treat like a timeout (no retry, confirm by enquiry).
        t = settle_transfer(conn, tid, "pending", reason=TIMEOUT_REASON, source="initiation")
    else:
        if not res.get("accepted"):
            t = settle_transfer(conn, tid, "failed", reason=res.get("reason") or "rejected_by_rail",
                                rail_reference=res.get("rail_reference"), source="initiation")
        elif res.get("status") == "success":
            t = settle_transfer(conn, tid, "success", rail_reference=res.get("rail_reference"), source="initiation")
        else:
            t = settle_transfer(conn, tid, "pending", rail_reference=res.get("rail_reference"), source="initiation")

    _notify_transfer(conn, t)
    audit_ctx(conn, ctx, "financial", f"transfer.{t['status']}", entity_type="transfer", entity_id=tid,
              details={"rail": rail, "amount_paise": amount, "rail_reference": t.get("rail_reference"), "payee": payee_label,
                       "posting_ref": posting["posting_ref"], "failure_reason": t.get("failure_reason")}, request=request)
    adapter = rail_adapter(conn, rail)
    return {**public_transfer(t), "replayed": False,
            "mock": mock_meta(adapter.provider) if getattr(adapter, "is_mock", False) else None}


# ------------------------------------------------------------------ history
@router.get("/me/transfers")
def list_transfers(status: Optional[str] = None, rail: Optional[str] = None, beneficiary_id: Optional[str] = None,
                   from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None, q: Optional[str] = None,
                   page: int = 1, page_size: int = 20,
                   ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    if page < 1:
        raise invalid("page", "Page must be 1 or more")
    if not (1 <= page_size <= 100):
        raise invalid("page_size", "Page size must be between 1 and 100")
    where = ["t.customer_id = ?"]
    params: list = [ctx.customer_id]
    if status:
        if status not in ("initiated", "pending", "success", "failed", "reversed"):
            raise invalid("status", "Unknown status")
        where.append("t.status = ?")
        params.append(status)
    if rail:
        if rail.upper() not in RAILS:
            raise invalid("rail", "Choose IMPS, NEFT, RTGS or UPI")
        where.append("t.rail = ?")
        params.append(rail.upper())
    if beneficiary_id:
        where.append("t.beneficiary_id = ?")
        params.append(beneficiary_id)
    d_from, d_to = parse_date(from_), parse_date(to)
    if from_ and not d_from:
        raise invalid("from", "Use DD/MM/YYYY or YYYY-MM-DD")
    if to and not d_to:
        raise invalid("to", "Use DD/MM/YYYY or YYYY-MM-DD")
    if d_from and d_to and d_to < d_from:
        raise invalid("to", "The end date must be on or after the start date")
    if d_from:
        where.append("t.created_at >= ?")
        params.append(iso(parse_iso(d_from.isoformat() + "T00:00:00")))
    if d_to:
        where.append("t.created_at < ?")
        params.append(iso(parse_iso((d_to + timedelta(days=1)).isoformat() + "T00:00:00")))
    if q:
        where.append("(b.name LIKE ? OR t.remarks LIKE ? OR t.rail_reference LIKE ?)")
        params.extend([f"%{q[:50]}%"] * 3)
    clause = " AND ".join(where)
    base = "FROM transfers t LEFT JOIN accounts a ON a.id = t.from_account_id LEFT JOIN beneficiaries b ON b.id = t.beneficiary_id"
    total = conn.execute(f"SELECT COUNT(*) {base} WHERE {clause}", params).fetchone()[0]
    rows = conn.execute(
        f"""SELECT t.*, a.masked_number AS from_masked, a.nickname AS from_nickname, b.name AS ben_name,
                   b.account_masked AS ben_account_masked, b.vpa AS ben_vpa, b.ifsc AS ben_ifsc, b.payee_type AS ben_payee_type
            {base} WHERE {clause} ORDER BY t.created_at DESC, t.id LIMIT ? OFFSET ?""",
        params + [page_size, (page - 1) * page_size],
    ).fetchall()
    return {"items": [public_transfer(dict(r)) for r in rows], "total": total, "page": page, "page_size": page_size}


@router.get("/me/transfers/{transfer_id}")
def get_transfer(transfer_id: str, ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    t = _transfer_row(conn, str(transfer_id)[:64])
    if not t:
        raise not_found("Transfer")
    if t["customer_id"] != ctx.customer_id:
        raise api_error(403, "forbidden", "This transfer does not belong to you")
    if t["status"] in OPEN_STATUSES:
        t = enquire_status(conn, t)
    return public_transfer(t)


# ------------------------------------------------------------------ rail callbacks
def _callback_secret() -> str | None:
    secret = os.getenv("RAIL_CALLBACK_SECRET")
    if secret:
        return secret
    return None if APP_ENV == "production" else DEV_CALLBACK_SECRET


def sign_callback(raw: bytes, secret: str | None = None) -> str:
    """Helper for tests and the mock ESB: hex HMAC-SHA256 of the raw body."""
    return hmac.new((secret or _callback_secret() or "").encode("utf-8"), raw, hashlib.sha256).hexdigest()


def _record_exception(conn: sqlite3.Connection, payment_type: str, payment_id: str, mismatch_type: str, *,
                      rail_reference: str | None, amount_paise: int | None, details: dict) -> bool:
    exists = conn.execute("SELECT 1 FROM reconciliation_exceptions WHERE payment_type = ? AND payment_id = ? AND mismatch_type = ?",
                          (payment_type, payment_id, mismatch_type)).fetchone()
    if exists:
        return False
    conn.execute(
        """INSERT INTO reconciliation_exceptions (id, payment_type, payment_id, mismatch_type, rail_reference, amount_paise, details,
           status, created_at) VALUES (?,?,?,?,?,?,?,'open',?)""",
        (new_id(), payment_type, payment_id, mismatch_type, rail_reference, amount_paise, jdump(details), now_iso()),
    )
    return True


@router.post("/internal/rail-callbacks/{rail}")
async def rail_callback(rail: str, request: Request, x_esb_signature: Optional[str] = Header(None, alias="X-ESB-Signature"),
                        conn: sqlite3.Connection = Depends(get_db)):
    raw = await request.body()
    secret = _callback_secret()
    if not secret or not x_esb_signature or not hmac.compare_digest(sign_callback(raw, secret), x_esb_signature.strip().lower()):
        raise api_error(401, "invalid_signature", "Callback signature is missing or invalid")
    rail = rail.upper()
    if rail not in RAILS:
        raise not_found("Rail")
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise invalid("body", "Callback body must be JSON")
    if not isinstance(payload, dict):
        raise invalid("body", "Callback body must be a JSON object")
    ref = str(payload.get("rail_reference") or "").strip()[:64]
    status = str(payload.get("status") or "").strip().lower()
    if not ref:
        raise invalid("rail_reference", "rail_reference is required")
    if status not in ("success", "failed"):
        raise invalid("status", "status must be success or failed")

    first = one(conn, "SELECT * FROM rail_callbacks WHERE rail_reference = ? AND duplicate = 0 ORDER BY received_at LIMIT 1", (ref,))
    cb_id = new_id()
    conn.execute("INSERT INTO rail_callbacks (id, rail, rail_reference, payload, duplicate, received_at) VALUES (?,?,?,?,?,?)",
                 (cb_id, rail, ref, jdump(payload), 1 if first else 0, now_iso()))
    t = one(conn, "SELECT * FROM transfers WHERE rail_reference = ? AND rail = ?", (ref,  rail))
    if not t and payload.get("payment_id"):
        t = one(conn, "SELECT * FROM transfers WHERE id = ? AND rail = ?", (str(payload["payment_id"])[:64], rail))

    if first:
        prev = jload(first["payload"], {}) or {}
        conflict = (str(prev.get("status", "")).lower() != status
                    or (payload.get("amount_paise") is not None and prev.get("amount_paise") is not None
                        and payload.get("amount_paise") != prev.get("amount_paise")))
        if conflict and t:
            _record_exception(conn, "transfer", t["id"], "duplicate_callback", rail_reference=ref, amount_paise=t["amount_paise"],
                              details={"first_callback": prev, "duplicate_callback": payload, "transfer_status": t["status"]})
        audit(conn, "integration", "rail_callback.duplicate", actor_type="system", entity_type="transfer",
              entity_id=t["id"] if t else None, details={"rail": rail, "rail_reference": ref, "conflict": conflict})
        return {"received": True, "duplicate": True, "conflict": conflict, "callback_id": cb_id}

    audit(conn, "integration", "rail_callback.received", actor_type="system", entity_type="transfer",
          entity_id=t["id"] if t else None, details={"rail": rail, "rail_reference": ref, "status": status})
    if not t:
        return {"received": True, "duplicate": False, "matched": False, "callback_id": cb_id}
    amt = payload.get("amount_paise")
    if isinstance(amt, int) and amt != t["amount_paise"]:
        _record_exception(conn, "transfer", t["id"], "amount_mismatch", rail_reference=ref, amount_paise=t["amount_paise"],
                          details={"rail_amount_paise": amt, "transfer_amount_paise": t["amount_paise"], "source": "callback"})
    if t["status"] in OPEN_STATUSES:
        t = settle_transfer(conn, t["id"], status, reason=str(payload.get("reason") or "")[:200] or None, rail_reference=ref,
                            source="callback")
    elif (t["status"] == "success") != (status == "success"):
        _record_exception(conn, "transfer", t["id"], "posting_no_rail_success" if status == "failed" else "rail_success_no_posting",
                          rail_reference=ref, amount_paise=t["amount_paise"],
                          details={"callback_status": status, "transfer_status": t["status"], "source": "callback"})
    return {"received": True, "duplicate": False, "matched": True, "transfer_id": t["id"], "transfer_status": t["status"],
            "callback_id": cb_id}


# ------------------------------------------------------------------ reconciliation
def posting_by_ref(conn: sqlite3.Connection, posting_ref: str | None) -> dict | None:
    if not posting_ref:
        return None
    core = finacle.get(conn)
    finder = getattr(core, "find_posting_by_ref", None)
    if callable(finder) and getattr(core, "is_mock", False) is False:
        return finder(posting_ref)
    row = conn.execute("SELECT * FROM mock_finacle_postings WHERE posting_ref = ?", (posting_ref,)).fetchone()
    return dict(row) if row else None


def posting_state(conn: sqlite3.Connection, payment_type: str, payment: dict) -> dict:
    """The Finacle side of a payment: the debit posting (if any) and whether it was reversed."""
    core = finacle.get(conn)
    posting = None
    if payment_type == "transfer":
        posting = core.find_posting(f"TRF-{payment['id']}")
    if posting is None:
        posting = posting_by_ref(conn, payment.get("finacle_posting_ref"))
    reversal = core.find_posting(f"REV-{posting['posting_ref']}") if posting else None
    return {
        "posted": bool(posting),
        "posting_ref": posting["posting_ref"] if posting else None,
        "amount_paise": posting["amount_paise"] if posting else None,
        "dr_cr": posting["dr_cr"] if posting else None,
        "posted_at": posting["posted_at"] if posting else None,
        "reversed": bool(reversal),
        "reversal_ref": reversal["posting_ref"] if reversal else None,
        "net_debited": bool(posting) and not reversal,
    }


SUCCESS_WORDS = {"success", "completed", "sent", "settled", "paid"}
FAILED_WORDS = {"failed", "rejected", "returned", "cancelled"}


def run_reconciliation(conn: sqlite3.Connection, *, max_rows: int = 5000) -> dict:
    """Compare payments with Finacle postings; create one exception per payment and mismatch type."""
    created = checked = 0
    since = iso(utcnow() - timedelta(days=90))
    for t in conn.execute("SELECT * FROM transfers WHERE created_at >= ? AND status IN ('success','failed') ORDER BY created_at DESC LIMIT ?",
                          (since, max_rows)).fetchall():
        t = dict(t)
        checked += 1
        ps = posting_state(conn, "transfer", t)
        details = {"transfer_status": t["status"], "rail": t["rail"], "finacle": ps}
        if t["status"] == "success" and not ps["net_debited"]:
            created += _record_exception(conn, "transfer", t["id"], "rail_success_no_posting", rail_reference=t["rail_reference"],
                                         amount_paise=t["amount_paise"], details=details)
        elif t["status"] == "failed" and ps["net_debited"]:
            created += _record_exception(conn, "transfer", t["id"], "posting_no_rail_success", rail_reference=t["rail_reference"],
                                         amount_paise=t["amount_paise"], details=details)
        if ps["posted"] and ps["amount_paise"] != t["amount_paise"]:
            created += _record_exception(conn, "transfer", t["id"], "amount_mismatch", rail_reference=t["rail_reference"],
                                         amount_paise=t["amount_paise"], details=details)
    for table, ptype, amount_col in (("bill_payments", "bill_payment", "amount_paise"), ("lrs_remittances", "lrs_remittance", "inr_amount_paise")):
        ref_col = "rail_reference" if table == "bill_payments" else "swift_uetr"
        for p in conn.execute(f"SELECT * FROM {table} WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?", (since, max_rows)).fetchall():
            p = dict(p)
            st = str(p.get("status") or "").lower()
            if st not in SUCCESS_WORDS and st not in FAILED_WORDS:
                continue
            checked += 1
            ps = posting_state(conn, ptype, p)
            details = {"payment_status": p["status"], "finacle": ps}
            if st in SUCCESS_WORDS and not ps["net_debited"]:
                created += _record_exception(conn, ptype, p["id"], "rail_success_no_posting", rail_reference=p.get(ref_col),
                                             amount_paise=p.get(amount_col), details=details)
            elif st in FAILED_WORDS and ps["net_debited"]:
                created += _record_exception(conn, ptype, p["id"], "posting_no_rail_success", rail_reference=p.get(ref_col),
                                             amount_paise=p.get(amount_col), details=details)
            if ps["posted"] and p.get(amount_col) is not None and ps["amount_paise"] != p.get(amount_col):
                created += _record_exception(conn, ptype, p["id"], "amount_mismatch", rail_reference=p.get(ref_col),
                                             amount_paise=p.get(amount_col), details=details)
    return {"checked": checked, "created": created, "run_at": now_iso()}


__all__ = ["router", "run_reconciliation", "settle_transfer", "enquire_status", "posting_state", "rail_adapter", "sign_callback",
           "user_id_for_customer", "public_transfer", "RAIL_INFO"]

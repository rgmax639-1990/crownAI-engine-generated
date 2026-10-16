"""Operations: payment reconciliation exceptions (rail status vs Finacle postings).

``ops_admin`` staff list mismatches, run reconciliation and resolve each exception as
``settled`` (the payment went through: make sure the customer's account is debited) or
``reversed`` (the payment did not go through: make sure any debit is reversed). Every
resolution needs a note, is audited and alerts the customer.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from audit import audit_ctx
from banking import account_number
from core import api_error, format_inr, invalid, jdump, jload, not_found, now_iso, one, parse_iso, utcnow
from db import get_db
from integrations import finacle
from integrations.base import IntegrationError
from notify import notify, outbox
from routers.transfers import posting_state, run_reconciliation, settle_transfer, user_id_for_customer
from security import AuthContext, require_staff

router = APIRouter(tags=["ops"])

MISMATCH_TYPES = ("rail_success_no_posting", "posting_no_rail_success", "amount_mismatch", "duplicate_callback")
MISMATCH_LABELS = {
    "rail_success_no_posting": "Rail success, no Finacle debit",
    "posting_no_rail_success": "Finacle debit, rail not successful",
    "amount_mismatch": "Amount mismatch",
    "duplicate_callback": "Duplicate / conflicting rail callback",
}
PAYMENT_TABLES = {"transfer": "transfers", "bill_payment": "bill_payments", "lrs_remittance": "lrs_remittances"}


class Body(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class ResolveIn(Body):
    resolution: str
    note: Optional[str] = None


def _age(created_at: str) -> dict:
    dt = parse_iso(created_at)
    secs = int((utcnow() - dt).total_seconds()) if dt else 0
    days, rem = divmod(max(0, secs), 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    label = f"{days}d {hours}h" if days else f"{hours}h {mins}m" if hours else f"{mins}m"
    return {"age_seconds": secs, "age_label": label}


def _payment(conn: sqlite3.Connection, payment_type: str, payment_id: str) -> dict | None:
    table = PAYMENT_TABLES.get(payment_type)
    if not table:
        return None
    return one(conn, f"SELECT * FROM {table} WHERE id = ?", (payment_id,))


def _payment_view(conn: sqlite3.Connection, payment_type: str, p: dict | None) -> dict | None:
    if not p:
        return None
    acct = one(conn, "SELECT masked_number FROM accounts WHERE id = ?", (p.get("from_account_id"),))
    cust = one(conn, "SELECT full_name FROM customers WHERE id = ?", (p.get("customer_id"),))
    view = {
        "id": p["id"],
        "status": p.get("status"),
        "customer_name": cust["full_name"] if cust else None,
        "from_account": acct["masked_number"] if acct else None,
        "created_at": p.get("created_at"),
        "failure_reason": p.get("failure_reason"),
        "finacle_posting_ref": p.get("finacle_posting_ref"),
    }
    if payment_type == "transfer":
        ben = one(conn, "SELECT name, account_masked, vpa, ifsc FROM beneficiaries WHERE id = ?", (p.get("beneficiary_id"),))
        view.update({
            "rail": p["rail"], "amount_paise": p["amount_paise"], "rail_reference": p.get("rail_reference"),
            "remarks": p.get("remarks"), "settled_at": p.get("settled_at"),
            "payee": (ben["account_masked"] or ben["vpa"]) if ben else None, "payee_name": ben["name"] if ben else None,
        })
    elif payment_type == "bill_payment":
        view.update({"rail": "BBPS", "amount_paise": p.get("amount_paise"), "rail_reference": p.get("rail_reference"),
                     "payee": p.get("biller_name") or p.get("payment_kind")})
    else:
        view.update({"rail": "SWIFT", "amount_paise": p.get("inr_amount_paise"), "rail_reference": p.get("swift_uetr"),
                     "payee": p.get("beneficiary_name")})
    return view


def _rail_side(payment_type: str, exc: dict, p: dict | None, callbacks: list[dict]) -> dict:
    details = jload(exc.get("details"), {}) or {}
    status = (p or {}).get("status")
    return {
        "payment_status": status,
        "rail_reference": exc.get("rail_reference") or (p or {}).get("rail_reference"),
        "reported_success": status in ("success", "completed", "sent", "settled", "paid"),
        "callbacks": callbacks,
        "detected": details,
    }


def _exception_view(conn: sqlite3.Connection, exc: dict, *, full: bool = False) -> dict:
    p = _payment(conn, exc["payment_type"], exc["payment_id"])
    callbacks: list[dict] = []
    ref = exc.get("rail_reference") or (p or {}).get("rail_reference")
    if full and ref:
        callbacks = [{"id": r["id"], "rail": r["rail"], "payload": jload(r["payload"], {}), "duplicate": bool(r["duplicate"]),
                      "received_at": r["received_at"]}
                     for r in conn.execute("SELECT * FROM rail_callbacks WHERE rail_reference = ? ORDER BY received_at", (ref,))]
    try:
        fin = posting_state(conn, exc["payment_type"], p) if p else None
    except IntegrationError as e:
        fin = {"error": e.message}
    resolver = one(conn, "SELECT display_name FROM users WHERE id = ?", (exc.get("resolved_by"),)) if exc.get("resolved_by") else None
    return {
        "id": exc["id"],
        "payment_type": exc["payment_type"],
        "payment_id": exc["payment_id"],
        "mismatch_type": exc["mismatch_type"],
        "mismatch_label": MISMATCH_LABELS.get(exc["mismatch_type"], exc["mismatch_type"]),
        "rail_reference": ref,
        "amount_paise": exc.get("amount_paise"),
        "status": exc["status"],
        "resolution": exc.get("resolution"),
        "resolution_note": exc.get("resolution_note"),
        "resolved_by": resolver["display_name"] if resolver else exc.get("resolved_by"),
        "resolved_at": exc.get("resolved_at"),
        "created_at": exc["created_at"],
        **_age(exc["created_at"]),
        "payment": _payment_view(conn, exc["payment_type"], p),
        "rail": _rail_side(exc["payment_type"], exc, p, callbacks),
        "finacle": fin,
        "details": jload(exc.get("details"), {}),
    }


def _lazy_run(conn: sqlite3.Connection) -> dict | None:
    try:
        return run_reconciliation(conn)
    except IntegrationError:
        return None


@router.post("/ops/reconciliation/run")
def run(request: Request, ctx: AuthContext = Depends(require_staff("ops_admin")), conn: sqlite3.Connection = Depends(get_db)):
    result = run_reconciliation(conn)
    audit_ctx(conn, ctx, "admin", "reconciliation.run", entity_type="reconciliation", details=result, request=request)
    return result


@router.get("/ops/reconciliation/exceptions")
def list_exceptions(status: Optional[str] = "open", mismatch_type: Optional[str] = None, payment_type: Optional[str] = None,
                    page: int = 1, page_size: int = 50,
                    ctx: AuthContext = Depends(require_staff("ops_admin")), conn: sqlite3.Connection = Depends(get_db)):
    if status not in (None, "", "open", "resolved", "all"):
        raise invalid("status", "Use open, resolved or all")
    if mismatch_type and mismatch_type not in MISMATCH_TYPES:
        raise invalid("mismatch_type", f"Use one of: {', '.join(MISMATCH_TYPES)}")
    if payment_type and payment_type not in PAYMENT_TABLES:
        raise invalid("payment_type", "Use transfer, bill_payment or lrs_remittance")
    if page < 1 or not (1 <= page_size <= 200):
        raise invalid("page_size", "Page size must be between 1 and 200")
    last_run = _lazy_run(conn)
    where, params = ["1 = 1"], []
    if status in ("open", "resolved"):
        where.append("status = ?")
        params.append(status)
    if mismatch_type:
        where.append("mismatch_type = ?")
        params.append(mismatch_type)
    if payment_type:
        where.append("payment_type = ?")
        params.append(payment_type)
    clause = " AND ".join(where)
    total = conn.execute(f"SELECT COUNT(*) FROM reconciliation_exceptions WHERE {clause}", params).fetchone()[0]
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM reconciliation_exceptions WHERE {clause} ORDER BY created_at ASC LIMIT ? OFFSET ?",
        params + [page_size, (page - 1) * page_size])]
    counts = {r["mismatch_type"]: r["n"] for r in conn.execute(
        "SELECT mismatch_type, COUNT(*) AS n FROM reconciliation_exceptions WHERE status = 'open' GROUP BY mismatch_type")}
    return {"items": [_exception_view(conn, r) for r in rows], "total": total, "page": page, "page_size": page_size,
            "open_counts": counts, "last_run": last_run}


@router.get("/ops/reconciliation/exceptions/{exception_id}")
def get_exception(exception_id: str, ctx: AuthContext = Depends(require_staff("ops_admin")), conn: sqlite3.Connection = Depends(get_db)):
    exc = one(conn, "SELECT * FROM reconciliation_exceptions WHERE id = ?", (str(exception_id)[:64],))
    if not exc:
        raise not_found("Reconciliation exception")
    return _exception_view(conn, exc, full=True)


def _resolve_transfer(conn: sqlite3.Connection, exc: dict, t: dict, resolution: str, note: str) -> dict:
    core = finacle.get(conn)
    ps = posting_state(conn, "transfer", t)
    if resolution == "reversed":
        final = "reversed" if ps["net_debited"] else "failed"
        settle_transfer(conn, t["id"], "failed", reason=f"reconciliation_reversed: {note[:120]}", source="reconciliation",
                        final_status=final)
        return {"action": "debit_reversed" if ps["net_debited"] else "no_debit_to_reverse", "transfer_status": final}
    # settled: the money reached the beneficiary, so the customer's account must carry exactly one debit.
    action = "debit_already_present"
    if not ps["net_debited"]:
        acct = one(conn, "SELECT * FROM accounts WHERE id = ?", (t["from_account_id"],))
        if not acct:
            raise api_error(409, "account_missing", "The debit account of this payment no longer exists")
        idem = f"TRF-{t['id']}" if not ps["posted"] else f"RECON-{exc['id']}"
        posting = core.post(account_number(acct), "DR", t["amount_paise"], f"{t['rail']}/DR/{t['id'][:8].upper()}/RECON",
                            idempotency_ref=idem, channel_ref=t["id"], allow_overdraft=True)
        conn.execute("UPDATE transfers SET finacle_posting_ref = ? WHERE id = ?", (posting["posting_ref"], t["id"]))
        action = "missing_debit_posted"
    now = now_iso()
    conn.execute("UPDATE transfers SET status = 'success', settled_at = COALESCE(settled_at, ?), failure_reason = NULL, updated_at = ? "
                 "WHERE id = ?", (now, now, t["id"]))
    outbox(conn, "transfer.success", {"transfer_id": t["id"], "source": "reconciliation"})
    return {"action": action, "transfer_status": "success"}


def _resolve_generic(conn: sqlite3.Connection, exc: dict, table: str, p: dict, resolution: str) -> dict:
    core = finacle.get(conn)
    ps = posting_state(conn, exc["payment_type"], p)
    amount = p.get("amount_paise") if table == "bill_payments" else p.get("inr_amount_paise")
    now = now_iso()
    if resolution == "reversed":
        action = "no_debit_to_reverse"
        if ps["net_debited"] and ps["posting_ref"]:
            core.reverse(ps["posting_ref"], f"REV {exc['payment_type']} {p['id'][:8].upper()} RECON")
            action = "debit_reversed"
        if table == "bill_payments":
            conn.execute("UPDATE bill_payments SET status = 'failed', failure_reason = 'reconciliation_reversed', updated_at = ? WHERE id = ?",
                         (now, p["id"]))
        else:
            conn.execute("UPDATE lrs_remittances SET status = 'failed' WHERE id = ?", (p["id"],))
        return {"action": action, "payment_status": "failed"}
    action = "debit_already_present"
    if not ps["net_debited"] and amount:
        acct = one(conn, "SELECT * FROM accounts WHERE id = ?", (p.get("from_account_id"),))
        if not acct:
            raise api_error(409, "account_missing", "The debit account of this payment no longer exists")
        posting = core.post(account_number(acct), "DR", int(amount), f"{exc['payment_type'].upper()}/{p['id'][:8].upper()}/RECON",
                            idempotency_ref=f"RECON-{exc['id']}", channel_ref=p["id"], allow_overdraft=True)
        conn.execute(f"UPDATE {table} SET finacle_posting_ref = ? WHERE id = ?", (posting["posting_ref"], p["id"]))
        action = "missing_debit_posted"
    if table == "bill_payments":
        conn.execute("UPDATE bill_payments SET status = 'success', failure_reason = NULL, updated_at = ? WHERE id = ?", (now, p["id"]))
        return {"action": action, "payment_status": "success"}
    conn.execute("UPDATE lrs_remittances SET status = 'completed' WHERE id = ?", (p["id"],))
    return {"action": action, "payment_status": "completed"}


@router.post("/ops/reconciliation/exceptions/{exception_id}/resolve")
def resolve_exception(exception_id: str, body: ResolveIn, request: Request, ctx: AuthContext = Depends(require_staff("ops_admin")),
                      conn: sqlite3.Connection = Depends(get_db)):
    exc = one(conn, "SELECT * FROM reconciliation_exceptions WHERE id = ?", (str(exception_id)[:64],))
    if not exc:
        raise not_found("Reconciliation exception")
    if exc["status"] == "resolved":
        raise api_error(409, "already_resolved", f"Already resolved as {exc['resolution']} on {exc['resolved_at']}")
    resolution = (body.resolution or "").strip().lower()
    if resolution not in ("settled", "reversed"):
        raise invalid("resolution", "Choose Mark settled or Reverse")
    note = (body.note or "").strip()
    if len(note) < 5:
        raise invalid("note", "Add a note of at least 5 characters explaining the resolution")
    if len(note) > 1000:
        raise invalid("note", "The note can be at most 1000 characters")
    p = _payment(conn, exc["payment_type"], exc["payment_id"])
    if not p:
        raise api_error(409, "payment_missing", "The payment for this exception was not found")
    if exc["payment_type"] == "transfer":
        outcome = _resolve_transfer(conn, exc, p, resolution, note)
    else:
        outcome = _resolve_generic(conn, exc, PAYMENT_TABLES[exc["payment_type"]], p, resolution)

    now = now_iso()
    details = jload(exc["details"], {}) or {}
    details["resolution_outcome"] = outcome
    conn.execute(
        """UPDATE reconciliation_exceptions SET status = 'resolved', resolution = ?, resolution_note = ?, resolved_by = ?,
           resolved_at = ?, details = ? WHERE id = ?""",
        (resolution, note, ctx.user_id, now, jdump(details), exc["id"]),
    )
    # Other open exceptions for the same payment are settled by the same decision.
    conn.execute(
        """UPDATE reconciliation_exceptions SET status = 'resolved', resolution = ?, resolution_note = ?, resolved_by = ?, resolved_at = ?
           WHERE payment_type = ? AND payment_id = ? AND status = 'open' AND id != ?""",
        (resolution, f"Resolved with exception {exc['id'][:8]}: {note}"[:1000], ctx.user_id, now, exc["payment_type"], exc["payment_id"],
         exc["id"]),
    )
    amount = exc.get("amount_paise") or p.get("amount_paise") or p.get("inr_amount_paise")
    ref = exc.get("rail_reference") or p.get("rail_reference") or p["id"][:8].upper()
    uid = user_id_for_customer(conn, p.get("customer_id"))
    if resolution == "reversed":
        notify(conn, uid, "transaction", "Payment reversed",
               f"Your payment of {format_inr(amount)} (ref {ref}) did not complete. Any amount debited has been credited back to your account.")
    else:
        notify(conn, uid, "transaction", "Payment confirmed",
               f"Your payment of {format_inr(amount)} (ref {ref}) is confirmed as completed"
               + (" and has now been debited from your account." if outcome.get("action") == "missing_debit_posted" else "."))
    audit_ctx(conn, ctx, "financial", "reconciliation.resolved", entity_type="reconciliation_exception", entity_id=exc["id"],
              details={"payment_type": exc["payment_type"], "payment_id": exc["payment_id"], "mismatch_type": exc["mismatch_type"],
                       "resolution": resolution, "note": note, "outcome": outcome}, request=request)
    updated = one(conn, "SELECT * FROM reconciliation_exceptions WHERE id = ?", (exc["id"],))
    return _exception_view(conn, updated, full=True)  # type: ignore[arg-type]

"""Bill and tax payments: Bharat Connect (BBPS) bills, GST challans (CPIN) and income-tax challans.

Every payment is debited in Finacle first (idempotent on ``BIL-<payment id>``) and then sent
to the rail. If the rail fails, the Finacle debit is reversed and the payment is marked
``failed``. A request is idempotent on (customer, idempotency_key): the same payload
returns the original payment, a different payload with the same key is a 409.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from audit import audit_ctx
from banking import account_number, owned_account
from core import (api_error, financial_year, fmt_ist, format_inr, forbidden, invalid, mask_pan, new_id, not_found, now_iso,
                  one, positive_paise, request_fingerprint, valid_pan)
from db import get_db
from integrations import bbps, finacle, gst_tax_portal
from integrations.base import IntegrationError, is_mock, mock_meta
from notify import notify
from objectstore import create_document, get_bytes
from pdfgen import Section, make_pdf
from security import AuthContext, consume_mfa_token, require_customer

router = APIRouter(tags=["payments"])

PAYMENT_KINDS = ("bbps_bill", "gst_challan", "income_tax_challan")
PAYABLE_ACCOUNT_TYPES = ("savings", "current", "nre", "nro")
MAX_PAYMENT_PAISE = 10**11  # Rs. 100 crore sanity cap; rail limits apply on top
AY_RE = re.compile(r"^(\d{4})-(\d{2})$")

KIND_LABEL = {"bbps_bill": "Bharat Connect bill", "gst_challan": "GST challan", "income_tax_challan": "Income-tax challan"}
REFERENCE_LABEL = {"bbps_bill": "Bharat Connect transaction ID", "gst_challan": "CIN", "income_tax_challan": "CIN"}
RAIL_PROVIDER = {"bbps_bill": "bbps", "gst_challan": "gst_tax_portal", "income_tax_challan": "gst_tax_portal"}


class Body(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class FetchIn(Body):
    biller_id: Optional[str] = None
    consumer_number: Optional[str] = None
    cpin: Optional[str] = None


class BillPayIn(Body):
    payment_kind: str
    from_account_id: Optional[str] = None
    amount_paise: Optional[int] = None
    biller_id: Optional[str] = None
    consumer_number: Optional[str] = None
    cpin: Optional[str] = None
    pan: Optional[str] = None
    assessment_year: Optional[str] = None
    tax_type: Optional[str] = None
    idempotency_key: Optional[str] = None
    mfa_token: Optional[str] = None


# ------------------------------------------------------------------ helpers
def current_assessment_year() -> str:
    """AY that follows the current FY, e.g. FY 2026-27 -> AY 2027-28."""
    start = int(financial_year().split("-")[0]) + 1
    return f"{start}-{str(start + 1)[-2:]}"


def _validate_ay(value: str | None) -> str:
    m = AY_RE.match(value or "")
    if not m or (int(m.group(1)) + 1) % 100 != int(m.group(2)):
        raise invalid("assessment_year", "Enter the assessment year like 2027-28")
    cur = int(current_assessment_year()[:4])
    if not (cur - 3 <= int(m.group(1)) <= cur):
        raise invalid("assessment_year", f"Choose an assessment year between {cur - 3}-{str(cur - 2)[-2:]} and {current_assessment_year()}")
    return value  # type: ignore[return-value]


def _clean_cpin(value: str | None) -> str:
    cpin = re.sub(r"\s", "", value or "")
    if not gst_tax_portal.CPIN_RE.match(cpin):
        raise invalid("cpin", "Enter the 14-digit CPIN generated on the GST portal")
    return cpin


def _account_masked(conn: sqlite3.Connection, account_id: str) -> str | None:
    row = one(conn, "SELECT masked_number FROM accounts WHERE id = ?", (account_id,))
    return row["masked_number"] if row else None


def public_payment(conn: sqlite3.Connection, row: dict) -> dict:
    mock = is_mock(conn, RAIL_PROVIDER[row["payment_kind"]])
    return {
        "id": row["id"],
        "payment_kind": row["payment_kind"],
        "kind_label": KIND_LABEL[row["payment_kind"]],
        "biller_id": row["biller_id"],
        "biller_name": row["biller_name"],
        "consumer_number": row["consumer_number"],
        "cpin": row["cpin"],
        "pan_masked": row["pan"],
        "assessment_year": row["assessment_year"],
        "tax_type": row["tax_type"],
        "tax_type_label": gst_tax_portal.TAX_TYPES.get(row["tax_type"] or "", {}).get("label"),
        "amount_paise": row["amount_paise"],
        "from_account_id": row["from_account_id"],
        "from_account_masked": _account_masked(conn, row["from_account_id"]),
        "rail_reference": row["rail_reference"],
        "reference_label": REFERENCE_LABEL[row["payment_kind"]],
        "finacle_posting_ref": row["finacle_posting_ref"],
        "status": row["status"],
        "failure_reason": row["failure_reason"],
        "receipt_available": row["status"] == "success" and bool(row["receipt_document_id"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "mock": mock_meta(RAIL_PROVIDER[row["payment_kind"]]) if mock else None,
    }


def issue_receipt(conn: sqlite3.Connection, row: dict, account_masked: str | None, mock: bool = True) -> dict:
    """Generate and store the receipt PDF for a successful payment. Returns the document row."""
    kind = row["payment_kind"]
    lines = [
        f"Payment type: {KIND_LABEL[kind]}",
        f"{REFERENCE_LABEL[kind]}: {row['rail_reference']}",
        f"Amount: {format_inr(row['amount_paise'])}",
        f"Debited from: {account_masked or '-'}",
        f"Finacle posting reference: {row['finacle_posting_ref'] or '-'}",
        f"Paid on: {fmt_ist(row['updated_at'] or row['created_at'])}",
        f"Platform reference: {row['id']}",
    ]
    if kind == "bbps_bill":
        lines[1:1] = [f"Biller: {row['biller_name']} ({row['biller_id']})", f"Consumer number: {row['consumer_number']}"]
    elif kind == "gst_challan":
        lines[1:1] = [f"CPIN: {row['cpin']}"]
    else:
        tt = gst_tax_portal.TAX_TYPES.get(row["tax_type"] or "", {}).get("label", row["tax_type"])
        lines[1:1] = [f"PAN: {row['pan']}", f"Assessment year: {row['assessment_year']}", f"Type of payment: {tt}"]
    footer = ("Bharat Connect (BBPS) payment. Keep this receipt until the biller reflects the payment."
              if kind == "bbps_bill" else
              "Challan Identification Number (CIN) can be verified on the tax portal a few hours after payment.")
    pdf = make_pdf(f"{KIND_LABEL[kind]} receipt", [Section("Payment details", lines)], footer_note=footer, mock=mock)
    safe_ref = re.sub(r"[^A-Za-z0-9_-]", "", row["rail_reference"] or row["id"])[:40]
    return create_document(conn, owner_type="bill_payment", owner_id=row["id"], doc_type="receipt", data=pdf,
                           file_name=f"receipt-{safe_ref}.pdf")


# ------------------------------------------------------------------ catalogue & fetch
@router.get("/bbps/billers")
def billers(ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    mock = is_mock(conn, "bbps")
    return {"categories": bbps.catalogue(), "mock": mock_meta("bbps") if mock else None}


@router.post("/me/bill-payments/fetch")
def fetch_bill(body: FetchIn, ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    if body.cpin is not None and not body.biller_id:
        cpin = _clean_cpin(body.cpin)
        info = gst_tax_portal.get(conn).validate_cpin(cpin)
        if not info:
            raise invalid("cpin", "This CPIN was not found or has expired. Generate a new challan on the GST portal.")
        return {"payment_kind": "gst_challan", **info, "amount_editable": False}
    biller = bbps.find_biller(body.biller_id)
    if not biller:
        raise invalid("biller_id", "Choose a biller from the list", code="unknown_biller")
    consumer = bbps.normalize_consumer_number(body.consumer_number)
    if not bbps.valid_consumer_number(biller, consumer):
        raise invalid("consumer_number", f"Enter a valid {biller['consumer_number_label']} ({biller['consumer_number_hint']})")
    bill = bbps.get(conn).fetch_bill(biller["biller_id"], consumer)
    return {"payment_kind": "bbps_bill", "biller": biller, **bill}


# ------------------------------------------------------------------ pay
@router.post("/me/bill-payments", status_code=202)
def pay(body: BillPayIn, request: Request, ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    kind = body.payment_kind
    if kind not in PAYMENT_KINDS:
        raise invalid("payment_kind", "Choose a bill, GST challan or income-tax challan payment")
    key = (body.idempotency_key or "").strip()
    if not (8 <= len(key) <= 100):
        raise invalid("idempotency_key", "A unique idempotency key (8 to 100 characters) is required for each payment")
    if not body.from_account_id:
        raise invalid("from_account_id", "Choose the account to pay from")

    biller: dict | None = None
    fields: dict = {}
    amount: int | None = None
    full_pan: str | None = None
    if kind == "bbps_bill":
        biller = bbps.find_biller(body.biller_id)
        if not biller:
            raise invalid("biller_id", "Choose a biller from the list", code="unknown_biller")
        consumer = bbps.normalize_consumer_number(body.consumer_number)
        if not bbps.valid_consumer_number(biller, consumer):
            raise invalid("consumer_number", f"Enter a valid {biller['consumer_number_label']} ({biller['consumer_number_hint']})")
        amount = positive_paise(body.amount_paise, "amount_paise", MAX_PAYMENT_PAISE)
        fields = {"biller_id": biller["biller_id"], "consumer_number": consumer}
    elif kind == "gst_challan":
        fields = {"cpin": _clean_cpin(body.cpin)}
        if body.amount_paise is not None:
            amount = positive_paise(body.amount_paise, "amount_paise", MAX_PAYMENT_PAISE)
    else:
        full_pan = (body.pan or "").replace(" ", "").upper()
        if not valid_pan(full_pan):
            raise invalid("pan", "Enter a valid PAN, for example ABCDE1234F")
        ay = _validate_ay(body.assessment_year)
        if body.tax_type not in gst_tax_portal.TAX_TYPES:
            raise invalid("tax_type", "Choose advance tax, self-assessment tax or tax on regular assessment")
        if body.tax_type == "advance_tax" and ay != current_assessment_year():
            raise invalid("assessment_year", f"Advance tax is paid for the next assessment year ({current_assessment_year()})")
        amount = positive_paise(body.amount_paise, "amount_paise", MAX_PAYMENT_PAISE)
        fields = {"pan": full_pan, "assessment_year": ay, "tax_type": body.tax_type}

    fingerprint = request_fingerprint({"payment_kind": kind, "from_account_id": body.from_account_id,
                                       "amount_paise": body.amount_paise, **fields})
    existing = one(conn, "SELECT * FROM bill_payments WHERE customer_id = ? AND idempotency_key = ?", (ctx.customer_id, key))
    if existing:
        if existing["request_hash"] == fingerprint:
            return {**public_payment(conn, existing), "idempotent_replay": True}
        raise api_error(409, "idempotency_conflict",
                        "This idempotency key was already used for a different payment. Start a new payment.")

    consume_mfa_token(conn, ctx, body.mfa_token, "bill_payment")
    acct = owned_account(conn, ctx.customer_id, body.from_account_id)
    if acct["account_type"] not in PAYABLE_ACCOUNT_TYPES or acct["currency"] != "INR":
        raise invalid("from_account_id", "Pay from a rupee savings or current account")

    provider = RAIL_PROVIDER[kind]
    if kind == "bbps_bill":
        assert biller is not None and amount is not None
        bill = bbps.get(conn).fetch_bill(biller["biller_id"], fields["consumer_number"])
        if not biller["amount_editable"] and amount != bill["bill_amount_paise"]:
            raise invalid("amount_paise", f"This biller accepts only the billed amount of {format_inr(bill['bill_amount_paise'])}")
    elif kind == "gst_challan":
        info = gst_tax_portal.get(conn).validate_cpin(fields["cpin"])
        if not info:
            raise invalid("cpin", "This CPIN was not found or has expired. Generate a new challan on the GST portal.")
        if amount is not None and amount != info["amount_due_paise"]:
            raise invalid("amount_paise", f"The challan amount is {format_inr(info['amount_due_paise'])}")
        amount = info["amount_due_paise"]
    assert amount is not None

    pid = new_id()
    now = now_iso()
    row = {
        "id": pid, "customer_id": ctx.customer_id, "payment_kind": kind,
        "biller_id": biller["biller_id"] if biller else None, "biller_name": biller["name"] if biller else None,
        "consumer_number": fields.get("consumer_number"), "cpin": fields.get("cpin"),
        "pan": mask_pan(full_pan) if full_pan else None,  # only the masked PAN is stored
        "assessment_year": fields.get("assessment_year"), "tax_type": fields.get("tax_type"),
        "amount_paise": amount, "from_account_id": acct["id"], "idempotency_key": key, "request_hash": fingerprint,
        "rail_reference": None, "finacle_posting_ref": None, "receipt_document_id": None,
        "status": "initiated", "failure_reason": None, "created_at": now, "updated_at": now,
    }
    conn.execute(
        """INSERT INTO bill_payments (id, customer_id, payment_kind, biller_id, biller_name, consumer_number, cpin, pan,
           assessment_year, tax_type, amount_paise, from_account_id, idempotency_key, request_hash, status, created_at, updated_at)
           VALUES (:id, :customer_id, :payment_kind, :biller_id, :biller_name, :consumer_number, :cpin, :pan, :assessment_year,
           :tax_type, :amount_paise, :from_account_id, :idempotency_key, :request_hash, :status, :created_at, :updated_at)""",
        row,
    )

    core = finacle.get(conn)
    narration = {
        "bbps_bill": f"BBPS/{(biller or {}).get('name', '')[:40]}/{fields.get('consumer_number', '')}",
        "gst_challan": f"GST CHALLAN CPIN {fields.get('cpin', '')}",
        "income_tax_challan": f"INCOME TAX {fields.get('tax_type', '')} AY {fields.get('assessment_year', '')}",
    }[kind]
    posting = core.post(account_number(acct), "DR", amount, narration, idempotency_ref=f"BIL-{pid}", channel_ref=pid)
    row.update(status="pending", finacle_posting_ref=posting["posting_ref"], updated_at=now_iso())
    conn.execute("UPDATE bill_payments SET status = 'pending', finacle_posting_ref = ?, updated_at = ? WHERE id = ?",
                 (row["finacle_posting_ref"], row["updated_at"], pid))

    try:
        if kind == "bbps_bill":
            res = bbps.get(conn).pay(biller_id=row["biller_id"], consumer_number=row["consumer_number"], amount_paise=amount,
                                     payment_ref=pid)
            rail_ref = res["bbps_txn_id"]
        elif kind == "gst_challan":
            res = gst_tax_portal.get(conn).pay_gst(cpin=row["cpin"], amount_paise=amount, payment_ref=pid)
            rail_ref = res["cin"]
        else:
            res = gst_tax_portal.get(conn).pay_income_tax(pan=full_pan, assessment_year=row["assessment_year"],
                                                          tax_type=row["tax_type"], amount_paise=amount, payment_ref=pid)
            rail_ref = res["cin"]
    except IntegrationError as exc:
        reversal = core.reverse(posting["posting_ref"], f"REVERSAL {narration}"[:140])
        row.update(status="failed", failure_reason=exc.message, updated_at=now_iso())
        conn.execute("UPDATE bill_payments SET status = 'failed', failure_reason = ?, updated_at = ? WHERE id = ?",
                     (exc.message, row["updated_at"], pid))
        notify(conn, ctx.user_id, "transaction", "Payment failed, amount reversed",
               f"{KIND_LABEL[kind]} of {format_inr(amount)} from {acct['masked_number']} could not be completed. "
               f"The debit has been reversed (ref {reversal['posting_ref']}).")
        audit_ctx(conn, ctx, "financial", "bill_payment.failed", entity_type="bill_payment", entity_id=pid, request=request,
                  details={"kind": kind, "amount_paise": amount, "account": acct["masked_number"], "reason": exc.message,
                           "posting_ref": posting["posting_ref"], "reversal_ref": reversal["posting_ref"]})
        return public_payment(conn, row)

    row.update(status="success", rail_reference=rail_ref, updated_at=now_iso())
    doc = issue_receipt(conn, row, acct["masked_number"], mock=is_mock(conn, provider))
    row["receipt_document_id"] = doc["id"]
    conn.execute(
        "UPDATE bill_payments SET status = 'success', rail_reference = ?, receipt_document_id = ?, updated_at = ? WHERE id = ?",
        (rail_ref, doc["id"], row["updated_at"], pid),
    )
    what = row["biller_name"] if kind == "bbps_bill" else KIND_LABEL[kind]
    notify(conn, ctx.user_id, "transaction", "Payment successful",
           f"{format_inr(amount)} debited from {acct['masked_number']} for {what}. {REFERENCE_LABEL[kind]} {rail_ref}. "
           "Not you? Call 1930 or report fraud immediately.")
    audit_ctx(conn, ctx, "financial", "bill_payment.success", entity_type="bill_payment", entity_id=pid, request=request,
              details={"kind": kind, "amount_paise": amount, "account": acct["masked_number"], "rail_reference": rail_ref,
                       "posting_ref": posting["posting_ref"], "pan": row["pan"]})
    return public_payment(conn, row)


# ------------------------------------------------------------------ history & receipt
@router.get("/me/bill-payments")
def history(limit: int = Query(50, ge=1, le=200), ctx: AuthContext = Depends(require_customer),
            conn: sqlite3.Connection = Depends(get_db)):
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM bill_payments WHERE customer_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?", (ctx.customer_id, limit))]
    return {"items": [public_payment(conn, r) for r in rows]}


@router.get("/me/bill-payments/{payment_id}/receipt")
def receipt(payment_id: str, ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    row = one(conn, "SELECT * FROM bill_payments WHERE id = ?", (payment_id[:64],))
    if not row:
        raise not_found("Payment")
    if row["customer_id"] != ctx.customer_id:
        raise forbidden("This payment does not belong to you")
    if row["status"] != "success" or not row["receipt_document_id"]:
        raise api_error(409, "receipt_unavailable", "A receipt is available only for successful payments")
    doc = one(conn, "SELECT * FROM documents WHERE id = ?", (row["receipt_document_id"],))
    if not doc:
        raise not_found("Receipt")
    try:
        data = get_bytes(doc["storage_key"])
    except FileNotFoundError:
        raise not_found("Receipt")
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{doc["file_name"]}"'})

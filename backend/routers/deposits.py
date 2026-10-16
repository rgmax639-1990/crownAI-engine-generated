"""Fixed deposits (booked in Finacle at the LIVE published rate) and tax forms (15G / 15H / 16A).

Deposits are read from Finacle (``deposits_for_cif``) and merged with the platform's
``fixed_deposits`` rows (which hold the advice document and the booking channel). Rates
come only from ``rates_service`` so the FD form quotes the same number as the public rates
page and calculators.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from audit import audit_ctx
from banking import account_number, customer_for, owned_account
from core import (api_error, financial_year, fmt_date, format_inr, forbidden, get_reg_value, invalid, mask_account, new_id,
                  not_found, now_iso, one, parse_date, reference_no, sha256_hex, today_ist)
from db import get_db
from integrations import finacle
from integrations.base import is_mock, mock_meta
from notify import notify
from objectstore import create_document, get_bytes
from pdfgen import Section, make_pdf
from rates_service import fd_rate
from security import AuthContext, consume_mfa_token, require_customer

router = APIRouter(tags=["deposits"])

MIN_PRINCIPAL_PAISE = 100000  # Rs. 1,000
MAX_PRINCIPAL_PAISE = 10**11
MIN_TENURE_DAYS, MAX_TENURE_DAYS = 7, 3650
PAYOUTS = ("cumulative", "monthly", "quarterly")
MATURITY_INSTRUCTIONS = ("auto_renew_principal_interest", "auto_renew_principal", "credit_to_account")
FD_SOURCE_ACCOUNT_TYPES = ("savings", "current", "nro")
PAYOUT_LABEL = {"cumulative": "Cumulative (reinvest interest)", "monthly": "Monthly interest payout",
                "quarterly": "Quarterly interest payout"}
MATURITY_LABEL = {"auto_renew_principal_interest": "Auto-renew principal and interest",
                  "auto_renew_principal": "Auto-renew principal, credit interest to account",
                  "credit_to_account": "Close and credit maturity amount to account"}

FY_RE = re.compile(r"^(\d{4})-(\d{2})$")
QUARTERS = ("Q1", "Q2", "Q3", "Q4")
FORM_DECLARATIONS = {
    "15G": ("I declare that I am a resident individual below 60 years of age, that the tax on my estimated total income "
            "for the financial year, including the interest on deposits with this bank, will be NIL, and that the "
            "particulars given are correct and complete. I understand that a false declaration is an offence under "
            "section 277 of the Income-tax Act, 1961."),
    "15H": ("I declare that I am a resident individual aged 60 years or more, that the tax on my estimated total income "
            "for the financial year, including the interest on deposits with this bank, will be NIL, and that the "
            "particulars given are correct and complete. I understand that a false declaration is an offence under "
            "section 277 of the Income-tax Act, 1961."),
}


class Body(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class FdIn(Body):
    from_account_id: Optional[str] = None
    principal_paise: Optional[int] = None
    tenure_days: Optional[int] = None
    payout: Optional[str] = "cumulative"
    maturity_instruction: Optional[str] = "credit_to_account"
    idempotency_key: Optional[str] = None
    mfa_token: Optional[str] = None


class TaxFormIn(Body):
    form_type: Optional[str] = None
    financial_year: Optional[str] = None
    estimated_income_paise: Optional[int] = None
    declaration: Optional[bool] = None


# ------------------------------------------------------------------ shared helpers
def customer_age(customer: dict, on: date | None = None) -> int | None:
    dob = parse_date(customer.get("date_of_birth"))
    if not dob:
        return None
    on = on or today_ist()
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def is_senior(conn: sqlite3.Connection, customer: dict) -> bool:
    age = customer_age(customer)
    return age is not None and age >= int(get_reg_value(conn, "senior_citizen_age", 60) or 60)


def _validate_tenure(value: int | None) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not (MIN_TENURE_DAYS <= value <= MAX_TENURE_DAYS):
        raise invalid("tenure_days", f"Choose a tenure between {MIN_TENURE_DAYS} days and 10 years ({MAX_TENURE_DAYS} days)")
    return value


def _validate_principal(value: int | None) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < MIN_PRINCIPAL_PAISE:
        raise invalid("principal_paise", f"The minimum deposit is {format_inr(MIN_PRINCIPAL_PAISE)}")
    if value > MAX_PRINCIPAL_PAISE:
        raise invalid("principal_paise", f"The maximum online deposit is {format_inr(MAX_PRINCIPAL_PAISE)}")
    return value


def _live_rate(conn: sqlite3.Connection, tenure_days: int, senior: bool, principal: int) -> dict:
    rate = fd_rate(conn, tenure_days, senior, principal)
    if not rate:
        raise invalid("tenure_days", "No published interest rate covers this tenure and amount. Choose another tenure.",
                      code="no_live_rate")
    return rate


def _stream_document(conn: sqlite3.Connection, document_id: str | None) -> Response:
    doc = one(conn, "SELECT * FROM documents WHERE id = ?", (document_id or "",))
    if not doc:
        raise not_found("Document")
    try:
        data = get_bytes(doc["storage_key"])
    except FileNotFoundError:
        raise not_found("Document")
    return Response(content=data, media_type=doc["content_type"] or "application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{doc["file_name"]}"'})


def fd_advice_pdf(conn: sqlite3.Connection, *, customer: dict, deposit: dict, account_masked: str, rate: dict | None,
                  senior: bool, mock: bool) -> bytes:
    dicgc = get_reg_value(conn, "dicgc_limit_paise", 50000000)
    tds = get_reg_value(conn, "tds_rate_percent", 10)
    lines = [
        f"Depositor: {customer['full_name']}",
        f"Finacle deposit reference: {deposit['deposit_ref']}",
        f"Principal: {format_inr(deposit['principal_paise'])}",
        f"Rate of interest: {deposit['rate_percent']:.2f}% p.a.{' (senior citizen rate)' if senior else ''}",
        f"Tenure: {deposit['tenure_days']} days",
        f"Start date: {fmt_date(deposit['start_date'])}",
        f"Maturity date: {fmt_date(deposit['maturity_date'])}",
        f"Maturity amount: {format_inr(deposit['maturity_amount_paise'])}",
        f"Interest payout: {PAYOUT_LABEL.get(deposit['payout'], deposit['payout'])}",
        f"On maturity: {MATURITY_LABEL.get(deposit['maturity_instruction'], deposit['maturity_instruction'])}",
        f"Debited from: {account_masked}",
    ]
    if rate:
        lines.append(f"Rate source: published rate table {rate.get('rate_table_slug') or ''} as on {fmt_date(rate.get('as_on_date'))}")
    notes = [
        f"TDS at {tds}% applies to interest above the annual threshold unless Form 15G/15H is on record (PAN required).",
        f"Deposits are insured by DICGC up to {format_inr(dicgc)} per depositor per bank (principal and interest).",
        "Premature withdrawal may attract a penalty as per the bank's deposit policy.",
    ]
    return make_pdf("Fixed deposit advice", [Section("Deposit details", lines), Section("Important", notes)], mock=mock)


def book_fixed_deposit(conn: sqlite3.Connection, *, customer: dict, acct: dict, principal: int, tenure_days: int, payout: str,
                       maturity_instruction: str, deposit_ref: str, rate: dict, senior: bool) -> dict:
    """Book in Finacle, store the platform row and the advice PDF. Returns the fixed_deposits row."""
    core = finacle.get(conn)
    dep = core.book_fd(cif=customer["finacle_cif"], account_number=account_number(acct), principal_paise=principal,
                       rate_percent=float(rate["rate_percent"]), tenure_days=tenure_days, payout=payout,
                       maturity_instruction=maturity_instruction, idempotency_ref=deposit_ref)
    fid = new_id()
    now = now_iso()
    row = {
        "id": fid, "customer_id": customer["id"], "from_account_id": acct["id"], "finacle_deposit_ref": dep["deposit_ref"],
        "principal_paise": dep["principal_paise"], "tenure_days": dep["tenure_days"], "rate_percent": dep["rate_percent"],
        "senior_citizen": int(senior), "payout": dep["payout"], "maturity_instruction": dep["maturity_instruction"],
        "maturity_date": dep["maturity_date"], "maturity_amount_paise": dep["maturity_amount_paise"],
        "advice_document_id": None, "status": "booked", "failure_reason": None, "created_at": now, "booked_at": now,
    }
    pdf = fd_advice_pdf(conn, customer=customer, deposit=dep, account_masked=acct["masked_number"], rate=rate, senior=senior,
                        mock=is_mock(conn, "finacle"))
    doc = create_document(conn, owner_type="fixed_deposit", owner_id=fid, doc_type="fd_advice", data=pdf,
                          file_name=f"fd-advice-{dep['deposit_ref']}.pdf")
    row["advice_document_id"] = doc["id"]
    conn.execute(
        """INSERT INTO fixed_deposits (id, customer_id, from_account_id, finacle_deposit_ref, principal_paise, tenure_days,
           rate_percent, senior_citizen, payout, maturity_instruction, maturity_date, maturity_amount_paise, advice_document_id,
           status, created_at, booked_at) VALUES (:id, :customer_id, :from_account_id, :finacle_deposit_ref, :principal_paise,
           :tenure_days, :rate_percent, :senior_citizen, :payout, :maturity_instruction, :maturity_date, :maturity_amount_paise,
           :advice_document_id, :status, :created_at, :booked_at)""",
        row,
    )
    row["start_date"] = dep.get("start_date")
    row["posting_ref"] = dep.get("posting_ref")
    return row


def _public_fd(conn: sqlite3.Connection, fin: dict | None, plat: dict | None, mock: bool) -> dict:
    src = fin or {}
    acct_masked = None
    if plat:
        a = one(conn, "SELECT masked_number FROM accounts WHERE id = ?", (plat["from_account_id"],))
        acct_masked = a["masked_number"] if a else None
    if not acct_masked and src.get("account_number"):
        acct_masked = mask_account(src["account_number"])
    get = lambda k, pk=None: src.get(k) if src.get(k) is not None else (plat or {}).get(pk or k)  # noqa: E731
    return {
        "id": plat["id"] if plat else src.get("deposit_ref"),
        "finacle_reference": src.get("deposit_ref") or (plat or {}).get("finacle_deposit_ref"),
        "principal_paise": get("principal_paise"),
        "rate_percent": get("rate_percent"),
        "tenure_days": get("tenure_days"),
        "start_date": src.get("start_date") or ((plat or {}).get("booked_at") or "")[:10] or None,
        "maturity_date": get("maturity_date"),
        "maturity_amount_paise": get("maturity_amount_paise"),
        "payout": get("payout"),
        "maturity_instruction": get("maturity_instruction"),
        "senior_citizen": bool((plat or {}).get("senior_citizen")),
        "status": src.get("status") or (plat or {}).get("status"),
        "from_account_masked": acct_masked,
        "advice_available": bool(plat and plat.get("advice_document_id")),
        "booked_online": bool(plat),
        "mock": mock_meta("finacle") if mock else None,
    }


# ------------------------------------------------------------------ deposits
@router.get("/me/deposits")
def my_deposits(ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    cust = customer_for(conn, ctx.customer_id)  # type: ignore[arg-type]
    core = finacle.get(conn)
    mock = is_mock(conn, "finacle")
    fin = core.deposits_for_cif(cust["finacle_cif"])
    plat = {r["finacle_deposit_ref"]: dict(r) for r in conn.execute(
        "SELECT * FROM fixed_deposits WHERE customer_id = ? AND finacle_deposit_ref IS NOT NULL", (cust["id"],))}
    items = [_public_fd(conn, d, plat.pop(d["deposit_ref"], None), mock) for d in fin]
    items += [_public_fd(conn, None, p, mock) for p in plat.values()]
    active = [i for i in items if i["status"] in ("active", "booked")]
    return {
        "items": items,
        "summary": {"count": len(active), "total_principal_paise": sum(i["principal_paise"] or 0 for i in active),
                    "total_maturity_paise": sum(i["maturity_amount_paise"] or 0 for i in active)},
        "senior_citizen": is_senior(conn, cust),
        "as_of": now_iso(),
        "source": "finacle",
        "mock": mock_meta("finacle") if mock else None,
    }


@router.get("/me/deposits/quote")
def quote(principal_paise: int = Query(...), tenure_days: int = Query(...), payout: str = Query("cumulative"),
          ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    principal = _validate_principal(principal_paise)
    tenure = _validate_tenure(tenure_days)
    if payout not in PAYOUTS:
        raise invalid("payout", "Choose cumulative, monthly or quarterly payout")
    cust = customer_for(conn, ctx.customer_id)  # type: ignore[arg-type]
    senior = is_senior(conn, cust)
    rate = _live_rate(conn, tenure, senior, principal)
    return {
        "principal_paise": principal,
        "tenure_days": tenure,
        "payout": payout,
        "senior_citizen": senior,
        "rate_percent": rate["rate_percent"],
        "rate_product": rate["product_type"],
        "rate_as_on_date": rate.get("as_on_date"),
        "maturity_date": (today_ist() + timedelta(days=tenure)).isoformat(),
        "maturity_amount_paise": finacle.maturity_amount(principal, float(rate["rate_percent"]), tenure, payout),
        "note": "Estimate at the live published rate. TDS, if applicable, is deducted from interest.",
    }


@router.post("/me/deposits", status_code=201)
def open_fd(body: FdIn, request: Request, ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    principal = _validate_principal(body.principal_paise)
    tenure = _validate_tenure(body.tenure_days)
    if body.payout not in PAYOUTS:
        raise invalid("payout", "Choose cumulative, monthly or quarterly payout")
    if body.maturity_instruction not in MATURITY_INSTRUCTIONS:
        raise invalid("maturity_instruction", "Choose what happens on maturity")
    if not body.from_account_id:
        raise invalid("from_account_id", "Choose the account to fund the deposit from")
    key = (body.idempotency_key or "").strip()
    if key and not (8 <= len(key) <= 100):
        raise invalid("idempotency_key", "The idempotency key must be 8 to 100 characters")
    cust = customer_for(conn, ctx.customer_id)  # type: ignore[arg-type]
    deposit_ref = "FD" + (sha256_hex(f"{cust['id']}|{key}")[:12] if key else new_id().replace("-", "")[:12]).upper()
    if key:
        prior = one(conn, "SELECT * FROM fixed_deposits WHERE customer_id = ? AND finacle_deposit_ref = ?", (cust["id"], deposit_ref))
        if prior:
            fin = next((d for d in finacle.get(conn).deposits_for_cif(cust["finacle_cif"]) if d["deposit_ref"] == deposit_ref), None)
            return {**_public_fd(conn, fin, prior, is_mock(conn, "finacle")), "idempotent_replay": True}

    consume_mfa_token(conn, ctx, body.mfa_token, "fd_booking")
    acct = owned_account(conn, ctx.customer_id, body.from_account_id)
    if acct["account_type"] not in FD_SOURCE_ACCOUNT_TYPES or acct["currency"] != "INR":
        raise invalid("from_account_id", "Fund the deposit from a rupee savings or current account")
    senior = is_senior(conn, cust)
    rate = _live_rate(conn, tenure, senior, principal)
    row = book_fixed_deposit(conn, customer=cust, acct=acct, principal=principal, tenure_days=tenure, payout=body.payout,  # type: ignore[arg-type]
                             maturity_instruction=body.maturity_instruction, deposit_ref=deposit_ref, rate=rate, senior=senior)  # type: ignore[arg-type]
    notify(conn, ctx.user_id, "transaction", "Fixed deposit booked",
           f"{format_inr(principal)} debited from {acct['masked_number']} for FD {row['finacle_deposit_ref']} at "
           f"{row['rate_percent']:.2f}% for {tenure} days. Maturity {fmt_date(row['maturity_date'])}.")
    audit_ctx(conn, ctx, "financial", "fd.booked", entity_type="fixed_deposit", entity_id=row["id"], request=request,
              details={"principal_paise": principal, "tenure_days": tenure, "rate_percent": row["rate_percent"],
                       "senior": senior, "account": acct["masked_number"], "finacle_ref": row["finacle_deposit_ref"],
                       "posting_ref": row.get("posting_ref")})
    fin = {"deposit_ref": row["finacle_deposit_ref"], "start_date": row.get("start_date"), "status": "active"}
    return {**_public_fd(conn, fin, row, is_mock(conn, "finacle")), "posting_ref": row.get("posting_ref")}


@router.get("/me/deposits/{deposit_id}/advice")
def fd_advice(deposit_id: str, ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    row = one(conn, "SELECT * FROM fixed_deposits WHERE id = ? OR finacle_deposit_ref = ?", (deposit_id[:64], deposit_id[:64]))
    if not row:
        raise not_found("Deposit")
    if row["customer_id"] != ctx.customer_id:
        raise forbidden("This deposit does not belong to you")
    if not row["advice_document_id"]:
        raise api_error(409, "advice_unavailable", "No advice is available for this deposit")
    return _stream_document(conn, row["advice_document_id"])


# ------------------------------------------------------------------ tax forms
def _quarter_bounds(fy: str, quarter: str) -> tuple[date, date]:
    y = int(fy[:4])
    return {
        "Q1": (date(y, 4, 1), date(y, 6, 30)),
        "Q2": (date(y, 7, 1), date(y, 9, 30)),
        "Q3": (date(y, 10, 1), date(y, 12, 31)),
        "Q4": (date(y + 1, 1, 1), date(y + 1, 3, 31)),
    }[quarter]


def _valid_fy(value: str | None, field: str) -> str:
    m = FY_RE.match(value or "")
    if not m or (int(m.group(1)) + 1) % 100 != int(m.group(2)):
        raise invalid(field, "Enter the financial year like 2026-27")
    return value  # type: ignore[return-value]


def _next_fy(fy: str) -> str:
    s = int(fy[:4]) + 1
    return f"{s}-{str(s + 1)[-2:]}"


def _prev_fy(fy: str, n: int = 1) -> str:
    s = int(fy[:4]) - n
    return f"{s}-{str(s + 1)[-2:]}"


def _public_form(row: dict) -> dict:
    return {
        "id": row["id"], "form_type": row["form_type"], "financial_year": row["financial_year"], "quarter": row["quarter"],
        "estimated_income_paise": row["estimated_income_paise"], "acknowledgement_no": row["acknowledgement_no"],
        "status": row["status"], "document_available": bool(row["document_id"]), "created_at": row["created_at"],
    }


def _income_limit(conn: sqlite3.Connection, form_type: str) -> int:
    if form_type == "15G":
        return int(get_reg_value(conn, "form15g_max_income_paise", 40000000) or 40000000)
    return int(get_reg_value(conn, "form15h_max_income_paise", 120000000) or 120000000)


@router.get("/me/tax-forms")
def tax_forms(ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    cust = customer_for(conn, ctx.customer_id)  # type: ignore[arg-type]
    senior = is_senior(conn, cust)
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM tax_forms WHERE customer_id = ? AND form_type IN ('15G','15H') ORDER BY created_at DESC", (cust["id"],))]
    cur = financial_year()
    today = today_ist()
    available = []
    for fy in (cur, _prev_fy(cur)):
        for q in QUARTERS:
            _, end = _quarter_bounds(fy, q)
            available.append({"financial_year": fy, "quarter": q, "available": end < today, "period_end": end.isoformat()})
    eligible = "15H" if senior else "15G"
    return {
        "items": [_public_form(r) for r in rows],
        "form16a": available,
        "eligible_form": eligible,
        "senior_citizen": senior,
        "allowed_financial_years": [cur, _next_fy(cur)],
        "current_financial_year": cur,
        "max_estimated_income_paise": _income_limit(conn, eligible),
        "declarations": FORM_DECLARATIONS,
        "mock": mock_meta("finacle") if is_mock(conn, "finacle") else None,
    }


@router.post("/me/tax-forms", status_code=201)
def submit_tax_form(body: TaxFormIn, request: Request, ctx: AuthContext = Depends(require_customer),
                    conn: sqlite3.Connection = Depends(get_db)):
    if body.form_type not in ("15G", "15H"):
        raise invalid("form_type", "Choose Form 15G or Form 15H")
    cust = customer_for(conn, ctx.customer_id)  # type: ignore[arg-type]
    if cust.get("residency_status") != "resident":
        raise invalid("form_type", "Form 15G/15H can be submitted only by resident individuals")
    senior = is_senior(conn, cust)
    if body.form_type == "15H" and not senior:
        raise invalid("form_type", "Form 15H is only for senior citizens (60 years or more). Submit Form 15G instead.")
    if body.form_type == "15G" and senior:
        raise invalid("form_type", "Senior citizens submit Form 15H instead of Form 15G.")
    fy = _valid_fy(body.financial_year, "financial_year")
    cur = financial_year()
    if fy not in (cur, _next_fy(cur)):
        raise invalid("financial_year", f"Choose the current financial year ({cur}) or the next one ({_next_fy(cur)})")
    income = body.estimated_income_paise
    limit = _income_limit(conn, body.form_type)
    if not isinstance(income, int) or isinstance(income, bool) or income < 0:
        raise invalid("estimated_income_paise", "Enter your estimated total income for the year")
    if income > limit:
        raise invalid("estimated_income_paise",
                      f"Form {body.form_type} is for income on which tax is nil (up to {format_inr(limit)}). "
                      "TDS will apply as usual.")
    if body.declaration is not True:
        raise invalid("declaration", "Confirm the declaration to submit the form")
    if one(conn, "SELECT id FROM tax_forms WHERE customer_id = ? AND form_type = ? AND financial_year = ?",
           (cust["id"], body.form_type, fy)):
        raise api_error(409, "already_submitted", f"Form {body.form_type} is already on record for FY {fy}")

    fid = new_id()
    ack = reference_no("F" + body.form_type)
    deposits = finacle.get(conn).deposits_for_cif(cust["finacle_cif"])
    lines = [
        f"Name: {cust['full_name']}",
        f"PAN: {cust.get('pan_masked') or '-'}",
        f"Residential status: {cust.get('residency_status')}",
        f"Financial year: {fy} (assessment year {_next_fy(fy)})",
        f"Estimated total income for the year: {format_inr(income)}",
        f"Acknowledgement number: {ack}",
    ]
    table = (["Deposit ref", "Principal", "Rate", "Maturity"],
             [[d["deposit_ref"], format_inr(d["principal_paise"]), f"{d['rate_percent']:.2f}%", fmt_date(d["maturity_date"])]
              for d in deposits])
    pdf = make_pdf(f"Form No. {body.form_type} (declaration for non-deduction of tax)",
                   [Section("Part I - declarant", lines), Section("Deposits covered", table=table),
                    Section("Declaration", [FORM_DECLARATIONS[body.form_type], f"Declared electronically on {fmt_date(today_ist())}."])],
                   footer_note="Recorded in core banking for TDS processing. Part II is completed by the bank.",
                   mock=is_mock(conn, "finacle"))
    doc = create_document(conn, owner_type="tax_form", owner_id=fid, doc_type="form_15g" if body.form_type == "15G" else "form_15h",
                          data=pdf, file_name=f"form-{body.form_type.lower()}-{fy}.pdf")
    row = {"id": fid, "customer_id": cust["id"], "form_type": body.form_type, "financial_year": fy, "quarter": None,
           "estimated_income_paise": income, "acknowledgement_no": ack, "document_id": doc["id"],
           "status": "recorded_in_finacle", "created_at": now_iso()}
    conn.execute(
        """INSERT INTO tax_forms (id, customer_id, form_type, financial_year, quarter, estimated_income_paise, acknowledgement_no,
           document_id, status, created_at) VALUES (:id, :customer_id, :form_type, :financial_year, :quarter,
           :estimated_income_paise, :acknowledgement_no, :document_id, :status, :created_at)""",
        row,
    )
    audit_ctx(conn, ctx, "financial", "tax_form.submitted", entity_type="tax_form", entity_id=fid, request=request,
              details={"form_type": body.form_type, "financial_year": fy, "acknowledgement_no": ack})
    return {**_public_form(row), "mock": mock_meta("finacle") if is_mock(conn, "finacle") else None}


@router.get("/me/tax-forms/form16a")
def form16a(financial_year_: Optional[str] = Query(None, alias="financial_year"), quarter: Optional[str] = Query(None),
            ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    fy = _valid_fy(financial_year_, "financial_year")
    cur = financial_year()
    if not (int(cur[:4]) - 3 <= int(fy[:4]) <= int(cur[:4])):
        raise invalid("financial_year", f"Form 16A is available for FY {_prev_fy(cur, 3)} to {cur}")
    q = (quarter or "").upper()
    if q not in QUARTERS:
        raise invalid("quarter", "Choose a quarter: Q1, Q2, Q3 or Q4")
    q_start, q_end = _quarter_bounds(fy, q)
    if q_end >= today_ist():
        raise invalid("quarter", "Form 16A for this quarter is not available yet. It is issued after the quarter ends.",
                      code="not_available_yet")
    cust = customer_for(conn, ctx.customer_id)  # type: ignore[arg-type]
    tds_rate = float(get_reg_value(conn, "tds_rate_percent", 10) or 10)
    exempt = one(conn, "SELECT form_type, acknowledgement_no FROM tax_forms WHERE customer_id = ? AND financial_year = ? "
                       "AND form_type IN ('15G','15H')", (cust["id"], fy))
    rows, total_interest, total_tds = [], 0, 0
    for d in finacle.get(conn).deposits_for_cif(cust["finacle_cif"]):
        start, maturity = parse_date(d["start_date"]), parse_date(d["maturity_date"])
        if not start or not maturity:
            continue
        lo, hi = max(start, q_start), min(maturity, q_end + timedelta(days=1))
        days = (hi - lo).days
        if days <= 0:
            continue
        interest = int(round(d["principal_paise"] * d["rate_percent"] / 100.0 * days / 365.0))
        tds = 0 if exempt else int(round(interest * tds_rate / 100.0))
        total_interest += interest
        total_tds += tds
        rows.append([d["deposit_ref"], fmt_date(lo), fmt_date(hi - timedelta(days=1)), format_inr(interest), format_inr(tds)])
    lines = [
        "Certificate under section 203 of the Income-tax Act, 1961 for tax deducted at source (section 194A)",
        f"Deductee: {cust['full_name']}  PAN: {cust.get('pan_masked') or '-'}",
        "Deductor: Example Bank Ltd, TAN MUMX00000X (synthetic)",
        f"Financial year {fy}, quarter {q} ({fmt_date(q_start)} to {fmt_date(q_end)})",
        f"Total interest credited/accrued: {format_inr(total_interest)}",
        f"Total tax deducted and deposited: {format_inr(total_tds)}",
    ]
    if exempt:
        lines.append(f"No TDS: Form {exempt['form_type']} on record (ack {exempt['acknowledgement_no']}).")
    pdf = make_pdf(f"Form 16A - FY {fy} {q}",
                   [Section("Summary", lines),
                    Section("Deposit-wise interest and TDS", table=(["Deposit ref", "From", "To", "Interest", "TDS"], rows or [["-", "-", "-", "Rs. 0.00", "Rs. 0.00"]]))],
                   footer_note="Figures are computed from deposits in core banking. Verify against Form 26AS / AIS.",
                   mock=is_mock(conn, "finacle"))
    existing = one(conn, "SELECT id FROM tax_forms WHERE customer_id = ? AND form_type = '16A' AND financial_year = ? AND quarter = ?",
                   (cust["id"], fy, q))
    fid = existing["id"] if existing else new_id()
    doc = create_document(conn, owner_type="tax_form", owner_id=fid, doc_type="form_16a", data=pdf, file_name=f"form16a-{fy}-{q}.pdf")
    if existing:
        conn.execute("UPDATE tax_forms SET document_id = ? WHERE id = ?", (doc["id"], fid))
    else:
        conn.execute(
            """INSERT INTO tax_forms (id, customer_id, form_type, financial_year, quarter, document_id, status, created_at)
               VALUES (?, ?, '16A', ?, ?, ?, 'generated', ?)""",
            (fid, cust["id"], fy, q, doc["id"], now_iso()),
        )
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="form16a-{fy}-{q}.pdf"'})


@router.get("/me/tax-forms/{form_id}/document")
def tax_form_document(form_id: str, ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    row = one(conn, "SELECT * FROM tax_forms WHERE id = ?", (form_id[:64],))
    if not row:
        raise not_found("Tax form")
    if row["customer_id"] != ctx.customer_id:
        raise forbidden("This form does not belong to you")
    return _stream_document(conn, row["document_id"])

"""Outward remittances under the Liberalised Remittance Scheme (US-31): quote, Form A2, SWIFT gpi tracking.

LRS is available to resident individuals only. The annual LRS limit (USD), the TCS
threshold and the TCS rates are CMS-held regulatory values (flagged for compliance
verification). FX rates are mock card rates (mid plus spread). The INR amount plus TCS is
debited in Finacle with an idempotent posting reference, then the payment is sent over
SWIFT and tracked through gpi.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from audit import audit_ctx
from banking import account_number, customer_for, owned_account
from core import (api_error, client_ip, financial_year, format_inr, get_reg_value, invalid, iso, jdump, jload, many,
                  mask_account, new_id, not_found, forbidden, now_iso, one, parse_iso, today_ist, utcnow, valid_swift)
from crypto import decrypt_str, encrypt_str
from db import get_db
from integrations import finacle, swift
from integrations.base import is_mock, mock_meta
from notify import notify
from security import AuthContext, consume_mfa_token, require_customer

router = APIRouter(tags=["lrs"])

QUOTE_TTL_MINUTES = 10
FX_SPREAD_PERCENT = 0.75
# Mock mid-market rates (INR per unit). A live treasury feed replaces these.
FX_MID = {"USD": 84.10, "GBP": 108.35, "EUR": 91.20, "AED": 22.90, "SGD": 63.40, "AUD": 55.20, "CAD": 61.10}
CURRENCY_NAMES = {"USD": "US dollar", "GBP": "Pound sterling", "EUR": "Euro", "AED": "UAE dirham", "SGD": "Singapore dollar",
                  "AUD": "Australian dollar", "CAD": "Canadian dollar"}

# Common RBI purpose codes for LRS (to be verified by compliance against the current RBI list).
PURPOSE_CODES = {
    "S0305": {"label": "Travel for education (fees, hostel and related expenses)", "tcs_category": "education"},
    "S0304": {"label": "Travel for medical treatment", "tcs_category": "medical"},
    "S0306": {"label": "Other travel (holiday trips, private visits)", "tcs_category": "general"},
    "S0301": {"label": "Business travel", "tcs_category": "general"},
    "S0303": {"label": "Travel for pilgrimage", "tcs_category": "general"},
    "S1302": {"label": "Personal gifts and donations", "tcs_category": "general"},
    "S1301": {"label": "Maintenance of close relatives abroad", "tcs_category": "general"},
    "S0001": {"label": "Portfolio investment abroad - equity shares", "tcs_category": "general"},
    "S0023": {"label": "Opening a foreign currency account abroad", "tcs_category": "general"},
}

FORM_A2_DECLARATION = (
    "I declare that the total amount of foreign exchange purchased from or remitted through all sources in India during "
    "this financial year, including this remittance, is within the limit prescribed by the Reserve Bank of India under the "
    "Liberalised Remittance Scheme; that the money is my own and is not borrowed from any other person or institution; "
    "that this remittance is not for any purpose prohibited under Schedule I or restricted under Schedule II of the "
    "Foreign Exchange Management (Current Account Transactions) Rules, 2000; that this remittance is not to a country or "
    "entity identified as non-co-operative by the FATF; and that the information given is true and complete."
)
BENEFICIARY_ACCOUNT_RE = re.compile(r"^[A-Z0-9]{5,34}$")


class Body(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class QuoteIn(Body):
    currency: Optional[str] = None
    fx_amount_minor: Any = None
    purpose_code: Optional[str] = None
    education_loan_financed: Optional[bool] = False


class RemitIn(Body):
    quote_id: Optional[str] = None
    from_account_id: Optional[str] = None
    beneficiary_name: Optional[str] = None
    beneficiary_account: Optional[str] = None
    beneficiary_address: Optional[str] = None
    swift_bic: Optional[str] = None
    form_a2_declaration: Optional[bool] = None
    mfa_token: Optional[str] = None


# ------------------------------------------------------------------ helpers
def _fy_start(d: date | None = None) -> date:
    d = d or today_ist()
    return date(d.year if d.month >= 4 else d.year - 1, 4, 1)


def _require_resident(conn: sqlite3.Connection, ctx: AuthContext) -> dict:
    cust = customer_for(conn, str(ctx.customer_id))
    if cust.get("residency_status") != "resident":
        raise forbidden("The Liberalised Remittance Scheme applies to resident individuals only. As a non-resident you can "
                        "remit from your NRE or NRO account; please contact your relationship manager.", "lrs_residents_only")
    return cust


def _fy_used(conn: sqlite3.Connection, customer_id: str) -> int:
    """INR remitted under LRS this financial year (excluding failed remittances)."""
    start = _fy_start()
    # created_at is UTC; FY boundaries are IST dates. 1 April 00:00 IST is 31 March 18:30 UTC.
    since = iso(parse_iso(start.isoformat() + "T00:00:00"))
    row = conn.execute(
        "SELECT COALESCE(SUM(inr_amount_paise), 0) FROM lrs_remittances WHERE customer_id = ? AND created_at >= ? AND status != 'failed'",
        (customer_id, since)).fetchone()
    return int(row[0] or 0)


def _tcs_rate(conn: sqlite3.Connection, purpose_code: str, education_loan: bool) -> float:
    cat = PURPOSE_CODES[purpose_code]["tcs_category"]
    if cat == "education" and education_loan:
        return float(get_reg_value(conn, "tcs_rate_percent_education_loan", 0))
    if cat in ("education", "medical"):
        return float(get_reg_value(conn, "tcs_rate_percent_education_medical", 5))
    return float(get_reg_value(conn, "tcs_rate_percent_general", 20))


def _limits(conn: sqlite3.Connection) -> tuple[int, int]:
    return int(get_reg_value(conn, "lrs_annual_limit_usd", 250000)), int(get_reg_value(conn, "tcs_threshold_paise", 100000000))


def _usd(inr_paise: int) -> float:
    return round(inr_paise / 100.0 / FX_MID["USD"], 2)


def _public_remittance(r: dict) -> dict:
    acct = decrypt_str(r["beneficiary_account"]) or ""
    return {
        "id": r["id"], "quote_id": r["quote_id"], "from_account_id": r["from_account_id"], "beneficiary_name": r["beneficiary_name"],
        "beneficiary_account_masked": mask_account(acct), "beneficiary_address": r["beneficiary_address"], "swift_bic": r["swift_bic"],
        "currency": r["currency"], "fx_amount_minor": r["fx_amount_minor"], "fx_rate": r["fx_rate"],
        "inr_amount_paise": r["inr_amount_paise"], "purpose_code": r["purpose_code"],
        "purpose_label": PURPOSE_CODES.get(r["purpose_code"], {}).get("label"), "tcs_paise": r["tcs_paise"],
        "total_debit_paise": r["inr_amount_paise"] + r["tcs_paise"], "swift_uetr": r["swift_uetr"],
        "finacle_posting_ref": r["finacle_posting_ref"], "status": r["status"], "created_at": r["created_at"],
        "form_a2": {k: v for k, v in (jload(r["form_a2"], {}) or {}).items() if k in ("declared", "declared_at", "form", "version")},
    }


# ------------------------------------------------------------------ endpoints
@router.get("/me/lrs-remittances/purpose-codes")
def purpose_codes(ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    return {
        "items": [{"code": k, **v, "tcs_rate_percent": _tcs_rate(conn, k, False)} for k, v in PURPOSE_CODES.items()],
        "currencies": [{"code": c, "name": CURRENCY_NAMES[c]} for c in FX_MID],
        "needs_compliance_verification": True,
        "note": "Purpose codes, LRS limit and TCS rates are to be verified by the bank's compliance team.",
    }


@router.post("/me/lrs-remittances/quote", status_code=201)
def quote(body: QuoteIn, request: Request, ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    _require_resident(conn, ctx)
    currency = (body.currency or "").upper()
    if currency not in FX_MID:
        raise invalid("currency", "Choose USD, GBP, EUR, AED, SGD, AUD or CAD")
    amt = body.fx_amount_minor
    if isinstance(amt, float) and amt.is_integer():
        amt = int(amt)
    if isinstance(amt, bool) or not isinstance(amt, int) or amt <= 0:
        raise invalid("fx_amount_minor", "Enter an amount greater than zero")
    purpose = (body.purpose_code or "").upper()
    if purpose not in PURPOSE_CODES:
        raise invalid("purpose_code", "Choose the purpose of the remittance")
    mid = FX_MID[currency]
    rate = round(mid * (1 + FX_SPREAD_PERCENT / 100), 4)
    inr = int(round(amt * rate))  # minor units x INR/unit = paise
    limit_usd, threshold = _limits(conn)
    used_inr = _fy_used(conn, str(ctx.customer_id))
    used_usd, this_usd = _usd(used_inr), _usd(inr)
    if used_usd + this_usd > limit_usd:
        raise invalid("fx_amount_minor",
                      f"This remittance (about USD {this_usd:,.2f}) would take you over the LRS limit of USD {limit_usd:,} for "
                      f"FY {financial_year()}. You have used about USD {used_usd:,.2f}.", code="lrs_limit_exceeded")
    education_loan = bool(body.education_loan_financed) and PURPOSE_CODES[purpose]["tcs_category"] == "education"
    tcs_rate = _tcs_rate(conn, purpose, education_loan)
    taxable = min(inr, max(0, used_inr + inr - threshold))
    tcs = int(round(taxable * tcs_rate / 100))
    expires = utcnow() + timedelta(minutes=QUOTE_TTL_MINUTES)
    details = {
        "mid_rate": mid, "spread_percent": FX_SPREAD_PERCENT, "tcs_rate_percent": tcs_rate, "tcs_taxable_paise": taxable,
        "tcs_threshold_paise": threshold, "education_loan_financed": education_loan, "lrs_limit_usd": limit_usd,
        "lrs_used_inr_paise": used_inr, "lrs_used_usd": used_usd, "this_usd": this_usd, "financial_year": financial_year(),
    }
    q = {"id": new_id(), "customer_id": ctx.customer_id, "currency": currency, "fx_amount_minor": amt, "purpose_code": purpose,
         "fx_rate": rate, "inr_amount_paise": inr, "tcs_paise": tcs, "details": jdump(details), "created_at": now_iso(),
         "expires_at": iso(expires)}
    conn.execute(
        """INSERT INTO lrs_quotes (id, customer_id, currency, fx_amount_minor, purpose_code, fx_rate, inr_amount_paise, tcs_paise,
           details, created_at, expires_at) VALUES (:id, :customer_id, :currency, :fx_amount_minor, :purpose_code, :fx_rate,
           :inr_amount_paise, :tcs_paise, :details, :created_at, :expires_at)""",
        q,
    )
    audit_ctx(conn, ctx, "financial", "lrs.quote_created", entity_type="lrs_quote", entity_id=q["id"],
              details={"currency": currency, "fx_amount_minor": amt, "purpose_code": purpose, "inr_amount_paise": inr,
                       "tcs_paise": tcs}, request=request)
    return {
        "quote_id": q["id"], "currency": currency, "fx_amount_minor": amt, "purpose_code": purpose,
        "purpose_label": PURPOSE_CODES[purpose]["label"], "fx_rate": rate, "mid_rate": mid, "spread_percent": FX_SPREAD_PERCENT,
        "inr_amount_paise": inr, "tcs_rate_percent": tcs_rate, "tcs_paise": tcs, "tcs_taxable_paise": taxable,
        "total_debit_paise": inr + tcs, "lrs_used_usd": used_usd, "lrs_used_inr_paise": used_inr, "lrs_limit_usd": limit_usd,
        "lrs_remaining_usd": round(limit_usd - used_usd, 2), "this_remittance_usd": this_usd,
        "tcs_threshold_paise": threshold, "financial_year": financial_year(), "expires_at": q["expires_at"],
        "valid_minutes": QUOTE_TTL_MINUTES, "form_a2_declaration_text": FORM_A2_DECLARATION,
        "needs_compliance_verification": True, "mock": {"provider": "fx_rates", "mode": "MOCK", "label": "MOCK FX rates"},
    }


@router.post("/me/lrs-remittances", status_code=201)
def remit(body: RemitIn, request: Request, ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    cust = _require_resident(conn, ctx)
    q = one(conn, "SELECT * FROM lrs_quotes WHERE id = ?", (str(body.quote_id or "")[:64],))
    if not q or q["customer_id"] != ctx.customer_id:
        raise invalid("quote_id", "Quote not found. Get a new quote.", code="quote_invalid")
    if q["used_at"]:
        raise invalid("quote_id", "This quote has already been used. Get a new quote.", code="quote_used")
    if parse_iso(q["expires_at"]) <= utcnow():  # type: ignore[operator]
        raise api_error(410, "quote_expired", "The quote has expired (quotes are valid for 10 minutes). Get a new quote.",
                        field="quote_id")
    if body.form_a2_declaration is not True:
        raise invalid("form_a2_declaration", "Confirm the Form A2 declaration to continue")
    name = (body.beneficiary_name or "").strip()
    if not (2 <= len(name) <= 140):
        raise invalid("beneficiary_name", "Enter the beneficiary's full name")
    ben_acct = re.sub(r"[\s-]", "", (body.beneficiary_account or "")).upper()
    if not BENEFICIARY_ACCOUNT_RE.match(ben_acct):
        raise invalid("beneficiary_account", "Enter the beneficiary account number or IBAN (5 to 34 letters and digits)")
    address = (body.beneficiary_address or "").strip()
    if not (5 <= len(address) <= 200):
        raise invalid("beneficiary_address", "Enter the beneficiary's address (5 to 200 characters)")
    bic = (body.swift_bic or "").strip().upper()
    if not valid_swift(bic):
        raise invalid("swift_bic", "Enter a valid SWIFT BIC (8 or 11 characters, for example SYNTUS33 or SYNTUS33XXX)")
    if body.from_account_id:
        acct = owned_account(conn, ctx.customer_id, body.from_account_id)
    else:
        acct = one(conn, """SELECT * FROM accounts WHERE customer_id = ? AND owner_type = 'customer' AND account_type IN
                            ('savings','current') ORDER BY created_at LIMIT 1""", (ctx.customer_id,))
        if not acct:
            raise invalid("from_account_id", "Choose the account to debit")
    if acct["account_type"] not in ("savings", "current") or acct["currency"] != "INR":
        raise invalid("from_account_id", "Choose an INR savings or current account to debit")
    limit_usd, _threshold = _limits(conn)
    used_inr = _fy_used(conn, str(ctx.customer_id))
    if _usd(used_inr) + _usd(q["inr_amount_paise"]) > limit_usd:
        raise invalid("quote_id", f"This remittance would exceed the LRS limit of USD {limit_usd:,} for FY {financial_year()}.",
                      code="lrs_limit_exceeded")
    consume_mfa_token(conn, ctx, body.mfa_token, "lrs_remittance")

    rem_id = new_id()
    total = q["inr_amount_paise"] + q["tcs_paise"]
    posting = finacle.get(conn).post(
        account_number(acct), "DR", total,
        f"LRS {q['currency']} {q['fx_amount_minor'] / 100:.2f} {q['purpose_code']} incl TCS {format_inr(q['tcs_paise'])}",
        idempotency_ref=f"LRS-{q['id']}", channel_ref=rem_id)
    sent = swift.get(conn).send({"swift_bic": bic, "currency": q["currency"], "amount_minor": q["fx_amount_minor"],
                                 "beneficiary_name": name, "remittance_id": rem_id})
    now = now_iso()
    form_a2 = {
        "form": "A2", "version": "1.0", "declared": True, "declared_at": now, "declaration_text": FORM_A2_DECLARATION,
        "ip": client_ip(request), "user_agent": (request.headers.get("user-agent") or "")[:200],
        "remitter_name": cust["full_name"], "pan_masked": cust.get("pan_masked"), "purpose_code": q["purpose_code"],
        "currency": q["currency"], "fx_amount_minor": q["fx_amount_minor"], "inr_amount_paise": q["inr_amount_paise"],
        "tcs_paise": q["tcs_paise"], "financial_year": financial_year(),
    }
    row = {
        "id": rem_id, "customer_id": ctx.customer_id, "quote_id": q["id"], "from_account_id": acct["id"], "beneficiary_name": name,
        "beneficiary_account": encrypt_str(ben_acct), "beneficiary_address": address, "swift_bic": bic, "currency": q["currency"],
        "fx_amount_minor": q["fx_amount_minor"], "fx_rate": q["fx_rate"], "inr_amount_paise": q["inr_amount_paise"],
        "purpose_code": q["purpose_code"], "tcs_paise": q["tcs_paise"], "form_a2": jdump(form_a2), "swift_uetr": sent["uetr"],
        "finacle_posting_ref": posting["posting_ref"], "status": "sent_to_swift", "created_at": now,
    }
    conn.execute(
        """INSERT INTO lrs_remittances (id, customer_id, quote_id, from_account_id, beneficiary_name, beneficiary_account,
           beneficiary_address, swift_bic, currency, fx_amount_minor, fx_rate, inr_amount_paise, purpose_code, tcs_paise, form_a2,
           swift_uetr, finacle_posting_ref, status, created_at) VALUES (:id, :customer_id, :quote_id, :from_account_id,
           :beneficiary_name, :beneficiary_account, :beneficiary_address, :swift_bic, :currency, :fx_amount_minor, :fx_rate,
           :inr_amount_paise, :purpose_code, :tcs_paise, :form_a2, :swift_uetr, :finacle_posting_ref, :status, :created_at)""",
        row,
    )
    conn.execute("UPDATE lrs_quotes SET used_at = ? WHERE id = ?", (now, q["id"]))
    audit_ctx(conn, ctx, "financial", "lrs.remittance_sent", entity_type="lrs_remittance", entity_id=rem_id,
              details={"currency": q["currency"], "fx_amount_minor": q["fx_amount_minor"], "inr_amount_paise": q["inr_amount_paise"],
                       "tcs_paise": q["tcs_paise"], "purpose_code": q["purpose_code"], "swift_bic": bic, "uetr": sent["uetr"],
                       "posting_ref": posting["posting_ref"], "form_a2": True, "beneficiary_account": mask_account(ben_acct)},
              request=request)
    notify(conn, ctx.user_id, "transaction", "Foreign remittance sent",
           f"{q['currency']} {q['fx_amount_minor'] / 100:,.2f} to {name} sent via SWIFT (UETR {sent['uetr'][:8]}...). "
           f"{format_inr(total)} debited from {acct['masked_number']} incl. TCS {format_inr(q['tcs_paise'])}. "
           "Not you? Report fraud immediately.")
    out = _public_remittance(row)
    out["mock"] = [m for m in (sent.get("mock"), posting.get("mock")) if m]
    return out


@router.get("/me/lrs-remittances")
def list_remittances(ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    rows = many(conn, "SELECT * FROM lrs_remittances WHERE customer_id = ? ORDER BY created_at DESC LIMIT 100", (ctx.customer_id,))
    cust = customer_for(conn, str(ctx.customer_id))
    limit_usd, threshold = _limits(conn)
    used = _fy_used(conn, str(ctx.customer_id))
    return {
        "items": [_public_remittance(r) for r in rows],
        "eligible": cust.get("residency_status") == "resident",
        "residency_status": cust.get("residency_status"),
        "financial_year": financial_year(),
        "lrs_used_inr_paise": used,
        "lrs_used_usd": _usd(used),
        "lrs_limit_usd": limit_usd,
        "tcs_threshold_paise": threshold,
    }


@router.get("/me/lrs-remittances/{remittance_id}/tracking")
def tracking(remittance_id: str, ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    r = one(conn, "SELECT * FROM lrs_remittances WHERE id = ?", (str(remittance_id)[:64],))
    if not r:
        raise not_found("Remittance")
    if r["customer_id"] != ctx.customer_id:
        raise forbidden("This remittance does not belong to you")
    if not r["swift_uetr"]:
        return {"uetr": None, "events": [], "transaction_status": None, "completed": False, "status": r["status"]}
    tr = swift.get(conn).track(r["swift_uetr"], submitted_at=r["created_at"], currency=r["currency"], beneficiary_bic=r["swift_bic"])
    if tr.get("completed") and r["status"] != "credited":
        conn.execute("UPDATE lrs_remittances SET status = 'credited' WHERE id = ?", (r["id"],))
        r["status"] = "credited"
    return {**tr, "status": r["status"], "remittance_id": r["id"],
            "mock": tr.get("mock") or (mock_meta("swift") if is_mock(conn, "swift") else None)}

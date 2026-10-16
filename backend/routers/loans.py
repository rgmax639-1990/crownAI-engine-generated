"""Digital loans (US-16): personal and MSME loans with consent-based underwriting.

Journey (enforced in this order, 409 when a step is out of order):
  create (draft) -> consents (consented; AA consent artefact created)
  -> fetch-data (data_fetching -> underwritten | referred | declined)
  -> KFS (Key Fact Statement with APR, charges, cooling-off) -> accept (kfs_accepted)
  -> eSign (esigned; signed agreement stored) -> e-NACH (UMRN) -> submitted

Pricing always uses the LIVE published rate (rates_service). APR is the annualised IRR of
the net amount disbursed against the EMI stream. Every step is audited and alerted.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict

from audit import audit_ctx
from banking import account_number, customer_for, owned_account
from consent_service import consent_active, record_consent
from core import (ACCOUNT_RE, BRAND_NAME, api_error, fmt_date, fmt_ist, forbidden, format_inr, invalid, jdump, jload,
                  mask_account, new_id, not_found, now_iso, one, sha256_hex, today_ist, valid_gstin, valid_ifsc)
from db import get_db, register_ddl
from integrations import account_aggregator, bureau, esign, gstn, itr, npci_nach
from integrations.base import is_mock, mock_meta
from notify import notify
from objectstore import create_document, get_bytes
from pdfgen import Section, make_pdf
from rates_service import find_rate, published_rates
from security import AuthContext, require_customer

router = APIRouter(tags=["loans"])

register_ddl(
    """
CREATE TABLE IF NOT EXISTS loan_application_meta (
  application_id TEXT PRIMARY KEY,
  data TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);
"""
)

LOAN_TYPES = ("personal", "msme")
AMOUNT_LIMITS = {"personal": (5_000_000, 400_000_000), "msme": (10_000_000, 2_000_000_000)}  # Rs.50k-40L / Rs.1L-200L
TENURE_MIN, TENURE_MAX = 6, 84
RATE_PRODUCT = {"personal": "personal_loan", "msme": "msme_loan"}
MIN_SCORE, REFER_BELOW_SCORE = 650, 700
MAX_FOIR = {"personal": 0.50, "msme": 0.60}
MSME_NET_MARGIN = 0.10  # assumed net margin on GST turnover (credit policy assumption)
FEE_POLICY = {
    "personal": {"percent": 1.5, "min": 100_000, "max": 1_500_000},
    "msme": {"percent": 1.0, "min": 250_000, "max": 5_000_000},
}
GST_PERCENT = 18
STAMP_DUTY_PAISE = 50_000
COOLING_OFF_DAYS = 3
COMPLIANCE_NOTE = "Synthetic values to be verified by the bank's compliance team before go-live."

STATUS_NEXT = {
    "draft": "consent", "consented": "fetch_data", "data_fetching": "fetch_data", "underwritten": "kfs",
    "kfs_accepted": "esign", "esigned": "enach", "submitted": "done", "referred": "referred", "declined": "declined",
}

CONSENT_PURPOSES = {
    "aa_data_fetch": {
        "en": {"label": "Account Aggregator data fetch",
               "purpose": "Fetch the last 6 months of your bank statements through a licensed Account Aggregator to assess your "
                          "income and existing obligations for this loan application only.",
               "duration": "One-time fetch of 6 months' data; the consent is valid for 30 days or until you withdraw it."},
        "hi": {"label": "अकाउंट एग्रीगेटर डेटा",
               "purpose": "केवल इस ऋण आवेदन हेतु आपकी आय और मौजूदा दायित्वों के आकलन के लिए लाइसेंस प्राप्त अकाउंट एग्रीगेटर के माध्यम "
                          "से पिछले 6 महीनों के बैंक स्टेटमेंट प्राप्त करना।",
               "duration": "6 महीनों का डेटा एक बार प्राप्त किया जाएगा; सहमति 30 दिनों तक या वापस लेने तक मान्य है।"},
    },
    "bureau_pull": {
        "en": {"label": "Credit bureau check",
               "purpose": "Obtain your credit report and score from a credit information company to assess this loan application.",
               "duration": "One-time pull for this application; the report is retained for the period required by law."},
        "hi": {"label": "क्रेडिट ब्यूरो जाँच",
               "purpose": "इस ऋण आवेदन के आकलन हेतु क्रेडिट सूचना कंपनी से आपकी क्रेडिट रिपोर्ट और स्कोर प्राप्त करना।",
               "duration": "इस आवेदन के लिए एक बार; रिपोर्ट कानून द्वारा अपेक्षित अवधि तक रखी जाती है।"},
    },
    "gstn_itr_fetch": {
        "en": {"label": "GST and income-tax returns fetch",
               "purpose": "Fetch your GST returns (last 12 months) and income-tax returns (last 2 years) to assess business "
                          "turnover and income for this MSME loan application.",
               "duration": "One-time fetch; the consent is valid for 30 days or until you withdraw it."},
        "hi": {"label": "GST और आयकर रिटर्न",
               "purpose": "इस MSME ऋण आवेदन हेतु व्यवसाय टर्नओवर और आय के आकलन के लिए आपके GST रिटर्न (पिछले 12 महीने) और आयकर "
                          "रिटर्न (पिछले 2 वर्ष) प्राप्त करना।",
               "duration": "एक बार प्राप्त किया जाएगा; सहमति 30 दिनों तक या वापस लेने तक मान्य है।"},
    },
}

GRIEVANCE_OFFICER = {
    "name": "Loans Grievance Redressal Officer (synthetic)",
    "designation": "Grievance Redressal Officer - Retail and MSME Loans",
    "phone": "1800-000-0001",
    "email": "loans.grievance@example.bank.in",
    "address": f"{BRAND_NAME}, Synthetic Towers, Bandra Kurla Complex, Mumbai 400051",
    "escalation": "If your complaint is not resolved within 30 days, or you are not satisfied with the reply, you can "
                  "approach the RBI Ombudsman at https://cms.rbi.org.in.",
}

RECOVERY_POLICY = (
    "The bank may engage recovery agents only after giving you due notice. Agents carry the bank's authorisation letter "
    "and identity card, may contact you only between 8:00 a.m. and 7:00 p.m., must follow the RBI Fair Practices Code "
    "and the bank's code of conduct, and will never use intimidation, harassment or abusive language. The list of "
    "empanelled agencies is published on the bank's website. " + COMPLIANCE_NOTE
)


class Body(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class CreateIn(Body):
    loan_type: Optional[str] = None
    amount_paise: Any = None
    tenure_months: Any = None
    gstin: Optional[str] = None
    calculator_share_id: Optional[str] = None


class ConsentsIn(Body):
    notice_version_id: Optional[str] = None
    aa_consent: Optional[bool] = None
    bureau_consent: Optional[bool] = None
    gstn_consent: Optional[bool] = None
    language: Optional[str] = "en"


class AcceptIn(Body):
    accepted: Optional[bool] = None


class EnachIn(Body):
    account_id: Optional[str] = None
    account_number: Optional[str] = None
    ifsc: Optional[str] = None
    auth_mode: Optional[str] = None


# ------------------------------------------------------------------ maths
def emi_for(principal_paise: int, annual_rate_percent: float, months: int) -> int:
    r = annual_rate_percent / 1200.0
    if r <= 0:
        return int(math.ceil(principal_paise / months))
    f = (1 + r) ** months
    return int(round(principal_paise * r * f / (f - 1)))


def _pv(payment: float, r: float, n: int) -> float:
    return payment * n if r == 0 else payment * (1 - (1 + r) ** -n) / r


def apr_percent(net_disbursed_paise: int, emi_paise: int, months: int) -> float:
    """Annualised IRR (monthly IRR x 12, as in the RBI KFS illustration) of net disbursal vs the EMI stream."""
    if net_disbursed_paise <= 0 or emi_paise <= 0:
        return 0.0
    if _pv(emi_paise, 0.0, months) <= net_disbursed_paise:
        return 0.0
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if _pv(emi_paise, mid, months) > net_disbursed_paise:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2 * 12 * 100, 2)


def _add_months(d: date, months: int, day: int = 5) -> date:
    y, m = d.year, d.month + months
    while m > 12:
        m -= 12
        y += 1
    return date(y, m, day)


def repayment_schedule(principal: int, rate_percent: float, months: int, emi: int, start: date | None = None) -> list[dict]:
    r = rate_percent / 1200.0
    bal = principal
    start = start or today_ist()
    rows = []
    for k in range(1, months + 1):
        interest = int(round(bal * r))
        princ = emi - interest
        if k == months or princ > bal:
            princ = bal
        instalment = princ + interest
        opening = bal
        bal -= princ
        rows.append({"instalment_no": k, "due_date": _add_months(start, k).isoformat(), "opening_balance_paise": opening,
                     "principal_paise": princ, "interest_paise": interest, "instalment_paise": instalment,
                     "closing_balance_paise": max(bal, 0)})
    return rows


def fee_table(loan_type: str, amount: int) -> list[dict]:
    p = FEE_POLICY[loan_type]
    fee = max(p["min"], min(p["max"], int(round(amount * p["percent"] / 100))))
    gst = int(round(fee * GST_PERCENT / 100))
    return [
        {"code": "processing_fee", "name": "Processing fee", "payable_to": "Bank", "timing": "One-time, deducted from disbursal",
         "amount_paise": fee, "basis": f"{p['percent']}% of the loan amount (minimum {format_inr(p['min'])}, "
                                       f"maximum {format_inr(p['max'])})"},
        {"code": "gst_on_processing_fee", "name": f"GST on processing fee ({GST_PERCENT}%)", "payable_to": "Government",
         "timing": "One-time, deducted from disbursal", "amount_paise": gst, "basis": f"{GST_PERCENT}% of the processing fee"},
        {"code": "stamp_duty", "name": "Stamp duty (e-stamping)", "payable_to": "State government (third party)",
         "timing": "One-time, deducted from disbursal", "amount_paise": STAMP_DUTY_PAISE,
         "basis": "As per the applicable state stamp act (indicative; verify)"},
    ]


CONTINGENT_CHARGES = [
    {"name": "Late payment charge", "description": "2% per month on the overdue instalment amount, plus GST"},
    {"name": "Instalment bounce (NACH return) charge", "description": "Rs. 500 per instance, plus GST"},
    {"name": "Foreclosure / part-prepayment", "description": "Nil for floating-rate loans to individuals and micro and small "
                                                             "enterprises; up to 4% of the principal prepaid for fixed-rate loans"},
    {"name": "Duplicate statement / no-due certificate", "description": "Rs. 250 per request, plus GST"},
]


# ------------------------------------------------------------------ helpers
def _meta(conn: sqlite3.Connection, app_id: str) -> dict:
    row = one(conn, "SELECT data FROM loan_application_meta WHERE application_id = ?", (app_id,))
    return jload(row["data"], {}) if row else {}


def _meta_update(conn: sqlite3.Connection, app_id: str, event: str | None = None, **patch: Any) -> dict:
    data = _meta(conn, app_id)
    data.update(patch)
    if event:
        data.setdefault("timeline", []).append({"event": event, "at": now_iso()})
    conn.execute(
        """INSERT INTO loan_application_meta (application_id, data, updated_at) VALUES (?,?,?)
           ON CONFLICT(application_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at""",
        (app_id, jdump(data), now_iso()),
    )
    return data


def _own(conn: sqlite3.Connection, ctx: AuthContext, application_id: str) -> dict:
    app = one(conn, "SELECT * FROM loan_applications WHERE id = ?", (str(application_id)[:64],)) if application_id else None
    if not app:
        raise not_found("Loan application")
    if app["user_id"] != ctx.user_id:
        raise forbidden("This loan application does not belong to you")
    return app


def _set_status(conn: sqlite3.Connection, app_id: str, status: str, **cols: Any) -> None:
    sets = ["status = ?", "updated_at = ?"]
    vals: list[Any] = [status, now_iso()]
    for k, v in cols.items():
        sets.append(f"{k} = ?")
        vals.append(v)
    vals.append(app_id)
    conn.execute(f"UPDATE loan_applications SET {', '.join(sets)} WHERE id = ?", vals)


def _out_of_order(app: dict, needed: str) -> Exception:
    nice = {
        "consent": "give your consents", "fetch_data": "fetch your financial data", "kfs": "review and accept the Key Fact Statement",
        "esign": "eSign the loan agreement", "enach": "set up e-NACH", "done": "nothing more - the application is submitted",
        "referred": "wait for our credit team to review the application", "declined": "start a new application",
    }
    nxt = STATUS_NEXT.get(app["status"], "consent")
    return api_error(409, "invalid_step",
                     f"This step is not available yet: the application is '{app['status']}'. Next you need to {nice.get(nxt, nxt)}.",
                     status=app["status"], next_step=nxt, attempted=needed)


def _live_rate(conn: sqlite3.Connection, loan_type: str, tenure_months: int, amount_paise: int) -> dict:
    product = RATE_PRODUCT[loan_type]
    rate = find_rate(conn, product, tenure_days=tenure_months * 30, amount_paise=amount_paise)
    if not rate:
        live = published_rates(conn, product)
        rate = live[0] if live else None
    if not rate:
        raise api_error(422, "rate_unavailable",
                        "No published interest rate is live for this loan product right now. Please try again later.")
    return rate


def _rate_type(rate: dict) -> str:
    bm = rate.get("benchmark") or "none"
    if bm == "none":
        return "Fixed"
    return f"Floating (linked to {bm})"


def _public(conn: sqlite3.Connection, app: dict, meta: dict | None = None) -> dict:
    meta = meta if meta is not None else _meta(conn, app["id"])
    uw = jload(app["underwriting_result"], None)
    return {
        "id": app["id"],
        "loan_type": app["loan_type"],
        "amount_paise": app["amount_paise"],
        "tenure_months": app["tenure_months"],
        "gstin": app["gstin"],
        "calculator_share_id": app["calculator_share_id"],
        "status": app["status"],
        "next_step": STATUS_NEXT.get(app["status"], "consent"),
        "consents": meta.get("consents", []),
        "aa_consent_handle": app["aa_consent_handle"],
        "aa_redirect_url": meta.get("aa_redirect_url"),
        "bureau_score": app["bureau_score"],
        "underwriting_result": uw,
        "data_fetch": meta.get("data_fetch"),
        "interest_rate_percent": app["interest_rate_percent"],
        "apr_percent": app["apr_percent"],
        "processing_fee_paise": app["processing_fee_paise"],
        "emi_paise": app["emi_paise"],
        "sanctioned_amount_paise": (uw or {}).get("sanctioned_amount_paise"),
        "kfs_document_id": app["kfs_document_id"],
        "kfs_accepted_at": app["kfs_accepted_at"],
        "esign_ref": app["esign_ref"],
        "esign": meta.get("esign"),
        "agreement_document_id": meta.get("agreement_document_id"),
        "enach_mandate_ref": app["enach_mandate_ref"],
        "enach": meta.get("enach"),
        "timeline": meta.get("timeline", []),
        "created_at": app["created_at"],
        "updated_at": app["updated_at"],
    }


def _int_field(value: Any, field: str, message: str) -> int:
    if isinstance(value, bool):
        raise invalid(field, message)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int):
        raise invalid(field, message)
    return value


# ------------------------------------------------------------------ endpoints
@router.get("/loans/consent-purposes")
def consent_purposes(loan_type: str = "personal", locale: str = "en", ctx: AuthContext = Depends(require_customer)):
    loc = "hi" if locale == "hi" else "en"
    keys = ["aa_data_fetch", "bureau_pull"] + (["gstn_itr_fetch"] if loan_type == "msme" else [])
    return {"items": [{"purpose": k, "field": {"aa_data_fetch": "aa_consent", "bureau_pull": "bureau_consent",
                                                "gstn_itr_fetch": "gstn_consent"}[k], "required": True, **CONSENT_PURPOSES[k][loc]}
                      for k in keys]}


@router.get("/loans/applications")
def list_applications(ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute("SELECT * FROM loan_applications WHERE user_id = ? ORDER BY created_at DESC", (ctx.user_id,)).fetchall()
    return {"items": [_public(conn, dict(r)) for r in rows]}


@router.post("/loans/applications", status_code=201)
def create_application(body: CreateIn, request: Request, ctx: AuthContext = Depends(require_customer),
                       conn: sqlite3.Connection = Depends(get_db)):
    loan_type = (body.loan_type or "").lower()
    if loan_type not in LOAN_TYPES:
        raise invalid("loan_type", "Choose a personal loan or an MSME (business) loan")
    lo, hi = AMOUNT_LIMITS[loan_type]
    amount = _int_field(body.amount_paise, "amount_paise", "Enter the loan amount")
    if amount < lo or amount > hi:
        raise invalid("amount_paise", f"Enter an amount between {format_inr(lo)} and {format_inr(hi)}")
    tenure = _int_field(body.tenure_months, "tenure_months", "Enter the tenure in months")
    if tenure < TENURE_MIN or tenure > TENURE_MAX:
        raise invalid("tenure_months", f"Tenure must be between {TENURE_MIN} and {TENURE_MAX} months")
    gstin = None
    if loan_type == "msme":
        gstin = (body.gstin or "").upper()
        if not valid_gstin(gstin):
            raise invalid("gstin", "Enter a valid 15-character GSTIN, for example 27AAACS1234F1Z5")
    share_id = body.calculator_share_id or None
    if share_id:
        if len(share_id) > 64 or not one(conn, "SELECT id FROM calculator_shares WHERE id = ?", (share_id,)):
            raise invalid("calculator_share_id", "The shared calculation was not found. Start from the calculator again.")
    app_id = new_id()
    now = now_iso()
    conn.execute(
        """INSERT INTO loan_applications (id, user_id, loan_type, amount_paise, tenure_months, gstin, calculator_share_id,
           consent_record_ids, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,'[]','draft',?,?)""",
        (app_id, ctx.user_id, loan_type, amount, tenure, gstin, share_id, now, now),
    )
    meta = _meta_update(conn, app_id, "application_created")
    audit_ctx(conn, ctx, "financial", "loan.application_created", entity_type="loan_application", entity_id=app_id,
              details={"loan_type": loan_type, "amount_paise": amount, "tenure_months": tenure,
                       "calculator_share_id": share_id}, request=request)
    notify(conn, ctx.user_id, "transaction", "Loan application started",
           f"Your {loan_type} loan application for {format_inr(amount)} over {tenure} months has been started.")
    app = one(conn, "SELECT * FROM loan_applications WHERE id = ?", (app_id,))
    return _public(conn, app, meta)  # type: ignore[arg-type]


@router.get("/loans/applications/{application_id}")
def get_application(application_id: str, ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    return _public(conn, _own(conn, ctx, application_id))


@router.post("/loans/applications/{application_id}/consents")
def give_consents(application_id: str, body: ConsentsIn, request: Request, ctx: AuthContext = Depends(require_customer),
                  conn: sqlite3.Connection = Depends(get_db)):
    app = _own(conn, ctx, application_id)
    if app["status"] not in ("draft", "consented"):
        raise _out_of_order(app, "consent")
    version = (body.notice_version_id or "").strip()
    ok = version and len(version) <= 64 and conn.execute(
        """SELECT 1 FROM content_versions v JOIN content_items i ON i.id = v.content_item_id
           WHERE v.id = ? AND i.content_type = 'consent_notice' AND v.status IN ('published','superseded')""",
        (version,)).fetchone()
    if not ok:
        raise invalid("notice_version_id", "The consent notice version is unknown. Reload the page and try again.")
    if body.aa_consent is not True:
        raise invalid("aa_consent", "Allow the Account Aggregator data fetch to continue. We need it to assess your income.",
                      code="consent_required")
    if body.bureau_consent is not True:
        raise invalid("bureau_consent", "Allow the credit bureau check to continue. It is required for every loan.",
                      code="consent_required")
    purposes = ["aa_data_fetch", "bureau_pull"]
    if app["loan_type"] == "msme":
        if body.gstn_consent is not True:
            raise invalid("gstn_consent", "Allow the GST and income-tax returns fetch to continue with an MSME loan.",
                          code="consent_required")
        purposes.append("gstn_itr_fetch")
    lang = body.language if body.language in ("en", "hi") else "en"
    records, ids = [], []
    for p in purposes:
        rec = record_consent(conn, subject_type="user", subject_id=str(ctx.user_id), purpose=p, granted=True,
                             notice_version_id=version, language=lang)
        ids.append(rec["id"])
        text = CONSENT_PURPOSES[p][lang]
        records.append({"purpose": p, "consent_record_id": rec["id"], "label": text["label"], "purpose_text": text["purpose"],
                        "duration": text["duration"], "granted": True, "recorded_at": rec["recorded_at"]})
    aa = account_aggregator.get(conn).create_consent(
        subject_ref=str(ctx.user_id), purpose="Loan underwriting (FIU)", fi_types=["DEPOSIT"], duration_months=1,
        fetch_months=6, return_path=f"/portal/loans?application={app['id']}")
    prior_ids = jload(app["consent_record_ids"], []) or []
    _set_status(conn, app["id"], "consented", consent_record_ids=jdump(prior_ids + ids), aa_consent_handle=aa["consent_handle"])
    meta = _meta_update(conn, app["id"], "consents_recorded", consents=records, aa_redirect_url=aa["redirect_url"],
                        aa_consent={k: aa.get(k) for k in ("consent_handle", "status", "consent_expires_at", "fi_types")},
                        notice_version_id=version)
    audit_ctx(conn, ctx, "consent", "loan.consents_recorded", entity_type="loan_application", entity_id=app["id"],
              details={"purposes": purposes, "notice_version_id": version, "language": lang,
                       "aa_consent_handle": aa["consent_handle"]}, request=request)
    notify(conn, ctx.user_id, "transaction", "Loan consents recorded",
           "You allowed " + ", ".join(CONSENT_PURPOSES[p]["en"]["label"] for p in purposes)
           + " for your loan application. You can withdraw consent any time from Privacy choices.")
    app = _own(conn, ctx, app["id"])
    out = _public(conn, app, meta)
    out["aa"] = {"consent_handle": aa["consent_handle"], "redirect_url": aa["redirect_url"], "status": aa["status"],
                 "mock": aa.get("mock")}
    return out


def _underwrite(conn: sqlite3.Connection, ctx: AuthContext, app: dict, rate: dict) -> tuple[dict, dict]:
    customer = customer_for(conn, str(ctx.customer_id))
    loan_type = app["loan_type"]
    stages = []
    aa = account_aggregator.get(conn).fetch_statements(app["aa_consent_handle"] or "", subject_ref=str(ctx.user_id))
    stages.append({"stage": "account_aggregator", "status": "completed", "at": aa["fetched_at"]})
    bur = bureau.get(conn).pull(str(ctx.user_id), customer.get("pan_masked"))
    stages.append({"stage": "bureau", "status": "completed", "at": bur["pulled_at"]})
    gst = itr_data = None
    reasons: list[str] = []
    if loan_type == "msme":
        gst = gstn.get(conn).fetch_returns(app["gstin"] or "")
        stages.append({"stage": "gstn", "status": "completed", "at": gst["fetched_at"]})
        itr_data = itr.get(conn).fetch(customer.get("pan_masked") or str(ctx.user_id))
        stages.append({"stage": "itr", "status": "completed", "at": itr_data["fetched_at"]})
        income_gst = int(gst["annual_turnover_paise"] * MSME_NET_MARGIN / 12)
        income_itr = int(itr_data["latest_income_paise"] / 12)
        monthly_income = (income_gst + income_itr) // 2
        reasons.append(f"Assessed monthly income {format_inr(monthly_income)} (average of ITR income and "
                       f"{int(MSME_NET_MARGIN * 100)}% of GST turnover)")
    else:
        monthly_income = aa["avg_monthly_inflow_paise"]
        reasons.append(f"Average monthly inflow {format_inr(monthly_income)} from Account Aggregator statements")

    score = int(bur["score"])
    existing_emi = int(bur["total_emi_paise"])
    rate_pct = float(rate["rate_percent"])
    n = int(app["tenure_months"])
    r = rate_pct / 1200.0
    lo, hi = AMOUNT_LIMITS[loan_type]
    max_foir = MAX_FOIR[loan_type]
    requested = int(app["amount_paise"])
    available_emi = max(0.0, monthly_income * max_foir - existing_emi)
    eligible = int(_pv(available_emi, r, n)) // 100_000 * 100_000  # round down to Rs. 1,000
    eligible = max(0, min(hi, eligible))
    emi_requested = emi_for(requested, rate_pct, n)
    foir = round((existing_emi + emi_requested) / monthly_income, 4) if monthly_income > 0 else 9.99
    sanctioned = min(requested, eligible)

    decision = "approve"
    if score < MIN_SCORE:
        decision = "decline"
        reasons.append(f"Credit score {score} is below the minimum of {MIN_SCORE}")
    elif eligible < lo:
        decision = "decline"
        reasons.append(f"Eligible amount {format_inr(eligible)} is below the minimum loan of {format_inr(lo)} "
                       f"at a {int(max_foir * 100)}% FOIR limit")
    else:
        if score < REFER_BELOW_SCORE:
            decision = "refer"
            reasons.append(f"Credit score {score} needs a manual review (auto-approval from {REFER_BELOW_SCORE})")
        if aa.get("cheque_bounces_6m", 0) >= 3:
            decision = "refer"
            reasons.append("Three or more payment bounces in the last 6 months")
        if gst is not None and gst["returns_filed_12m"] < 10:
            decision = "refer"
            reasons.append(f"Only {gst['returns_filed_12m']} of 12 GST returns filed")
    if decision == "approve":
        reasons.append(f"Credit score {score} meets the policy")
        if sanctioned < requested:
            reasons.append(f"Approved for {format_inr(sanctioned)} (lower than requested) to keep FOIR within "
                           f"{int(max_foir * 100)}%")
        else:
            reasons.append(f"FOIR {foir * 100:.1f}% is within the {int(max_foir * 100)}% limit")

    uw = {
        "score": score,
        "foir": foir,
        "foir_percent": round(foir * 100, 2),
        "max_foir_percent": int(max_foir * 100),
        "monthly_income_paise": monthly_income,
        "existing_emi_paise": existing_emi,
        "eligible_amount_paise": eligible,
        "requested_amount_paise": requested,
        "sanctioned_amount_paise": sanctioned if decision == "approve" else None,
        "decision": decision,
        "reasons": reasons,
        "decided_at": now_iso(),
        "policy_note": "Synthetic credit policy for demonstration. " + COMPLIANCE_NOTE,
    }
    data = {
        "stages": stages,
        "aa_summary": {k: aa[k] for k in ("avg_monthly_inflow_paise", "avg_monthly_outflow_paise", "months", "cheque_bounces_6m")},
        "bureau": {"report_ref": bur["report_ref"], "score": score, "active_loans": bur["active_loans"],
                   "enquiries_6m": bur["enquiries_6m"]},
        "gstn": {"annual_turnover_paise": gst["annual_turnover_paise"], "returns_filed_12m": gst["returns_filed_12m"]} if gst else None,
        "itr": {"latest_income_paise": itr_data["latest_income_paise"], "returns": itr_data["returns"]} if itr_data else None,
        "mock": [mock_meta(p) for p in ("account_aggregator", "bureau") + (("gstn", "itr") if gst else ())
                 if is_mock(conn, p)],
    }
    return uw, data


@router.post("/loans/applications/{application_id}/fetch-data", status_code=202)
def fetch_data(application_id: str, request: Request, ctx: AuthContext = Depends(require_customer),
               conn: sqlite3.Connection = Depends(get_db)):
    app = _own(conn, ctx, application_id)
    if app["status"] not in ("consented", "data_fetching"):
        raise _out_of_order(app, "fetch_data")
    needed = ["aa_data_fetch", "bureau_pull"] + (["gstn_itr_fetch"] if app["loan_type"] == "msme" else [])
    withdrawn = [p for p in needed if not consent_active(conn, ctx.user_id, p)]
    if withdrawn:
        raise api_error(409, "consent_withdrawn",
                        "You have withdrawn a consent this application needs (" + ", ".join(withdrawn)
                        + "). Give your consent again to continue.", purposes=withdrawn)
    rate = _live_rate(conn, app["loan_type"], app["tenure_months"], app["amount_paise"])
    _set_status(conn, app["id"], "data_fetching")
    _meta_update(conn, app["id"], "data_fetch_started")
    uw, data = _underwrite(conn, ctx, app, rate)

    cols: dict[str, Any] = {"bureau_score": uw["score"], "underwriting_result": jdump(uw)}
    patch: dict[str, Any] = {"data_fetch": data}
    if uw["decision"] == "approve":
        amount = uw["sanctioned_amount_paise"]
        n = app["tenure_months"]
        rate_pct = float(rate["rate_percent"])
        emi = emi_for(amount, rate_pct, n)
        fees = fee_table(app["loan_type"], amount)
        upfront = sum(f["amount_paise"] for f in fees)
        cols.update({"interest_rate_percent": rate_pct, "emi_paise": emi, "processing_fee_paise": fees[0]["amount_paise"],
                     "apr_percent": apr_percent(amount - upfront, emi, n)})
        patch["pricing"] = {
            "rate_id": rate.get("id"), "rate_table_slug": rate.get("rate_table_slug"), "rate_percent": rate_pct,
            "benchmark": rate.get("benchmark"), "benchmark_rate_percent": rate.get("benchmark_rate_percent"),
            "spread_percent": rate.get("spread_percent"), "as_on_date": rate.get("as_on_date"),
            "rate_type": _rate_type(rate), "fees": fees, "upfront_charges_paise": upfront,
        }
        new_status = "underwritten"
    else:
        new_status = "referred" if uw["decision"] == "refer" else "declined"
    _set_status(conn, app["id"], new_status, **cols)
    meta = _meta_update(conn, app["id"], f"underwriting_{uw['decision']}", **patch)
    audit_ctx(conn, ctx, "financial", "loan.underwritten", entity_type="loan_application", entity_id=app["id"],
              details={"decision": uw["decision"], "score": uw["score"], "foir": uw["foir"],
                       "eligible_amount_paise": uw["eligible_amount_paise"]}, request=request)
    msg = {
        "approve": f"Good news: your loan offer for {format_inr(uw['sanctioned_amount_paise'])} is ready. Review the Key Fact Statement.",
        "refer": "Your loan application needs a review by our credit team. We will contact you within 2 working days.",
        "decline": "We are unable to offer a loan on this application right now. See the reasons in internet banking.",
    }[uw["decision"]]
    notify(conn, ctx.user_id, "transaction", "Loan application update", msg)
    app = _own(conn, ctx, app["id"])
    return _public(conn, app, meta)


# ------------------------------------------------------------------ KFS
def _kfs(conn: sqlite3.Connection, app: dict, meta: dict) -> dict:
    uw = jload(app["underwriting_result"], {}) or {}
    pricing = meta.get("pricing") or {}
    amount = int(uw.get("sanctioned_amount_paise") or app["amount_paise"])
    n = int(app["tenure_months"])
    rate = float(app["interest_rate_percent"] or 0)
    emi = int(app["emi_paise"] or 0)
    fees = pricing.get("fees") or fee_table(app["loan_type"], amount)
    upfront = sum(f["amount_paise"] for f in fees)
    start = date.fromisoformat(app["created_at"][:10])
    schedule = repayment_schedule(amount, rate, n, emi, start)
    total_interest = sum(r["interest_paise"] for r in schedule)
    total_instalments = sum(r["instalment_paise"] for r in schedule)
    return {
        "application_id": app["id"],
        "kfs_version": "1.0",
        "lender": BRAND_NAME,
        "loan_type": app["loan_type"],
        "loan_amount_paise": amount,
        "tenure_months": n,
        "instalment_frequency": "Monthly",
        "number_of_instalments": n,
        "rate_type": pricing.get("rate_type") or "Fixed",
        "interest_rate_percent": rate,
        "benchmark": pricing.get("benchmark"),
        "benchmark_rate_percent": pricing.get("benchmark_rate_percent"),
        "spread_percent": pricing.get("spread_percent"),
        "rate_as_on_date": pricing.get("as_on_date"),
        "apr_percent": app["apr_percent"],
        "apr_method": "Annualised internal rate of return (monthly IRR x 12) of the net amount disbursed against the EMI stream",
        "emi_paise": emi,
        "fees_and_charges": fees,
        "upfront_charges_paise": upfront,
        "net_disbursed_amount_paise": amount - upfront,
        "total_interest_paise": total_interest,
        "total_amount_payable_paise": total_instalments + upfront,
        "contingent_charges": CONTINGENT_CHARGES,
        "cooling_off": {
            "days": COOLING_OFF_DAYS,
            "text": f"You can exit the loan within {COOLING_OFF_DAYS} days of disbursal (cooling-off / look-up period) by "
                    "repaying the principal and the proportionate interest, without any penalty.",
            "needs_compliance_verification": True,
        },
        "grievance_officer": GRIEVANCE_OFFICER,
        "recovery_agent_policy": RECOVERY_POLICY,
        "repayment_schedule": schedule,
        "first_instalment_date": schedule[0]["due_date"] if schedule else None,
        "kfs_document_id": app["kfs_document_id"],
        "accepted_at": app["kfs_accepted_at"],
        "needs_compliance_verification": True,
        "compliance_note": COMPLIANCE_NOTE,
    }


def _kfs_pdf(conn: sqlite3.Connection, kfs: dict) -> bytes:
    fees_rows = [[f["name"], f["payable_to"], f["timing"], format_inr(f["amount_paise"])] for f in kfs["fees_and_charges"]]
    sched = [[r["instalment_no"], fmt_date(r["due_date"]), format_inr(r["opening_balance_paise"]), format_inr(r["principal_paise"]),
              format_inr(r["interest_paise"]), format_inr(r["instalment_paise"]), format_inr(r["closing_balance_paise"])]
             for r in kfs["repayment_schedule"]]
    sections = [
        Section("Part 1 - Interest rate and fees / charges", [
            f"Application ID: {kfs['application_id']}",
            f"Loan type: {kfs['loan_type'].upper()}    Loan amount sanctioned: {format_inr(kfs['loan_amount_paise'])}",
            f"Loan term: {kfs['tenure_months']} months    Instalments: {kfs['number_of_instalments']} monthly",
            f"Interest rate type: {kfs['rate_type']}    Rate of interest: {kfs['interest_rate_percent']:.2f}% p.a."
            + (f" (benchmark {kfs['benchmark']} {kfs['benchmark_rate_percent']}% + spread {kfs['spread_percent']}%)"
               if kfs.get("benchmark") not in (None, "none") else ""),
            f"EMI: {format_inr(kfs['emi_paise'])}    Annual Percentage Rate (APR): {kfs['apr_percent']:.2f}%",
            f"Total interest: {format_inr(kfs['total_interest_paise'])}    Upfront charges: {format_inr(kfs['upfront_charges_paise'])}",
            f"Net amount disbursed: {format_inr(kfs['net_disbursed_amount_paise'])}    "
            f"Total amount payable: {format_inr(kfs['total_amount_payable_paise'])}",
        ], table=(["Fee / charge", "Payable to", "Timing", "Amount"], fees_rows), widths=[60, 45, 50, 35]),
        Section("Contingent charges", [f"{c['name']}: {c['description']}" for c in kfs["contingent_charges"]]),
        Section("Part 2 - Other qualitative information", [
            f"Cooling-off / look-up period: {kfs['cooling_off']['text']} (to be verified by compliance)",
            f"Recovery agents: {kfs['recovery_agent_policy']}",
            f"Grievance redressal officer: {GRIEVANCE_OFFICER['name']}, {GRIEVANCE_OFFICER['phone']}, {GRIEVANCE_OFFICER['email']}, "
            f"{GRIEVANCE_OFFICER['address']}",
            GRIEVANCE_OFFICER["escalation"],
            f"APR method: {kfs['apr_method']}",
        ]),
        Section("Illustrative repayment schedule", [], table=(["#", "Due date", "Opening", "Principal", "Interest", "Instalment",
                                                               "Closing"], sched), widths=[10, 24, 28, 28, 26, 28, 28]),
    ]
    return make_pdf("Key Fact Statement (KFS)", sections, mock=True,
                    footer_note="Synthetic KFS for demonstration. Format and values to be verified by the bank's compliance team.")


def _ensure_kfs_document(conn: sqlite3.Connection, app: dict, kfs: dict) -> dict:
    if app["kfs_document_id"]:
        doc = one(conn, "SELECT * FROM documents WHERE id = ?", (app["kfs_document_id"],))
        if doc:
            return doc
    doc = create_document(conn, owner_type="loan_application", owner_id=app["id"], doc_type="kfs", data=_kfs_pdf(conn, kfs),
                          file_name=f"KFS-{app['id'][:8]}.pdf")
    conn.execute("UPDATE loan_applications SET kfs_document_id = ?, updated_at = ? WHERE id = ?", (doc["id"], now_iso(), app["id"]))
    app["kfs_document_id"] = doc["id"]
    return doc


KFS_STATUSES = ("underwritten", "kfs_accepted", "esigned", "submitted")


@router.get("/loans/applications/{application_id}/kfs")
def get_kfs(application_id: str, request: Request, ctx: AuthContext = Depends(require_customer),
            conn: sqlite3.Connection = Depends(get_db)):
    app = _own(conn, ctx, application_id)
    if app["status"] not in KFS_STATUSES:
        raise _out_of_order(app, "kfs")
    meta = _meta(conn, app["id"])
    kfs = _kfs(conn, app, meta)
    first = not app["kfs_document_id"]
    doc = _ensure_kfs_document(conn, app, kfs)
    kfs["kfs_document_id"] = doc["id"]
    kfs["pdf_url"] = f"/loans/applications/{app['id']}/kfs/pdf"
    if first:
        _meta_update(conn, app["id"], "kfs_generated")
        audit_ctx(conn, ctx, "financial", "loan.kfs_generated", entity_type="loan_application", entity_id=app["id"],
                  details={"kfs_document_id": doc["id"], "apr_percent": kfs["apr_percent"]}, request=request)
    return kfs


def _pdf_response(doc: dict) -> Response:
    try:
        data = get_bytes(doc["storage_key"])
    except FileNotFoundError:
        raise not_found("Document")
    return Response(content=data, media_type=doc.get("content_type") or "application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{doc.get("file_name") or "document.pdf"}"'})


@router.get("/loans/applications/{application_id}/kfs/pdf")
def kfs_pdf(application_id: str, ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    app = _own(conn, ctx, application_id)
    if app["status"] not in KFS_STATUSES:
        raise _out_of_order(app, "kfs")
    doc = _ensure_kfs_document(conn, app, _kfs(conn, app, _meta(conn, app["id"])))
    return _pdf_response(doc)


@router.post("/loans/applications/{application_id}/kfs/accept")
def accept_kfs(application_id: str, body: AcceptIn, request: Request, ctx: AuthContext = Depends(require_customer),
               conn: sqlite3.Connection = Depends(get_db)):
    app = _own(conn, ctx, application_id)
    if app["status"] != "underwritten":
        raise _out_of_order(app, "kfs_accept")
    if body.accepted is not True:
        raise invalid("accepted", "Tick the box to confirm you have read and accept the Key Fact Statement")
    if not app["kfs_document_id"]:
        raise api_error(409, "kfs_not_viewed", "Open the Key Fact Statement before accepting it.")
    now = now_iso()
    _set_status(conn, app["id"], "kfs_accepted", kfs_accepted_at=now)
    meta = _meta_update(conn, app["id"], "kfs_accepted")
    audit_ctx(conn, ctx, "financial", "loan.kfs_accepted", entity_type="loan_application", entity_id=app["id"],
              details={"kfs_document_id": app["kfs_document_id"], "apr_percent": app["apr_percent"]}, request=request)
    notify(conn, ctx.user_id, "transaction", "Key Fact Statement accepted",
           f"You accepted the Key Fact Statement for your loan (APR {app['apr_percent']}%). Next: eSign the agreement.")
    return _public(conn, _own(conn, ctx, app["id"]), meta)


# ------------------------------------------------------------------ eSign
def _agreement_pdf(app: dict, kfs: dict, customer: dict, signature: dict | None) -> bytes:
    lines = [
        f"Borrower: {customer['full_name']} (CIF {customer['finacle_cif']}, PAN {customer.get('pan_masked') or '-'})",
        f"Lender: {BRAND_NAME}",
        f"Loan amount: {format_inr(kfs['loan_amount_paise'])}; tenure {kfs['tenure_months']} months; "
        f"rate {kfs['interest_rate_percent']:.2f}% p.a. ({kfs['rate_type']}); EMI {format_inr(kfs['emi_paise'])}; "
        f"APR {kfs['apr_percent']:.2f}%",
        f"The Key Fact Statement (document {kfs['kfs_document_id']}) accepted on {fmt_ist(app['kfs_accepted_at'])} forms "
        "part of this agreement.",
        "The borrower agrees to repay the loan in equated monthly instalments through e-NACH as per the repayment schedule.",
        f"Cooling-off period: {COOLING_OFF_DAYS} days from disbursal (to be verified by compliance).",
    ]
    sections = [Section("Loan agreement", lines)]
    if signature:
        sections.append(Section("Electronic signature", [
            f"Signed by: {customer['full_name']} using {signature['signature_type']}",
            f"eSign reference: {signature['esign_ref']}   Signed at: {fmt_ist(signature['signed_at'])}",
            f"Certificate issuer: {signature['certificate_issuer']}",
            f"Signature hash: {signature['signature_hash']}",
        ]))
    return make_pdf("Loan agreement" + (" (eSigned)" if signature else ""), sections, mock=True)


@router.post("/loans/applications/{application_id}/esign")
def do_esign(application_id: str, request: Request, ctx: AuthContext = Depends(require_customer),
             conn: sqlite3.Connection = Depends(get_db)):
    app = _own(conn, ctx, application_id)
    if app["status"] != "kfs_accepted":
        raise _out_of_order(app, "esign")
    customer = customer_for(conn, str(ctx.customer_id))
    meta = _meta(conn, app["id"])
    kfs = _kfs(conn, app, meta)
    unsigned = _agreement_pdf(app, kfs, customer, None)
    provider = esign.get(conn)
    started = provider.start({"document_id": app["id"], "sha256": sha256_hex(unsigned), "signer_name": customer["full_name"]},
                             return_path=f"/portal/loans?application={app['id']}")
    signed = provider.complete(started["esign_ref"], started.get("document_sha256"))  # mock: completes immediately
    doc = create_document(conn, owner_type="loan_application", owner_id=app["id"], doc_type="esigned_agreement",
                          data=_agreement_pdf(app, kfs, customer, signed), file_name=f"Loan-agreement-{app['id'][:8]}.pdf")
    _set_status(conn, app["id"], "esigned", esign_ref=started["esign_ref"])
    meta = _meta_update(conn, app["id"], "esigned", agreement_document_id=doc["id"],
                        esign={"esign_ref": started["esign_ref"], "status": signed["status"], "signed_at": signed["signed_at"],
                               "redirect_url": started["redirect_url"], "signature_type": signed["signature_type"]})
    audit_ctx(conn, ctx, "financial", "loan.esigned", entity_type="loan_application", entity_id=app["id"],
              details={"esign_ref": started["esign_ref"], "agreement_document_id": doc["id"]}, request=request)
    notify(conn, ctx.user_id, "transaction", "Loan agreement eSigned",
           "Your loan agreement has been signed electronically. Next: set up e-NACH for your EMIs.")
    out = _public(conn, _own(conn, ctx, app["id"]), meta)
    out["esign_redirect_url"] = started["redirect_url"]
    out["mock"] = started.get("mock")
    return out


@router.get("/loans/applications/{application_id}/agreement/pdf")
def agreement_pdf(application_id: str, ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    app = _own(conn, ctx, application_id)
    doc_id = _meta(conn, app["id"]).get("agreement_document_id")
    doc = one(conn, "SELECT * FROM documents WHERE id = ? AND owner_id = ?", (doc_id, app["id"])) if doc_id else None
    if not doc:
        raise api_error(409, "invalid_step", "The signed agreement is available after you eSign it.")
    return _pdf_response(doc)


# ------------------------------------------------------------------ e-NACH
@router.post("/loans/applications/{application_id}/enach")
def setup_enach(application_id: str, body: EnachIn, request: Request, ctx: AuthContext = Depends(require_customer),
                conn: sqlite3.Connection = Depends(get_db)):
    app = _own(conn, ctx, application_id)
    if app["status"] != "esigned":
        raise _out_of_order(app, "enach")
    if body.account_id:
        acct = owned_account(conn, ctx.customer_id, body.account_id)
        if acct["account_type"] not in ("savings", "current", "nro"):
            raise invalid("account_id", "Choose a savings or current account for EMI debits")
        number, ifsc = account_number(acct), (acct["ifsc"] or "").upper()
    else:
        number = (body.account_number or "").replace(" ", "")
        ifsc = (body.ifsc or "").upper().strip()
        if not ACCOUNT_RE.match(number):
            raise invalid("account_number", "Enter the account number (6 to 20 digits)")
        if not valid_ifsc(ifsc):
            raise invalid("ifsc", "Enter a valid 11-character IFSC, for example EXMB0000001")
    if body.auth_mode not in npci_nach.AUTH_MODES:
        raise invalid("auth_mode", "Choose net banking, debit card or Aadhaar to authorise the mandate")
    emi = int(app["emi_paise"] or 0)
    max_amount = int(math.ceil(emi * 1.5 / 100_000) * 100_000) or 100_000
    start = _add_months(date.fromisoformat(app["created_at"][:10]), 1)
    end = _add_months(start, int(app["tenure_months"]) + 12)
    try:
        mandate = npci_nach.get(conn).register_mandate(
            account_number=number, ifsc=ifsc, auth_mode=body.auth_mode, max_amount_paise=max_amount, frequency="monthly",
            start_date=start.isoformat(), end_date=end.isoformat())
    except npci_nach.NachRejected as exc:
        raise api_error(422, exc.code, exc.message)
    enach = {"umrn": mandate["umrn"], "auth_mode": body.auth_mode, "account_masked": mask_account(number), "ifsc": ifsc,
             "max_amount_paise": max_amount, "start_date": start.isoformat(), "end_date": end.isoformat(),
             "registered_at": mandate["registered_at"], "status": mandate["status"]}
    _set_status(conn, app["id"], "enach_registered", enach_mandate_ref=mandate["umrn"])
    _meta_update(conn, app["id"], "enach_registered", enach=enach)
    _set_status(conn, app["id"], "submitted")
    meta = _meta_update(conn, app["id"], "submitted", submitted_at=now_iso())
    audit_ctx(conn, ctx, "financial", "loan.enach_registered", entity_type="loan_application", entity_id=app["id"],
              details={"umrn": mandate["umrn"], "auth_mode": body.auth_mode, "account": mask_account(number), "ifsc": ifsc},
              request=request)
    audit_ctx(conn, ctx, "financial", "loan.submitted", entity_type="loan_application", entity_id=app["id"], request=request)
    notify(conn, ctx.user_id, "transaction", "Loan application submitted",
           f"e-NACH mandate {mandate['umrn']} registered on account {mask_account(number)}. Your loan application is submitted "
           "for disbursal.")
    out = _public(conn, _own(conn, ctx, app["id"]), meta)
    out["mock"] = mandate.get("mock")
    return out

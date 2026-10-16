"""Spend insights, budgets, bill reminders and consent-gated personalised offers.

Insights categorise the customer's card (POS), UPI and Bharat Connect debits read from
Finacle by narration keywords. Offers are shown only while the customer's
``personalisation`` consent is active (DPDP); withdrawing it hides them immediately.
"""
from __future__ import annotations

import calendar
import re
import sqlite3
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict

from audit import audit_ctx
from banking import account_number, customer_for
from consent_service import consent_active
from core import invalid, iso, jdump, jload, new_id, now_iso, one, today_ist, utcnow
from db import get_db, register_seed
from integrations import bbps, finacle
from integrations.base import is_mock, mock_meta
from security import AuthContext, require_customer

router = APIRouter(tags=["insights"])

CATEGORIES = ("food", "travel", "shopping", "bills", "fuel", "entertainment", "health", "other")
CATEGORY_LABELS = {
    "food": ("Food & groceries", "भोजन और किराना"), "travel": ("Travel", "यात्रा"), "shopping": ("Shopping", "खरीदारी"),
    "bills": ("Bills & utilities", "बिल और यूटिलिटी"), "fuel": ("Fuel", "ईंधन"), "entertainment": ("Entertainment", "मनोरंजन"),
    "health": ("Health", "स्वास्थ्य"), "other": ("Other", "अन्य"),
}
KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("food", ("SWIGGY", "ZOMATO", "BIGBASKET")),
    ("travel", ("UBER", "IRCTC")),
    ("shopping", ("AMAZON", "MYNTRA")),
    ("bills", ("ELECTRICITY", "AIRTEL", "BBPS")),
    ("fuel", ("FUEL", "INDIANOIL")),
    ("entertainment", ("BOOKMYSHOW",)),
    ("health", ("PHARMACY", "APOLLO")),
]
SPEND_PREFIXES = ("UPI/", "POS ", "BBPS/")
SPEND_ACCOUNT_TYPES = ("savings", "current", "nre", "nro")
MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


class Body(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class BudgetIn(Body):
    category: Optional[str] = None
    monthly_limit_paise: Optional[int] = None


class ReminderIn(Body):
    biller_id: Optional[str] = None
    due_day: Optional[int] = None
    amount_paise: Optional[int] = None


# ------------------------------------------------------------------ helpers
def categorise(narration: str) -> str:
    text = (narration or "").upper()
    for cat, words in KEYWORDS:
        if any(w in text for w in words):
            return cat
    return "other"


def is_spend(narration: str) -> bool:
    return (narration or "").upper().startswith(SPEND_PREFIXES)


def _month_bounds(month: str) -> tuple[date, date]:
    y, m = int(month[:4]), int(month[5:7])
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def month_spend(conn: sqlite3.Connection, customer_id: str, month: str) -> dict:
    """{category: {amount_paise, count}} for UPI/POS/BBPS debits in ``month`` across the customer's accounts."""
    start, end = _month_bounds(month)
    core = finacle.get(conn)
    totals = {c: {"amount_paise": 0, "count": 0} for c in CATEGORIES}
    accounts = [dict(r) for r in conn.execute(
        "SELECT * FROM accounts WHERE customer_id = ? AND owner_type = 'customer' AND currency = 'INR'", (customer_id,))]
    for acct in accounts:
        if acct["account_type"] not in SPEND_ACCOUNT_TYPES:
            continue
        txns = core.transactions(account_number(acct), from_date=start, to_date=end, limit=2000, direction="DR")
        for t in txns["items"]:
            if t["dr_cr"] != "DR" or not is_spend(t["narration"]):
                continue
            cat = categorise(t["narration"])
            totals[cat]["amount_paise"] += t["amount_paise"]
            totals[cat]["count"] += 1
    return totals


def _budgets(conn: sqlite3.Connection, customer_id: str) -> dict[str, dict]:
    return {r["category"]: dict(r) for r in conn.execute("SELECT * FROM budgets WHERE customer_id = ?", (customer_id,))}


def _current_month() -> str:
    return today_ist().strftime("%Y-%m")


def next_due_date(due_day: int, today: date | None = None) -> date:
    today = today or today_ist()

    def clamp(y: int, m: int) -> date:
        return date(y, m, min(due_day, calendar.monthrange(y, m)[1]))

    d = clamp(today.year, today.month)
    if d < today:
        y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        d = clamp(y, m)
    return d


def _public_reminder(r: dict) -> dict:
    biller = bbps.find_biller(r["biller_id"]) or {}
    due = next_due_date(r["due_day"])
    return {
        "id": r["id"], "biller_id": r["biller_id"], "biller_name": r["biller_name"] or biller.get("name"),
        "category": biller.get("category"), "due_day": r["due_day"], "amount_paise": r["amount_paise"],
        "next_due_date": due.isoformat(), "days_until_due": (due - today_ist()).days, "created_at": r["created_at"],
    }


def _age(customer: dict) -> int | None:
    from routers.deposits import customer_age

    return customer_age(customer)


def rules_match(rules: dict, customer: dict, age: int | None) -> bool:
    segs = rules.get("segments")
    if segs and customer.get("segment") not in segs:
        return False
    res = rules.get("residency")
    if res and customer.get("residency_status") not in res:
        return False
    if rules.get("min_age") is not None and (age is None or age < int(rules["min_age"])):
        return False
    if rules.get("max_age") is not None and (age is None or age > int(rules["max_age"])):
        return False
    return True


def _reason(rules: dict) -> str:
    parts = []
    if rules.get("segments"):
        parts.append(" / ".join(s.upper() if s in ("hni", "uhni") else s.capitalize() for s in rules["segments"]) + " customers")
    if rules.get("residency"):
        parts.append("NRI customers" if "nri" in rules["residency"] else "customers by residency")
    if rules.get("min_age") is not None:
        parts.append(f"customers aged {rules['min_age']}+")
    return "Selected for " + ", ".join(parts) if parts else "Available to all customers"


# ------------------------------------------------------------------ insights
@router.get("/me/insights")
def insights(month: Optional[str] = Query(None), ctx: AuthContext = Depends(require_customer),
             conn: sqlite3.Connection = Depends(get_db)):
    month = month or _current_month()
    if not MONTH_RE.match(month):
        raise invalid("month", "Use the month format YYYY-MM, for example 2026-10")
    start, end = _month_bounds(month)
    totals = month_spend(conn, ctx.customer_id, month)  # type: ignore[arg-type]
    budgets = _budgets(conn, ctx.customer_id)  # type: ignore[arg-type]
    total = sum(v["amount_paise"] for v in totals.values())
    cats = []
    for c in CATEGORIES:
        amt = totals[c]["amount_paise"]
        limit = budgets.get(c, {}).get("monthly_limit_paise")
        cats.append({
            "category": c, "label": CATEGORY_LABELS[c][0], "label_hi": CATEGORY_LABELS[c][1],
            "amount_paise": amt, "count": totals[c]["count"],
            "share_percent": round(amt * 100.0 / total, 1) if total else 0.0,
            "budget_paise": limit,
            "budget_used_percent": round(amt * 100.0 / limit, 1) if limit else None,
            "over_budget": bool(limit and amt > limit),
        })
    cats.sort(key=lambda x: x["amount_paise"], reverse=True)
    mock = is_mock(conn, "finacle")
    return {
        "month": month,
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "currency": "INR",
        "total_spend_paise": total,
        "transaction_count": sum(v["count"] for v in totals.values()),
        "categories": cats,
        "budgets_exceeded": [c["category"] for c in cats if c["over_budget"]],
        "method": "Card (POS), UPI and Bharat Connect debits categorised by merchant keywords in the narration",
        "as_of": now_iso(),
        "source": "finacle",
        "mock": mock_meta("finacle") if mock else None,
    }


# ------------------------------------------------------------------ budgets
@router.get("/me/budgets")
def list_budgets(ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    month = _current_month()
    spend = month_spend(conn, ctx.customer_id, month)  # type: ignore[arg-type]
    items = []
    for c, b in sorted(_budgets(conn, ctx.customer_id).items(), key=lambda kv: CATEGORIES.index(kv[0])):  # type: ignore[arg-type]
        spent = spend[c]["amount_paise"]
        items.append({"id": b["id"], "category": c, "label": CATEGORY_LABELS[c][0], "label_hi": CATEGORY_LABELS[c][1],
                      "monthly_limit_paise": b["monthly_limit_paise"], "spent_paise": spent,
                      "used_percent": round(spent * 100.0 / b["monthly_limit_paise"], 1),
                      "over_budget": spent > b["monthly_limit_paise"], "updated_at": b["updated_at"]})
    return {"month": month, "items": items, "categories": list(CATEGORIES)}


@router.post("/me/budgets", status_code=201)
def upsert_budget(body: BudgetIn, request: Request, ctx: AuthContext = Depends(require_customer),
                  conn: sqlite3.Connection = Depends(get_db)):
    if body.category not in CATEGORIES:
        raise invalid("category", f"Choose a category: {', '.join(CATEGORIES)}")
    limit = body.monthly_limit_paise
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise invalid("monthly_limit_paise", "Enter a monthly limit greater than zero")
    if limit > 10**10:
        raise invalid("monthly_limit_paise", "Enter a monthly limit up to Rs. 1 crore")
    now = now_iso()
    existing = one(conn, "SELECT id FROM budgets WHERE customer_id = ? AND category = ?", (ctx.customer_id, body.category))
    bid = existing["id"] if existing else new_id()
    conn.execute(
        """INSERT INTO budgets (id, customer_id, category, monthly_limit_paise, updated_at) VALUES (?,?,?,?,?)
           ON CONFLICT (customer_id, category) DO UPDATE SET monthly_limit_paise = excluded.monthly_limit_paise,
           updated_at = excluded.updated_at""",
        (bid, ctx.customer_id, body.category, limit, now),
    )
    audit_ctx(conn, ctx, "admin", "budget.saved", entity_type="budget", entity_id=bid, request=request,
              details={"category": body.category, "monthly_limit_paise": limit})
    return {"id": bid, "category": body.category, "label": CATEGORY_LABELS[body.category][0],
            "label_hi": CATEGORY_LABELS[body.category][1], "monthly_limit_paise": limit, "updated_at": now}


# ------------------------------------------------------------------ bill reminders
@router.get("/me/bill-reminders")
def list_reminders(ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    rows = [_public_reminder(dict(r)) for r in conn.execute(
        "SELECT * FROM bill_reminders WHERE customer_id = ? ORDER BY created_at", (ctx.customer_id,))]
    rows.sort(key=lambda r: r["next_due_date"])
    return {"items": rows}


@router.post("/me/bill-reminders", status_code=201)
def create_reminder(body: ReminderIn, request: Request, ctx: AuthContext = Depends(require_customer),
                    conn: sqlite3.Connection = Depends(get_db)):
    biller = bbps.find_biller(body.biller_id)
    if not biller:
        raise invalid("biller_id", "Choose a biller from the Bharat Connect list", code="unknown_biller")
    if not isinstance(body.due_day, int) or isinstance(body.due_day, bool) or not (1 <= body.due_day <= 31):
        raise invalid("due_day", "Choose a due day between 1 and 31")
    if body.amount_paise is not None and (not isinstance(body.amount_paise, int) or body.amount_paise <= 0):
        raise invalid("amount_paise", "Enter an amount greater than zero, or leave it empty")
    row = {"id": new_id(), "customer_id": ctx.customer_id, "biller_id": biller["biller_id"], "biller_name": biller["name"],
           "due_day": body.due_day, "amount_paise": body.amount_paise, "created_at": now_iso()}
    conn.execute(
        """INSERT INTO bill_reminders (id, customer_id, biller_id, biller_name, due_day, amount_paise, created_at)
           VALUES (:id, :customer_id, :biller_id, :biller_name, :due_day, :amount_paise, :created_at)""",
        row,
    )
    audit_ctx(conn, ctx, "admin", "bill_reminder.created", entity_type="bill_reminder", entity_id=row["id"], request=request,
              details={"biller_id": biller["biller_id"], "due_day": body.due_day})
    return _public_reminder(row)


# ------------------------------------------------------------------ offers
@router.get("/me/offers")
def offers(locale: str = Query("en"), ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    if not consent_active(conn, ctx.user_id, "personalisation"):
        return {"consent_active": False, "items": [],
                "message": "Personalised offers are off. Turn on 'Personalisation' in Privacy settings to see them."}
    cust = customer_for(conn, ctx.customer_id)  # type: ignore[arg-type]
    age = _age(cust)
    today = today_ist().isoformat()
    rows = [dict(r) for r in conn.execute(
        """SELECT o.id, o.segment_rules, o.valid_from, o.valid_to, i.slug, v.body, v.published_at
           FROM offers o
           JOIN content_items i ON i.id = o.content_item_id AND i.content_type = 'offer'
           JOIN content_versions v ON v.id = i.published_version_id AND v.status = 'published'
           WHERE o.valid_from <= ? AND o.valid_to >= ? ORDER BY o.valid_to""",
        (today, today),
    )]
    hi = locale == "hi"
    items = []
    for r in rows:
        rules = jload(r["segment_rules"], {}) or {}
        if not rules_match(rules, cust, age):
            continue
        body = jload(r["body"], {}) or {}
        items.append({
            "id": r["id"],
            "slug": r["slug"],
            "title": (hi and body.get("title_hi")) or body.get("title") or r["slug"],
            "summary": (hi and body.get("summary_hi")) or body.get("summary") or "",
            "cta_label": (hi and body.get("cta_label_hi")) or body.get("cta_label") or "Know more",
            "cta_href": body.get("cta_href"),
            "terms": body.get("terms"),
            "tag": body.get("tag"),
            "valid_from": r["valid_from"],
            "valid_to": r["valid_to"],
            "reason": _reason(rules),
        })
    return {"consent_active": True, "items": items}


# ------------------------------------------------------------------ synthetic seed (slice D2)
SEED_OFFERS = [
    ("offer-portal-wealth-review", {"segments": ["affluent", "hni", "uhni", "private"]}, {
        "title": "Complimentary portfolio review with your RM", "title_hi": "आपके RM के साथ निःशुल्क पोर्टफ़ोलियो समीक्षा",
        "summary": "Book a 30-minute review of your deposits and investments with your relationship manager.",
        "summary_hi": "अपने रिलेशनशिप मैनेजर के साथ जमा और निवेश की 30 मिनट की समीक्षा बुक करें।",
        "cta_label": "Book a review", "cta_label_hi": "समीक्षा बुक करें", "cta_href": "/portal/wealth", "tag": "Wealth",
        "terms": "Synthetic offer for demonstration. No charges apply."}),
    ("offer-portal-nri-remit", {"residency": ["nri", "pio_oci"]}, {
        "title": "Zero transfer fee to your NRE account", "title_hi": "आपके NRE खाते में शून्य ट्रांसफ़र शुल्क",
        "summary": "Inward remittances to your NRE account carry no bank fee this quarter.",
        "summary_hi": "इस तिमाही आपके NRE खाते में आने वाले रेमिटेंस पर कोई बैंक शुल्क नहीं।",
        "cta_label": "View NRI services", "cta_label_hi": "एनआरआई सेवाएँ देखें", "cta_href": "/portal/nri", "tag": "NRI",
        "terms": "Synthetic offer for demonstration. Correspondent bank charges may apply."}),
    ("offer-portal-senior-fd", {"min_age": 60}, {
        "title": "Senior citizen FD: higher rate, doorstep service", "title_hi": "वरिष्ठ नागरिक FD: ऊँची दर, घर पर सेवा",
        "summary": "Senior citizens get the published senior rate plus doorstep banking for deposit paperwork.",
        "summary_hi": "वरिष्ठ नागरिकों को प्रकाशित वरिष्ठ दर और जमा कागज़ात के लिए घर पर बैंकिंग।",
        "cta_label": "Open an FD", "cta_label_hi": "FD खोलें", "cta_href": "/portal/deposits", "tag": "Deposits",
        "terms": "Synthetic offer for demonstration. Rates as per the live published rate table."}),
    ("offer-portal-bbps-cashback", {}, {
        "title": "Pay bills on Bharat Connect, never miss a due date", "title_hi": "भारत कनेक्ट पर बिल भरें, कोई ड्यू डेट न चूकें",
        "summary": "Set bill reminders and pay electricity, mobile and broadband bills in a few taps.",
        "summary_hi": "बिल रिमाइंडर सेट करें और बिजली, मोबाइल और ब्रॉडबैंड बिल कुछ टैप में भरें।",
        "cta_label": "Pay a bill", "cta_label_hi": "बिल भरें", "cta_href": "/portal/payments", "tag": "Bills",
        "terms": "Synthetic offer for demonstration."}),
]


def seed_d2(conn: sqlite3.Connection) -> None:
    from cms_seed import seed_published
    from rates_service import fd_rate
    from routers.deposits import book_fixed_deposit, is_senior
    from routers.payments import issue_receipt

    today = today_ist()
    for slug, rules, body in SEED_OFFERS:
        if one(conn, "SELECT id FROM content_items WHERE slug = ? AND locale = 'en'", (slug,)):
            continue
        item = seed_published(conn, content_type="offer", template="generic", slug=slug, locale="en", body=body)
        conn.execute("INSERT INTO offers (id, content_item_id, segment_rules, valid_from, valid_to) VALUES (?,?,?,?,?)",
                     (new_id(), item["item_id"], jdump(rules), (today - timedelta(days=30)).isoformat(),
                      (today + timedelta(days=90)).isoformat()))

    user = one(conn, "SELECT * FROM users WHERE email = 'customer@example.bank.in'")
    if not user or not user.get("customer_id"):
        return
    cust = customer_for(conn, user["customer_id"])
    cid = cust["id"]
    now = now_iso()
    for cat, limit in (("food", 1500000), ("shopping", 1000000), ("travel", 800000), ("bills", 600000)):
        conn.execute("INSERT OR IGNORE INTO budgets (id, customer_id, category, monthly_limit_paise, updated_at) VALUES (?,?,?,?,?)",
                     (new_id(), cid, cat, limit, now))
    for biller_id, due_day, amount in (("MSEDCL000MAH01", 10, None), ("AIRTELPOST0IN1", 22, 59900)):
        b = bbps.BILLERS[biller_id]
        conn.execute("INSERT INTO bill_reminders (id, customer_id, biller_id, biller_name, due_day, amount_paise, created_at) "
                     "VALUES (?,?,?,?,?,?,?)", (new_id(), cid, biller_id, b["name"], due_day, amount, now))

    acct = one(conn, "SELECT * FROM accounts WHERE customer_id = ? AND masked_number = 'XXXX4821'", (cid,))
    if not acct:
        return
    core = finacle.MockFinacle(conn)
    acct_no = account_number(acct)
    for days_ago, biller_id, consumer in ((40, "MSEDCL000MAH01", "000123456789"), (12, "AIRTELPOST0IN1", "9876543210")):
        b = bbps.BILLERS[biller_id]
        bill = bbps.MockBbps().fetch_bill(biller_id, consumer)
        pid = new_id()
        when = utcnow() - timedelta(days=days_ago)
        posting = core.post(acct_no, "DR", bill["bill_amount_paise"], f"BBPS/{b['name'][:40]}/{consumer}",
                            idempotency_ref=f"BIL-{pid}", channel_ref=pid, posted_at=when)
        row = {
            "id": pid, "customer_id": cid, "payment_kind": "bbps_bill", "biller_id": biller_id, "biller_name": b["name"],
            "consumer_number": consumer, "cpin": None, "pan": None, "assessment_year": None, "tax_type": None,
            "amount_paise": bill["bill_amount_paise"], "from_account_id": acct["id"], "idempotency_key": f"seed-bill-{pid}",
            "request_hash": "seed", "rail_reference": "BBPS" + when.strftime("%y%m%d") + pid.replace("-", "")[:10].upper(),
            "finacle_posting_ref": posting["posting_ref"], "receipt_document_id": None, "status": "success",
            "failure_reason": None, "created_at": iso(when), "updated_at": iso(when),
        }
        doc = issue_receipt(conn, row, acct["masked_number"], mock=True)
        row["receipt_document_id"] = doc["id"]
        conn.execute(
            """INSERT INTO bill_payments (id, customer_id, payment_kind, biller_id, biller_name, consumer_number, cpin, pan,
               assessment_year, tax_type, amount_paise, from_account_id, idempotency_key, request_hash, rail_reference,
               finacle_posting_ref, receipt_document_id, status, failure_reason, created_at, updated_at) VALUES (:id, :customer_id,
               :payment_kind, :biller_id, :biller_name, :consumer_number, :cpin, :pan, :assessment_year, :tax_type, :amount_paise,
               :from_account_id, :idempotency_key, :request_hash, :rail_reference, :finacle_posting_ref, :receipt_document_id,
               :status, :failure_reason, :created_at, :updated_at)""",
            row,
        )

    senior = is_senior(conn, cust)
    principal, tenure = 20000000, 365
    rate = fd_rate(conn, tenure, senior, principal) or {"rate_percent": 7.1, "rate_table_slug": "fallback (no published rate at seed time)",
                                                       "as_on_date": today.isoformat()}
    book_fixed_deposit(conn, customer=cust, acct=acct, principal=principal, tenure_days=tenure, payout="cumulative",
                       maturity_instruction="credit_to_account", deposit_ref="FDSEEDDEMO0001", rate=rate, senior=senior)


register_seed(70, "d2_payments_deposits_insights", seed_d2)

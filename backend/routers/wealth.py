"""Wealth (US-30): mutual fund portfolio with XIRR, orders (purchase / redeem / SIP) and RM connect.

Holdings come from the mutual fund RTA adapter (mock register seeded per customer).
XIRR is solved from the dated cash flows (purchases negative, redemptions positive,
current value positive today) with Newton's method and a bisection fallback.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from audit import audit_ctx
from banking import customer_for
from core import IST, api_error, format_inr, invalid, many, new_id, now_iso, one, parse_iso, today_ist, utcnow
from db import get_db, register_seed
from integrations import mf_rta
from integrations.base import is_mock, mock_meta
from notify import notify
from security import AuthContext, consume_mfa_token, require_customer

router = APIRouter(tags=["wealth"])

ORDER_TYPES = ("purchase", "redeem", "sip")
CATEGORIES = ("equity", "debt", "hybrid", "gold")
MAX_ORDER_PAISE = 1_000_000_000  # Rs. 1 crore per online order (bank policy assumption)
RM_HOURS = {"days": "Monday to Saturday (except bank holidays)", "from": "10:00", "to": "18:00", "timezone": "IST"}
CHANNELS = ("call", "whatsapp", "video")


class Body(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class OrderIn(Body):
    scheme_code: Optional[str] = None
    order_type: Optional[str] = None
    amount_paise: Any = None
    units: Any = None
    folio: Optional[str] = None
    sip_day: Any = None
    mfa_token: Optional[str] = None


class ContactIn(Body):
    channel: Optional[str] = None
    preferred_time: Optional[str] = None
    message: Optional[str] = None


# ------------------------------------------------------------------ XIRR
def xirr(cashflows: list[tuple[date, float]]) -> float | None:
    """Annualised XIRR in percent, or None when it cannot be determined."""
    flows = [(d, a) for d, a in cashflows if a]
    if not flows or not any(a > 0 for _, a in flows) or not any(a < 0 for _, a in flows):
        return None
    d0 = min(d for d, _ in flows)
    ts = [((d - d0).days / 365.0, a) for d, a in flows]

    def f(r: float) -> float:
        return sum(a / (1 + r) ** t for t, a in ts)

    def df(r: float) -> float:
        return sum(-t * a / (1 + r) ** (t + 1) for t, a in ts)

    r = 0.1
    for _ in range(100):
        try:
            fv, dv = f(r), df(r)
        except (OverflowError, ZeroDivisionError):
            break
        if dv == 0:
            break
        nr = r - fv / dv
        if nr <= -0.9999 or nr != nr:
            break
        if abs(nr - r) < 1e-10:
            return round(nr * 100, 2)
        r = nr
    lo, hi = -0.9999, 100.0
    try:
        flo, fhi = f(lo), f(hi)
    except OverflowError:
        return None
    if flo * fhi > 0:
        return None
    for _ in range(300):
        mid = (lo + hi) / 2
        fm = f(mid)
        if abs(fm) < 1e-7:
            break
        if flo * fm < 0:
            hi = mid
        else:
            lo, flo = mid, fm
    return round((lo + hi) / 2 * 100, 2)


# ------------------------------------------------------------------ portfolio
def _portfolio(conn: sqlite3.Connection, customer_id: str) -> dict:
    rta = mf_rta.get(conn)
    schemes = {s["scheme_code"]: s for s in rta.schemes()}
    today = today_ist()
    holdings, all_flows = [], []
    total_value = total_cost = 0
    alloc = {c: 0 for c in CATEGORIES}
    for h in rta.holdings(customer_id):
        s = schemes.get(h["scheme_code"])
        if not s:
            continue
        value = int(round(h["units"] * s["nav"] * 100))
        flows = [(date.fromisoformat(c["date"]), float(c["amount_paise"])) for c in h["cashflows"]]
        all_flows.extend(flows)
        cost = h["cost_paise"]
        holdings.append({
            "folio": h["folio"],
            "scheme_code": h["scheme_code"],
            "scheme_name": s["scheme_name"],
            "category": s["category"],
            "sub_category": s["sub_category"],
            "riskometer": s["riskometer"],
            "units": h["units"],
            "avg_cost_nav": round(cost / 100 / h["units"], 4) if h["units"] else None,
            "nav": s["nav"],
            "nav_date": s["nav_date"],
            "invested_paise": cost,
            "current_value_paise": value,
            "gain_paise": value - cost,
            "gain_percent": round((value - cost) / cost * 100, 2) if cost else None,
            "xirr_percent": xirr(flows + [(today, float(value))]),
            "first_investment_date": min(c["date"] for c in h["cashflows"]),
        })
        total_value += value
        total_cost += cost
        alloc[s["category"]] = alloc.get(s["category"], 0) + value
    holdings.sort(key=lambda x: -x["current_value_paise"])
    allocation = [{"category": c, "value_paise": alloc[c],
                   "percent": round(alloc[c] / total_value * 100, 2) if total_value else 0.0} for c in CATEGORIES]
    sips = [{"scheme_code": s["scheme_code"], "scheme_name": schemes.get(s["scheme_code"], {}).get("scheme_name"),
             "amount_paise": s["amount_paise"], "sip_day": s["sip_day"], "status": s["status"], "folio": s["folio"],
             "start_date": s["start_date"], "rta_ref": s["rta_ref"]} for s in rta.sips(customer_id)] if hasattr(rta, "sips") else []
    return {
        "as_of": today.isoformat(),
        "current_value_paise": total_value,
        "total_invested_paise": total_cost,
        "total_gain_paise": total_value - total_cost,
        "total_gain_percent": round((total_value - total_cost) / total_cost * 100, 2) if total_cost else None,
        "overall_xirr_percent": xirr(all_flows + [(today, float(total_value))]) if total_value else None,
        "allocation": allocation,
        "holdings": holdings,
        "sips": sips,
        "default_folio": mf_rta.default_folio(customer_id),
        "mock": mock_meta("mf_rta") if is_mock(conn, "mf_rta") else None,
        "disclaimer": "Mutual fund investments are subject to market risks. Read all scheme-related documents carefully. "
                      "Synthetic schemes and NAVs for demonstration only.",
    }


@router.get("/me/wealth/portfolio")
def portfolio(ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    return _portfolio(conn, str(ctx.customer_id))


@router.get("/me/wealth/schemes")
def schemes(ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    return {"items": mf_rta.get(conn).schemes(), "mock": mock_meta("mf_rta") if is_mock(conn, "mf_rta") else None}


# ------------------------------------------------------------------ orders
def _order_public(o: dict) -> dict:
    return {k: o.get(k) for k in ("id", "scheme_code", "scheme_name", "order_type", "amount_paise", "units", "folio", "rta_ref",
                                  "status", "created_at")}


@router.get("/me/wealth/orders")
def list_orders(ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    rows = many(conn, "SELECT * FROM mutual_fund_orders WHERE customer_id = ? ORDER BY created_at DESC LIMIT 100", (ctx.customer_id,))
    return {"items": [_order_public(r) for r in rows]}


def _positive_int(value: Any, field: str, message: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float) and value.is_integer():
            return int(value)
        raise invalid(field, message)
    return value


@router.post("/me/wealth/orders", status_code=201)
def place_order(body: OrderIn, request: Request, ctx: AuthContext = Depends(require_customer),
                conn: sqlite3.Connection = Depends(get_db)):
    customer_id = str(ctx.customer_id)
    rta = mf_rta.get(conn)
    order_type = (body.order_type or "").lower()
    if order_type not in ORDER_TYPES:
        raise invalid("order_type", "Choose purchase, redeem or SIP")
    code = (body.scheme_code or "").upper()
    scheme = rta.scheme(code) if code else None
    if not scheme:
        raise invalid("scheme_code", "Choose a scheme from the list")
    amount: int | None = None
    units: float | None = None
    sip_day: int | None = None
    folio = (body.folio or "").strip() or None
    if order_type in ("purchase", "sip"):
        amount = _positive_int(body.amount_paise, "amount_paise", "Enter the amount")
        if amount < mf_rta.MIN_PURCHASE_PAISE:
            raise invalid("amount_paise", f"The minimum amount is {format_inr(mf_rta.MIN_PURCHASE_PAISE)}")
        if amount > MAX_ORDER_PAISE:
            raise invalid("amount_paise", f"The maximum online order is {format_inr(MAX_ORDER_PAISE)}")
        if order_type == "sip" and body.sip_day not in (None, ""):
            sip_day = _positive_int(body.sip_day, "sip_day", "Choose a SIP date between 1 and 28")
            if not 1 <= sip_day <= 28:
                raise invalid("sip_day", "Choose a SIP date between 1 and 28")
        if folio and folio != mf_rta.default_folio(customer_id) and not any(
                h["folio"] == folio for h in rta.holdings(customer_id)):
            raise invalid("folio", "This folio does not belong to you")
    else:
        try:
            units = round(float(body.units), 3)
        except (TypeError, ValueError):
            raise invalid("units", "Enter the number of units to redeem")
        if units <= 0:
            raise invalid("units", "Enter units greater than zero")
        mine = [h for h in rta.holdings(customer_id) if h["scheme_code"] == code and (not folio or h["folio"] == folio)]
        if not mine:
            raise invalid("scheme_code", "You do not hold units of this scheme" + (" in this folio" if folio else ""))
        held = mine[0]
        folio = held["folio"]
        if units - held["units"] > 1e-6:
            raise invalid("units", f"You can redeem at most {held['units']:.3f} units from folio {folio}", code="insufficient_units")
    consume_mfa_token(conn, ctx, body.mfa_token, "mf_order")
    try:
        res = rta.place_order(investor_ref=customer_id, scheme_code=code, order_type=order_type, amount_paise=amount,
                              units=units, folio=folio, sip_day=sip_day)
    except mf_rta.RtaRejected as exc:
        raise api_error(422, exc.code, exc.message)
    order = {
        "id": new_id(), "customer_id": customer_id, "scheme_code": code, "scheme_name": scheme["scheme_name"],
        "order_type": order_type, "amount_paise": res.get("amount_paise"), "units": res.get("units"), "folio": res["folio"],
        "rta_ref": res["rta_ref"], "status": "placed", "created_at": now_iso(),
    }
    conn.execute(
        """INSERT INTO mutual_fund_orders (id, customer_id, scheme_code, scheme_name, order_type, amount_paise, units, folio, rta_ref,
           status, created_at) VALUES (:id, :customer_id, :scheme_code, :scheme_name, :order_type, :amount_paise, :units, :folio,
           :rta_ref, :status, :created_at)""",
        order,
    )
    audit_ctx(conn, ctx, "financial", f"wealth.order_{order_type}", entity_type="mutual_fund_order", entity_id=order["id"],
              details={"scheme_code": code, "amount_paise": order["amount_paise"], "units": order["units"], "folio": order["folio"],
                       "rta_ref": order["rta_ref"]}, request=request)
    what = {"purchase": f"Purchase of {format_inr(amount)}", "sip": f"SIP of {format_inr(amount)} a month",
            "redeem": f"Redemption of {units:.3f} units" if units is not None else "Redemption"}[order_type]
    notify(conn, ctx.user_id, "transaction", "Mutual fund order placed",
           f"{what} in {scheme['scheme_name']} placed (ref {order['rta_ref']}). Not you? Report fraud immediately.")
    out = _order_public(order)
    out["nav"] = res.get("nav")
    out["sip_day"] = res.get("sip_day")
    out["mock"] = res.get("mock")
    return out


# ------------------------------------------------------------------ relationship manager
def _rm(conn: sqlite3.Connection, customer_id: str) -> dict:
    cust = customer_for(conn, customer_id)
    rm = one(conn, "SELECT * FROM users WHERE id = ? AND user_type = 'staff' AND status = 'active'", (cust.get("rm_staff_id"),)) \
        if cust.get("rm_staff_id") else None
    if not rm:
        raise api_error(404, "no_rm", "No relationship manager is assigned to you yet. Call 1800-000-0000 and we will help you.")
    return rm


def _available_now() -> bool:
    now = utcnow().astimezone(IST)
    return now.weekday() < 6 and 10 <= now.hour < 18


@router.get("/me/rm")
def my_rm(ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    rm = _rm(conn, str(ctx.customer_id))
    digits = "".join(ch for ch in (rm["mobile"] or "") if ch.isdigit())
    recent = many(conn, """SELECT id, channel, preferred_time, message, status, created_at FROM rm_contact_requests
                           WHERE customer_id = ? ORDER BY created_at DESC LIMIT 5""", (ctx.customer_id,))
    return {
        "id": rm["id"],
        "name": rm["display_name"],
        "designation": "Relationship Manager",
        "phone": rm["mobile"],
        "whatsapp": rm["mobile"],
        "whatsapp_url": f"https://wa.me/{digits}" if digits else None,
        "tel_url": f"tel:{rm['mobile']}" if rm["mobile"] else None,
        "email": rm["email"],
        "availability": RM_HOURS,
        "availability_text": f"{RM_HOURS['days']}, {RM_HOURS['from']} to {RM_HOURS['to']} {RM_HOURS['timezone']}",
        "available_now": _available_now(),
        "synthetic_contact": True,
        "recent_requests": recent,
    }


@router.post("/me/rm/contact-requests", status_code=201)
def contact_rm(body: ContactIn, request: Request, ctx: AuthContext = Depends(require_customer),
               conn: sqlite3.Connection = Depends(get_db)):
    if body.channel not in CHANNELS:
        raise invalid("channel", "Choose a call, WhatsApp or video call")
    preferred = None
    if body.preferred_time:
        dt = parse_iso(body.preferred_time)
        if not dt:
            raise invalid("preferred_time", "Enter a valid date and time")
        if dt <= utcnow():
            raise invalid("preferred_time", "Choose a time in the future")
        if dt > utcnow() + timedelta(days=60):
            raise invalid("preferred_time", "Choose a time within the next 60 days")
        preferred = dt.isoformat(timespec="seconds").replace("+00:00", "Z")
    message = (body.message or "").strip() or None
    if message and len(message) > 500:
        raise invalid("message", "Keep the message within 500 characters")
    rm = _rm(conn, str(ctx.customer_id))
    req = {"id": new_id(), "customer_id": ctx.customer_id, "rm_staff_id": rm["id"], "channel": body.channel,
           "preferred_time": preferred, "message": message, "status": "requested", "created_at": now_iso()}
    conn.execute(
        """INSERT INTO rm_contact_requests (id, customer_id, rm_staff_id, channel, preferred_time, message, status, created_at)
           VALUES (:id, :customer_id, :rm_staff_id, :channel, :preferred_time, :message, :status, :created_at)""",
        req,
    )
    audit_ctx(conn, ctx, "admin", "wealth.rm_contact_requested", entity_type="rm_contact_request", entity_id=req["id"],
              details={"channel": body.channel, "preferred_time": preferred}, request=request)
    label = {"call": "a call", "whatsapp": "a WhatsApp chat", "video": "a video call"}[body.channel]
    notify(conn, ctx.user_id, "transaction", "Request sent to your relationship manager",
           f"{rm['display_name']} will contact you for {label}" + (" at your preferred time." if preferred else " shortly."))
    notify(conn, rm["id"], "transaction", "Customer contact request",
           f"A customer requested {label}" + (f" at {preferred}" if preferred else "") + ".")
    return {**{k: req[k] for k in ("id", "channel", "preferred_time", "message", "status", "created_at")},
            "rm_name": rm["display_name"]}


# ------------------------------------------------------------------ seed
_PLANS = {
    "customer@example.bank.in": {
        "lumps": [("EXF001", 900, 100_000), ("EXF001", 400, 50_000), ("EXF003", 700, 75_000), ("EXF006", 500, 150_000),
                  ("EXF009", 300, 60_000), ("EXF010", 200, 40_000)],
        "sip": ("EXF002", 10_000, 18),
        "redeem": ("EXF001", 100, 10.0),
    },
    "nri@example.bank.in": {
        "lumps": [("EXF005", 800, 500_000), ("EXF007", 120, 300_000), ("EXF008", 600, 200_000), ("EXF004", 730, 150_000)],
        "sip": ("EXF003", 25_000, 12),
        "redeem": None,
    },
}


def seed_wealth(conn: sqlite3.Connection) -> None:
    rta = mf_rta.MockMfRta(conn)
    today = today_ist()
    for email, plan in _PLANS.items():
        u = one(conn, "SELECT customer_id FROM users WHERE email = ?", (email,))
        if not u or not u["customer_id"]:
            continue
        investor = u["customer_id"]
        folio = mf_rta.default_folio(investor)
        for code, days_ago, rupees in plan["lumps"]:
            rta.record_transaction(investor_ref=investor, folio=folio, scheme_code=code, txn_type="purchase",
                                   txn_date=today - timedelta(days=days_ago), amount_paise=rupees * 100)
        code, rupees, count = plan["sip"]
        for k in range(count, 0, -1):
            rta.record_transaction(investor_ref=investor, folio=folio, scheme_code=code, txn_type="sip_instalment",
                                   txn_date=today - timedelta(days=30 * k), amount_paise=rupees * 100)
        conn.execute(
            """INSERT INTO mock_mf_rta_sips (id, investor_ref, folio, scheme_code, amount_paise, sip_day, start_date, status, rta_ref,
               created_at) VALUES (?,?,?,?,?,?,?,'registered',?,?)""",
            (new_id(), investor, folio, code, rupees * 100, 5, (today - timedelta(days=30 * count)).isoformat(),
             "MOCK-SIP-" + new_id().replace("-", "")[:10].upper(), now_iso()),
        )
        if plan["redeem"]:
            rcode, days_ago, units = plan["redeem"]
            rta.record_transaction(investor_ref=investor, folio=folio, scheme_code=rcode, txn_type="redeem",
                                   txn_date=today - timedelta(days=days_ago), units=units)
        # Two recent platform orders so the order book is not empty.
        lcode, ldays, lrupees = plan["lumps"][-1]
        scheme = mf_rta.SCHEMES[lcode]
        conn.execute(
            """INSERT INTO mutual_fund_orders (id, customer_id, scheme_code, scheme_name, order_type, amount_paise, units, folio,
               rta_ref, status, created_at) VALUES (?,?,?,?,'purchase',?,?,?,?,'confirmed',?)""",
            (new_id(), investor, lcode, scheme["scheme_name"], lrupees * 100,
             round(lrupees / mf_rta.nav_on(lcode, today - timedelta(days=ldays)), 3), folio,
             "MOCK-RTA-" + new_id().replace("-", "")[:10].upper(), now_iso()),
        )
        conn.execute(
            """INSERT INTO mutual_fund_orders (id, customer_id, scheme_code, scheme_name, order_type, amount_paise, units, folio,
               rta_ref, status, created_at) VALUES (?,?,?,?,'sip',?,NULL,?,?,'confirmed',?)""",
            (new_id(), investor, code, mf_rta.SCHEMES[code]["scheme_name"], rupees * 100, folio,
             "MOCK-SIP-" + new_id().replace("-", "")[:10].upper(), now_iso()),
        )


register_seed(80, "e1_wealth", seed_wealth)

import os
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field, field_validator

from db import (
    create_lead,
    create_payment,
    get_conn,
    get_lead,
    get_payment,
    get_payment_by_session,
    get_user_by_email,
    set_payment_status,
    set_payment_user,
    upgrade_user_tier,
)
from deps import get_current_user, get_optional_user
from integrations.stripe_adapter import (
    StripeNotConfigured,
    create_checkout_session,
    retrieve_checkout_session,
    verify_webhook_event,
)

router = APIRouter(tags=["pricing"])

FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")

SETUP_FEE_CENTS = 500000  # one-time payment of 5,000 USD on every plan


def _plan(plan_id, name, seats, hosting, monthly_cents, annual_cents, description, features):
    return {
        "id": plan_id,
        "name": name,
        "seats": seats,
        "hosting": hosting,
        "currency": "usd",
        "setup_fee_cents": SETUP_FEE_CENTS,
        "monthly_cents": monthly_cents,
        "annual_cents": annual_cents,
        # Charged at checkout: the one-time setup fee plus the first month.
        "amount_cents": SETUP_FEE_CENTS + monthly_cents,
        "description": description,
        "features": features,
    }


PLANS = {
    "mid": _plan(
        "mid", "Mid", 50, "shared platform", 750000, 9000000,
        "For teams of up to 50 on our shared Crown AI platform.",
        [
            "50 seats",
            "Shared platform",
            "Unlimited generations and downloads",
        ],
    ),
    "large": _plan(
        "large", "Large", 250, "dedicated", 2250000, 27000000,
        "For larger organizations that need a dedicated Crown AI deployment.",
        [
            "250 seats",
            "Dedicated platform",
            "Unlimited generations and downloads",
        ],
    ),
    "global": _plan(
        "global", "Global", 1000, "HA + DR", 6000000, 72000000,
        "For global enterprises that need high availability and disaster recovery.",
        [
            "1,000 seats",
            "High availability (HA) + disaster recovery (DR)",
            "Unlimited generations and downloads",
        ],
    ),
}

PAID_PLAN_IDS = set(PLANS)

PHONE_RE = re.compile(r"^[0-9+()\-\s]{7,20}$")


class LeadCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    email: EmailStr
    company: str = Field(..., min_length=1, max_length=200)
    phone: str = Field(..., min_length=1, max_length=30)
    plan: str

    @field_validator("phone")
    @classmethod
    def valid_phone(cls, v: str) -> str:
        if not PHONE_RE.match(v):
            raise ValueError("must be a valid phone number")
        return v

    @field_validator("plan")
    @classmethod
    def valid_plan(cls, v: str) -> str:
        if v not in PAID_PLAN_IDS:
            raise ValueError(f"plan must be one of {sorted(PAID_PLAN_IDS)}")
        return v


class CheckoutCreate(BaseModel):
    lead_id: str
    plan: str

    @field_validator("plan")
    @classmethod
    def valid_plan(cls, v: str) -> str:
        if v not in PAID_PLAN_IDS:
            raise ValueError(f"plan must be one of {sorted(PAID_PLAN_IDS)}")
        return v


@router.get("/pricing/plans")
def get_plans():
    return list(PLANS.values())


@router.post("/pricing/leads", status_code=201)
def post_lead(body: LeadCreate):
    with get_conn() as conn:
        lead = create_lead(conn, body.name.strip(), body.email, body.company.strip(), body.phone, body.plan)
        return {
            "id": lead["id"],
            "name": lead["name"],
            "email": lead["email"],
            "company": lead["company"],
            "phone": lead["phone"],
            "plan": lead["plan"],
        }


@router.post("/pricing/checkout")
def post_checkout(body: CheckoutCreate, current_user: Optional[dict] = Depends(get_optional_user)):
    """Starts a hosted Stripe Checkout for a captured lead.

    Signed-in callers get a session tied to their account. Anonymous visitors
    (lead captured, not signed in yet) may also pay: the session is tied to
    the lead's email, and the payment is claimed by -- and upgrades -- the
    account with that email when it signs in (or immediately via the webhook
    if that account already exists).
    """
    plan = PLANS[body.plan]
    with get_conn() as conn:
        lead = get_lead(conn, body.lead_id)
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found. Submit the lead-capture form first.")
        if lead["plan"] != body.plan:
            raise HTTPException(status_code=400, detail="Lead was captured for a different plan.")

        user_id = current_user["id"] if current_user else None
        metadata = {"lead_id": lead["id"], "plan": body.plan}
        if user_id:
            metadata["user_id"] = user_id
        try:
            session = create_checkout_session(
                plan_name=plan["name"],
                amount_cents=plan["amount_cents"],
                currency=plan["currency"],
                customer_email=current_user["email"] if current_user else lead["email"],
                client_reference_id=user_id or f"lead_{lead['id']}",
                # Stripe substitutes {CHECKOUT_SESSION_ID} on redirect; the
                # Pricing page uses it to confirm the payment server-side.
                success_url=f"{FRONTEND_URL}/pricing?checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
                cancel_url=f"{FRONTEND_URL}/pricing?checkout=cancelled",
                metadata=metadata,
            )
        except StripeNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc))

        create_payment(
            conn,
            user_id=user_id,
            lead_id=lead["id"],
            plan=body.plan,
            amount_cents=plan["amount_cents"],
            currency=plan["currency"],
            stripe_session_id=session["id"],
        )
        return {"checkout_url": session["url"], "session_id": session["id"]}


@router.post("/pricing/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")
    try:
        event = verify_webhook_event(payload, signature)
    except StripeNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid Stripe webhook signature.")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        with get_conn() as conn:
            payment = get_payment_by_session(conn, session["id"])
            if payment:
                _mark_payment_succeeded(conn, payment)
    return {"received": True}


def _mark_payment_succeeded(conn, payment):
    """Idempotent: safe to run from both the webhook and the return-from-
    checkout confirmation, in either order."""
    set_payment_status(conn, payment["id"], "succeeded")
    user_id = payment["user_id"]
    if not user_id and payment["lead_id"]:
        # Anonymous checkout: upgrade the matching account if it exists;
        # otherwise upsert_user claims it at first sign-in.
        lead = get_lead(conn, payment["lead_id"])
        user = get_user_by_email(conn, lead["email"]) if lead else None
        if user:
            user_id = user["id"]
            set_payment_user(conn, payment["id"], user_id)
    if user_id:
        upgrade_user_tier(conn, user_id, payment["plan"])


@router.get("/pricing/checkout/{session_id}")
def confirm_checkout(session_id: str, current_user: Optional[dict] = Depends(get_optional_user)):
    """Called by the Pricing page when Stripe redirects back. Asks Stripe for
    the session's real payment status (never trusts the redirect itself), so
    the upgrade lands even if webhook delivery is delayed or not set up."""
    with get_conn() as conn:
        payment = get_payment_by_session(conn, session_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Checkout session not found.")
        if current_user and payment["user_id"] and payment["user_id"] != current_user["id"]:
            raise HTTPException(status_code=403, detail="This checkout belongs to another account.")
        if payment["status"] != "succeeded":
            try:
                session = retrieve_checkout_session(session_id)
            except StripeNotConfigured as exc:
                raise HTTPException(status_code=503, detail=str(exc))
            except Exception:
                raise HTTPException(status_code=502, detail="Could not confirm the payment with Stripe. Please retry.")
            if session["payment_status"] in ("paid", "no_payment_required"):
                _mark_payment_succeeded(conn, payment)
            payment = get_payment(conn, payment["id"])
        return {"status": payment["status"], "plan": payment["plan"]}


@router.get("/pricing/payments/{payment_id}")
def get_payment_detail(payment_id: str, current_user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        payment = get_payment(conn, payment_id)
        if not payment:
            raise HTTPException(status_code=404, detail="Payment not found.")
        if payment["user_id"] != current_user["id"]:
            raise HTTPException(status_code=403, detail="You do not have access to this payment.")
        return {
            "id": payment["id"],
            "plan": payment["plan"],
            "amount_cents": payment["amount_cents"],
            "currency": payment["currency"],
            "status": payment["status"],
            "created_at": payment["created_at"],
        }

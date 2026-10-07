"""Real Stripe adapter for live Crown AI plan purchases.

Credentials are read exclusively from the STRIPE_SECRET_KEY environment
variable (and STRIPE_WEBHOOK_SECRET for webhook signature verification) --
never hardcoded. If the key is missing, calling code gets a clear,
actionable error instead of a silent no-op or an import-time crash.
"""
import os

import stripe


class StripeNotConfigured(RuntimeError):
    def __init__(self):
        super().__init__(
            "Stripe is not configured on this server. Set the STRIPE_SECRET_KEY "
            "environment variable to a real Stripe secret key to enable live payments."
        )


class StripeWebhookNotConfigured(StripeNotConfigured):
    def __init__(self):
        RuntimeError.__init__(
            self,
            "Stripe webhooks are not configured on this server. Set the STRIPE_WEBHOOK_SECRET "
            "environment variable to the signing secret of your Stripe webhook endpoint.",
        )


def _client() -> "stripe":
    secret_key = os.environ.get("STRIPE_SECRET_KEY")
    if not secret_key:
        raise StripeNotConfigured()
    stripe.api_key = secret_key
    return stripe


def create_checkout_session(
    *,
    plan_name: str,
    amount_cents: int,
    currency: str,
    customer_email: str,
    client_reference_id: str,
    success_url: str,
    cancel_url: str,
    metadata: dict,
):
    """Creates a live Stripe Checkout Session for a one-time plan purchase."""
    client = _client()
    return client.checkout.Session.create(
        mode="payment",
        line_items=[
            {
                "price_data": {
                    "currency": currency,
                    "product_data": {"name": f"Crown AI - {plan_name} plan"},
                    "unit_amount": amount_cents,
                },
                "quantity": 1,
            }
        ],
        customer_email=customer_email,
        client_reference_id=client_reference_id,
        metadata=metadata,
        success_url=success_url,
        cancel_url=cancel_url,
    )


def verify_webhook_event(payload: bytes, signature_header: str):
    """Verifies and constructs a Stripe webhook Event, raising on a bad signature."""
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not webhook_secret:
        raise StripeWebhookNotConfigured()
    client = _client()
    return client.Webhook.construct_event(payload, signature_header, webhook_secret)


def retrieve_checkout_session(session_id: str) -> dict:
    """Fetches a Checkout Session from Stripe so a payment can be confirmed
    server-side when the visitor returns from checkout, without waiting on
    (or depending on) webhook delivery. Returns {"id", "payment_status"}."""
    client = _client()
    session = client.checkout.Session.retrieve(session_id)
    return {"id": session["id"], "payment_status": session["payment_status"]}

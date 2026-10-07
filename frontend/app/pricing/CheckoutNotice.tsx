"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Alert } from "@/components/States";
import { api, ApiError, friendlyDetail } from "@/lib/api";
import { getToken } from "@/lib/auth";
import { PRODUCT_NAME } from "@/lib/brand";
import { tierName } from "@/lib/plans";

type Confirmation =
  | { state: "checking" }
  | { state: "succeeded"; plan: string }
  | { state: "pending" }
  | { state: "error"; message: string };

// Derived purely from the URL, so nothing else on the page (e.g. reloading
// plans) can clear it before it renders.
export default function CheckoutNotice() {
  const searchParams = useSearchParams();
  const checkoutError = searchParams.get("error");
  const checkoutState = searchParams.get("checkout");
  const sessionId = searchParams.get("session_id");
  const reason = searchParams.get("reason");

  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);

  // Stripe's redirect alone proves nothing; ask the backend, which checks
  // the session with Stripe and upgrades the account once it's paid.
  const confirm = useCallback(async (sid: string) => {
    setConfirmation({ state: "checking" });
    const path = `/pricing/checkout/${encodeURIComponent(sid)}`;
    try {
      let res: { status: string; plan: string };
      try {
        res = await api.get(path, getToken());
      } catch (err) {
        // An expired sign-in shouldn't block confirming the payment itself.
        if (!(err instanceof ApiError && err.status === 401)) throw err;
        res = await api.get(path, null);
      }
      setConfirmation(res.status === "succeeded" ? { state: "succeeded", plan: res.plan } : { state: "pending" });
    } catch (err) {
      setConfirmation({
        state: "error",
        message: err instanceof ApiError ? friendlyDetail(err.detail) : "Could not confirm your payment.",
      });
    }
  }, []);

  useEffect(() => {
    if (checkoutState === "success" && sessionId) confirm(sessionId);
  }, [checkoutState, sessionId, confirm]);

  let content: React.ReactNode = null;
  if (checkoutError) {
    content = (
      <Alert kind="error">
        We couldn&apos;t complete checkout: {checkoutError}. Please try again.
      </Alert>
    );
  } else if (checkoutState === "success" && sessionId && confirmation) {
    if (confirmation.state === "checking") {
      content = (
        <Alert kind="info">
          <span className="spinner" aria-hidden="true" /> Confirming your payment with our payment provider…
        </Alert>
      );
    } else if (confirmation.state === "succeeded") {
      const planName = tierName(confirmation.plan);
      content = (
        <Alert kind="success">
          Payment confirmed. Thank you! Your {PRODUCT_NAME} {planName} plan is active, and downloads are unlocked.{" "}
          <Link href="/crown-ai" className="font-bold underline">
            Open your workspace
          </Link>
          . If you checked out before signing in, sign in with the same email you entered at checkout.
        </Alert>
      );
    } else if (confirmation.state === "pending") {
      content = (
        <Alert kind="warning">
          Your payment is still processing. Your plan upgrades automatically once it clears.{" "}
          <button type="button" className="font-bold underline" onClick={() => confirm(sessionId)}>
            Check again
          </button>
        </Alert>
      );
    } else {
      content = (
        <Alert kind="error">
          {confirmation.message}{" "}
          <button type="button" className="font-bold underline" onClick={() => confirm(sessionId)}>
            Retry
          </button>
        </Alert>
      );
    }
  } else if (checkoutState === "success") {
    content = (
      <Alert kind="success">
        Payment received, thank you! Your {PRODUCT_NAME} account is upgraded as soon as the payment is confirmed. Sign in
        with the same email you used at checkout to use your new plan.
      </Alert>
    );
  } else if (checkoutState === "cancelled") {
    content = (
      <Alert kind="warning">
        Checkout was cancelled and you have not been charged. You can pick a plan again whenever you&apos;re ready.
      </Alert>
    );
  } else if (reason === "download") {
    content = (
      <Alert kind="info">
        Downloading generated code and reports is part of our paid plans. Pick a plan below to unlock downloads in your{" "}
        {PRODUCT_NAME} workspace.
      </Alert>
    );
  }

  if (!content) return null;
  return (
    <div className="mx-auto mb-2 max-w-2xl" aria-live="polite">
      {content}
    </div>
  );
}

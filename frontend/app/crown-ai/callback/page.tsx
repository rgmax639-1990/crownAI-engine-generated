"use client";

import { Suspense, useEffect } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import { clearPendingCheckout, getPendingCheckout, saveToken } from "@/lib/auth";
import Logo from "@/components/Logo";
import { PRODUCT_NAME } from "@/lib/brand";

function Completing({ label }: { label: string }) {
  return (
    <div className="container-page flex min-h-[55vh] flex-col items-center justify-center gap-5 py-16 text-center" role="status" aria-live="polite">
      <Logo variant="mark" height={72} decorative />
      <p className="inline-flex items-center gap-2 text-sm text-muted">
        <span className="spinner" aria-hidden="true" /> {label}
      </p>
    </div>
  );
}

function CallbackInner() {
  const router = useRouter();
  const params = useSearchParams();

  useEffect(() => {
    const token = params.get("token");
    if (!token) {
      router.replace("/crown-ai?error=sign_in_failed");
      return;
    }
    saveToken(token);

    const pending = getPendingCheckout();
    if (!pending) {
      router.replace("/crown-ai");
      return;
    }

    (async () => {
      try {
        const checkout = await api.post<{ checkout_url: string }>(
          "/pricing/checkout",
          { lead_id: pending.leadId, plan: pending.plan },
          token
        );
        clearPendingCheckout();
        window.location.href = checkout.checkout_url;
      } catch (err) {
        clearPendingCheckout();
        const message = err instanceof ApiError ? String(err.message) : "checkout_failed";
        router.replace(`/pricing?error=${encodeURIComponent(message)}`);
      }
    })();
  }, [params, router]);

  return <Completing label={`Completing sign-in to ${PRODUCT_NAME}…`} />;
}

export default function CrownAiCallbackPage() {
  return (
    <Suspense fallback={<Completing label="Loading…" />}>
      <CallbackInner />
    </Suspense>
  );
}

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { formatPrice, isPlanList, Plan } from "@/lib/plans";
import LeadCaptureModal from "@/components/LeadCaptureModal";
import { ErrorState } from "@/components/States";

type Props = { initialPlans: Plan[] };

export default function PricingPlans({ initialPlans }: Props) {
  // Cards render immediately from the server-provided plans; the live list
  // is refreshed in the background rather than gating the page on it.
  const [plans, setPlans] = useState<Plan[]>(initialPlans);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [modalPlan, setModalPlan] = useState<Plan | null>(null);
  // Server-rendered buttons are visible before React attaches handlers; keep
  // them disabled until hydration so an early click isn't silently lost.
  const [hydrated, setHydrated] = useState(false);
  const mounted = useRef(true);

  const loadPlans = useCallback(async () => {
    setLoadError(null);
    setRefreshing(true);
    try {
      const fresh = await api.get<Plan[]>("/pricing/plans");
      if (!mounted.current) return;
      if (isPlanList(fresh)) setPlans(fresh);
    } catch {
      if (!mounted.current) return;
      setLoadError(
        "Could not load pricing from the server right now, so we're showing our standard published plans. Checkout may be unavailable until the connection is back."
      );
    } finally {
      if (mounted.current) setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    setHydrated(true);
    loadPlans();
    return () => {
      mounted.current = false;
    };
  }, [loadPlans]);

  return (
    <>
      {loadError && (
        <div className="mx-auto mt-6 max-w-2xl">
          <ErrorState title="Live pricing unavailable" message={loadError} onRetry={loadPlans} retrying={refreshing} />
        </div>
      )}

      {plans.length === 0 ? (
        <p className="body-muted mt-10 text-center">No plans are available right now. Please check back shortly.</p>
      ) : (
        <div className="mt-10 grid gap-6 lg:grid-cols-3">
          {plans.map((plan) => {
            const featured = plan.id === "large";
            return (
              <div key={plan.id} className={`card flex flex-col ${featured ? "card-featured lg:-translate-y-2" : ""}`}>
                <div className="flex min-h-[1.75rem] items-center">
                  {featured && <span className="badge badge-gold">♛ Most popular</span>}
                </div>
                <h2 className="mt-3 text-xl font-bold">{plan.name}</h2>
                <p className="text-sm font-semibold text-muted">
                  {plan.seats.toLocaleString("en-US")} seats, {plan.hosting}
                </p>
                <p className="mt-3 text-3xl font-bold tracking-tight text-brand">
                  {formatPrice(plan.monthly_cents, plan.currency)}
                  <span className="text-sm font-semibold text-muted"> per month</span>
                </p>
                <p className="mt-1 text-sm font-semibold text-ink">
                  or {formatPrice(plan.annual_cents, plan.currency)} per year
                </p>
                <p className="mt-1 text-sm text-muted">
                  + one-time payment of {formatPrice(plan.setup_fee_cents, plan.currency)}
                </p>
                <p className="body-muted mt-3">{plan.description}</p>
                <ul className="mt-5 flex-1 space-y-2 border-t border-line pt-5 text-sm text-ink">
                  {plan.features.map((f) => (
                    <li key={f} className="flex gap-2">
                      <span className="font-bold text-brand" aria-hidden="true">
                        ✓
                      </span>
                      <span>{f}</span>
                    </li>
                  ))}
                </ul>
                <button
                  type="button"
                  className="btn-primary mt-6"
                  onClick={() => setModalPlan(plan)}
                  disabled={!hydrated}
                >
                  Upgrade to {plan.name}
                </button>
              </div>
            );
          })}
        </div>
      )}

      {modalPlan && (
        <LeadCaptureModal plan={modalPlan.id} planLabel={modalPlan.name} onClose={() => setModalPlan(null)} />
      )}
    </>
  );
}

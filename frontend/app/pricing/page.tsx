import { Suspense } from "react";
import { PageHeader } from "@/components/States";
import { API_BASE } from "@/lib/api";
import { PRODUCT_NAME, pageTitle } from "@/lib/brand";
import { isPlanList, Plan, STATIC_PLANS } from "@/lib/plans";
import CheckoutNotice from "./CheckoutNotice";
import PricingPlans from "./PricingPlans";

export const metadata = { title: pageTitle(`${PRODUCT_NAME} Pricing`) };

// Plans change rarely: render them on the server (cached for an hour) so the
// cards are in the initial HTML instead of waiting on hydration + a client
// fetch. A slow or down API never blocks the page -- the static copy is used.
export const revalidate = 3600;

async function loadInitialPlans(): Promise<Plan[]> {
  // API_BASE_INTERNAL lets the Next server reach the backend by a
  // network-internal name (e.g. http://backend:8000 in docker-compose),
  // while the browser keeps using NEXT_PUBLIC_API_BASE.
  const base = process.env.API_BASE_INTERNAL || API_BASE;
  try {
    const res = await fetch(`${base}/pricing/plans`, {
      next: { revalidate: 3600 },
      // Short budget: a slow API must never delay first paint of pricing.
      signal: AbortSignal.timeout(800),
    });
    if (!res.ok) return STATIC_PLANS;
    const data: unknown = await res.json();
    return isPlanList(data) ? data : STATIC_PLANS;
  } catch {
    return STATIC_PLANS;
  }
}

export default async function PricingPage() {
  const initialPlans = await loadInitialPlans();

  return (
    <div>
      <PageHeader
        centered
        eyebrow="Pricing"
        title={`Simple pricing for ${PRODUCT_NAME}`}
        intro="Every plan has a one-time payment of $5,000 plus a monthly or yearly fee, sized to your team."
      />
      <section className="section">
        <div className="container-page">
          {/* Only the part that reads ?error / ?checkout needs a Suspense
              boundary; the plan cards below stay in the server-rendered HTML. */}
          <Suspense fallback={null}>
            <CheckoutNotice />
          </Suspense>

          <PricingPlans initialPlans={initialPlans} />

          <p className="body-muted mx-auto mt-10 max-w-xl text-center">
            Payments are processed live by Stripe on a secure hosted checkout page. Card details never touch our
            servers. Prices are in US Dollars. Checkout charges the one-time payment plus your first month.
          </p>
        </div>
      </section>
    </div>
  );
}

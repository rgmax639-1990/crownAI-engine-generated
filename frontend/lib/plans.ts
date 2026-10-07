export type Plan = {
  id: string;
  name: string;
  seats: number;
  hosting: string;
  currency: string;
  setup_fee_cents: number;
  monthly_cents: number;
  annual_cents: number;
  // Charged at checkout: setup fee + first month.
  amount_cents: number;
  description: string;
  features: string[];
};

const SETUP_FEE_CENTS = 500000;

function plan(
  id: string,
  name: string,
  seats: number,
  hosting: string,
  monthly_cents: number,
  annual_cents: number,
  description: string,
  features: string[]
): Plan {
  return {
    id,
    name,
    seats,
    hosting,
    currency: "usd",
    setup_fee_cents: SETUP_FEE_CENTS,
    monthly_cents,
    annual_cents,
    amount_cents: SETUP_FEE_CENTS + monthly_cents,
    description,
    features,
  };
}

// Mirror of the backend's PLANS (backend/routers/pricing.py). Used to render
// pricing instantly (server-side and on first paint) and as the fallback when
// the pricing API is slow or unreachable. The backend stays the source of
// truth for what is actually charged at checkout.
export const STATIC_PLANS: Plan[] = [
  plan("mid", "Mid", 50, "shared platform", 750000, 9000000, "For teams of up to 50 on our shared Crown AI platform.", [
    "50 seats",
    "Shared platform",
    "Unlimited generations and downloads",
  ]),
  plan(
    "large",
    "Large",
    250,
    "dedicated",
    2250000,
    27000000,
    "For larger organizations that need a dedicated Crown AI deployment.",
    ["250 seats", "Dedicated platform", "Unlimited generations and downloads"]
  ),
  plan(
    "global",
    "Global",
    1000,
    "HA + DR",
    6000000,
    72000000,
    "For global enterprises that need high availability and disaster recovery.",
    ["1,000 seats", "High availability (HA) + disaster recovery (DR)", "Unlimited generations and downloads"]
  ),
];

// Display names for paid tiers, including legacy ones from earlier plans.
const TIER_NAMES: Record<string, string> = {
  mid: "Mid",
  large: "Large",
  global: "Global",
  pro: "Pro",
  enterprise: "Enterprise",
};

export function tierName(tier: string): string {
  return TIER_NAMES[tier] ?? tier.charAt(0).toUpperCase() + tier.slice(1);
}

export function isPlanList(value: unknown): value is Plan[] {
  return (
    Array.isArray(value) &&
    value.length > 0 &&
    value.every(
      (p) =>
        p &&
        typeof p.id === "string" &&
        typeof p.name === "string" &&
        typeof p.amount_cents === "number" &&
        typeof p.setup_fee_cents === "number" &&
        typeof p.monthly_cents === "number" &&
        typeof p.annual_cents === "number" &&
        typeof p.currency === "string" &&
        Array.isArray(p.features)
    )
  );
}

export function formatPrice(cents: number, currency: string) {
  if (cents === 0) return "Free";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: currency.toUpperCase(),
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(cents / 100);
}

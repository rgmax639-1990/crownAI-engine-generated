"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { ConsentPolicy, policyForCountry } from "@/lib/consent";
import { guessCountryCode } from "@/lib/geo";

const DISMISS_KEY = "crownai_consent_dismissed";

function dismissedRegime(): string | null {
  try {
    return localStorage.getItem(DISMISS_KEY);
  } catch {
    return null;
  }
}

export default function ConsentBanner() {
  const [policy, setPolicy] = useState<ConsentPolicy | null>(null);
  const [visible, setVisible] = useState(false);
  // Set once the visitor answers, so a late API response can't re-open it.
  const answered = useRef(false);

  useEffect(() => {
    let cancelled = false;
    const country = guessCountryCode();
    const previous = dismissedRegime();

    const apply = (p: ConsentPolicy) => {
      if (cancelled || answered.current) return;
      setPolicy(p);
      setVisible(previous !== p.regime);
    };

    // First-time visitors see their jurisdiction's banner right away from
    // the local copy of the rules; the API answer then replaces it. Returning
    // visitors who already answered wait for the API instead, so they don't
    // get a flash of a banner they already dismissed.
    if (previous === null) apply(policyForCountry(country ?? "US"));

    (async () => {
      let p: ConsentPolicy;
      try {
        // No detectable country -> let the backend resolve it from its
        // geo-IP/CDN header. Short, single attempt: the local rules are an
        // equivalent fallback, so there's no point keeping the visitor waiting.
        const query = country ? `?country=${encodeURIComponent(country)}` : "";
        p = await api.get<ConsentPolicy>(`/consent/policy${query}`, null, { timeoutMs: 4000, retries: 0 });
      } catch {
        // Consent service unreachable: still apply the visitor's own
        // jurisdiction rules (DPDP / GDPR opt-in / CCPA ...) from the local
        // copy, never a generic notice that would skip required opt-in.
        p = policyForCountry(country ?? "US");
      }
      apply(p);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (!visible || !policy) return null;

  const dismiss = (accepted: boolean) => {
    answered.current = true;
    try {
      localStorage.setItem(DISMISS_KEY, policy.regime);
      localStorage.setItem("crownai_consent_choice", accepted ? "accepted" : "rejected");
    } catch {
      // ignore
    }
    setVisible(false);
  };

  return (
    <div
      role="region"
      aria-label="Privacy and cookie consent"
      className="fixed inset-x-0 bottom-0 z-50 border-t border-line-strong bg-surface/95 shadow-lg backdrop-blur"
    >
      <div className="container-page flex flex-col gap-3 py-4 sm:flex-row sm:items-center sm:justify-between">
        <p className="text-sm text-ink">
          <span className="badge mr-2">{policy.regime_name}</span>
          {policy.banner_text}{" "}
          <a href="/legal/privacy" className="link">
            Privacy Policy
          </a>
        </p>
        <div className="flex shrink-0 flex-col gap-2 min-[400px]:flex-row">
          {policy.requires_opt_in && (
            <button className="btn-secondary" onClick={() => dismiss(false)}>
              Reject non-essential
            </button>
          )}
          <button className="btn-primary" onClick={() => dismiss(true)}>
            Accept
          </button>
        </div>
      </div>
    </div>
  );
}

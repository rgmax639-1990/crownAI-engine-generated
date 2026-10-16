"use client";

import { useState } from "react";
import { useEnvironment } from "@/lib/env";
import { Locale, t } from "@/lib/i18n";
import { Badge, MockBadge } from "./ui";

/** Environment banner shown on every shell while any provider runs on a mock. */
export function MockBanner({ locale = "en" }: { locale?: Locale }) {
  const { env } = useEnvironment();
  const [open, setOpen] = useState(false);
  if (!env || env.mock_count === 0) return null;
  return (
    <div className="border-b border-warning bg-warning-soft text-fg" role="region" aria-label="Environment notice">
      <div className="page-container flex flex-wrap items-center gap-x-3 gap-y-1 py-2 text-sm">
        <MockBadge label={`MOCK · ${env.environment.toUpperCase()}`} />
        <span className="min-w-0 flex-1">{t(locale, "mockBanner")}</span>
        {env.mock_otp_hint && (
          <span className="whitespace-nowrap">
            {t(locale, "mockOtp")}: <strong className="font-mono">{env.mock_otp_hint}</strong>
          </span>
        )}
        <button type="button" onClick={() => setOpen((o) => !o)} aria-expanded={open} aria-controls="mock-providers"
          className="min-h-tap rounded-md px-2 font-semibold underline underline-offset-2">
          {t(locale, "details")}
        </button>
      </div>
      {open && (
        <div id="mock-providers" className="page-container pb-3">
          <ul className="flex flex-wrap gap-2">
            {env.providers.map((p) => (
              <li key={p.provider}>
                <Badge tone={p.badge === "LIVE" ? "success" : "mock"}>
                  {p.badge} · {p.label}
                </Badge>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

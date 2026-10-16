"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { isMobile } from "@/lib/form";
import type { Locale } from "@/lib/i18n";
import { Button, MockBadge } from "./ui";

/** Response of POST /otp/send. */
export type OtpSent = {
  otp_ref: string;
  expires_at: string;
  resend_after_seconds: number;
  masked_mobile: string;
  mock: boolean;
  mock_otp?: string;
};

const text = {
  en: { send: "Send OTP", resend: "Resend OTP", wait: "Resend in", sent: "OTP sent to", invalid: "Enter a valid mobile number first", mock: "Mock SMS: use OTP" },
  hi: { send: "OTP भेजें", resend: "OTP दोबारा भेजें", wait: "दोबारा भेजें", sent: "OTP भेजा गया", invalid: "पहले सही मोबाइल नंबर दर्ज करें", mock: "मॉक SMS: OTP" },
};

/**
 * "Send OTP" button for public flows (POST /otp/send). Shows the masked destination, a
 * resend countdown and, with the mock SMS gateway, the mock OTP with a MOCK label.
 */
export function OtpRequest({ mobile, purpose, locale = "en", onSent, disabled }: {
  mobile: string;
  purpose: "registration" | "complaint" | "complaint_track" | "fraud_report" | "onboarding" | "onboarding_resume" | "profile_change" | "beneficiary" | "login";
  locale?: Locale;
  onSent?: (info: OtpSent) => void;
  disabled?: boolean;
}) {
  const tx = text[locale];
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState<OtpSent | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [wait, setWait] = useState(0);

  useEffect(() => {
    if (wait <= 0) return;
    const t = setTimeout(() => setWait((w) => w - 1), 1000);
    return () => clearTimeout(t);
  }, [wait]);

  const send = async () => {
    setError(null);
    if (!isMobile(mobile)) {
      setError(tx.invalid);
      return;
    }
    setSending(true);
    try {
      const res = await api<OtpSent>("/otp/send", { method: "POST", auth: false, body: { mobile, purpose } });
      setSent(res);
      setWait(res.resend_after_seconds || 30);
      onSent?.(res);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not send OTP");
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="space-y-2">
      <Button variant="secondary" size="sm" onClick={send} loading={sending} disabled={disabled || wait > 0}>
        {wait > 0 ? `${tx.wait} ${wait}s` : sent ? tx.resend : tx.send}
      </Button>
      <div aria-live="polite" className="space-y-1 text-sm">
        {sent && (
          <p className="text-fg-muted">
            {tx.sent} {sent.masked_mobile}
          </p>
        )}
        {sent?.mock && sent.mock_otp && (
          <p className="flex flex-wrap items-center gap-2 text-fg-muted">
            <MockBadge /> {tx.mock} <strong className="font-mono text-fg">{sent.mock_otp}</strong>
          </p>
        )}
        {error && <p className="font-medium text-danger">{error}</p>}
      </div>
    </div>
  );
}

"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { api, ApiError, friendlyDetail } from "@/lib/api";
import { clearPendingCheckout, clearToken, getToken, savePendingCheckout } from "@/lib/auth";
import SignInOptions from "@/components/SignInOptions";
import Logo from "@/components/Logo";
import { Alert } from "@/components/States";

type Props = {
  plan: string;
  planLabel: string;
  onClose: () => void;
};

type Step = "form" | "submitting" | "checking-out" | "error";
type SavedLead = { id: string; key: string };
type Fields = { name: string; email: string; company: string; phone: string };
type Errors = Partial<Record<keyof Fields, string>>;

// Mirrors the backend's validation (routers/pricing.py) so problems are shown
// inline before a round trip.
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;
const PHONE_RE = /^[0-9+()\-\s]{7,20}$/;

function validate(f: Fields): Errors {
  const e: Errors = {};
  if (!f.name.trim()) e.name = "Please enter your name.";
  else if (f.name.trim().length > 200) e.name = "Name must be 200 characters or fewer.";
  if (!f.email.trim()) e.email = "Please enter your email.";
  else if (!EMAIL_RE.test(f.email.trim())) e.email = "Enter a valid email address, like you@company.com.";
  if (!f.company.trim()) e.company = "Please enter your company.";
  else if (f.company.trim().length > 200) e.company = "Company must be 200 characters or fewer.";
  if (!f.phone.trim()) e.phone = "Please enter a phone number.";
  else if (!PHONE_RE.test(f.phone.trim())) e.phone = "Use 7–20 digits; spaces, +, - and () are allowed.";
  return e;
}

export default function LeadCaptureModal({ plan, planLabel, onClose }: Props) {
  const [fields, setFields] = useState<Fields>({ name: "", email: "", company: "", phone: "" });
  const [touched, setTouched] = useState<Partial<Record<keyof Fields, boolean>>>({});
  const [submitted, setSubmitted] = useState(false);
  const [step, setStep] = useState<Step>("form");
  const [error, setError] = useState<string | null>(null);
  // Remembered so retrying after a checkout failure doesn't record the same
  // lead twice (a new lead is only created if the details change).
  const [savedLead, setSavedLead] = useState<SavedLead | null>(null);
  const firstFieldRef = useRef<HTMLInputElement>(null);

  const errors = validate(fields);
  const busy = step === "submitting" || step === "checking-out";
  const showError = (k: keyof Fields) => (submitted || touched[k]) && errors[k];

  useEffect(() => {
    firstFieldRef.current?.focus();
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  function set<K extends keyof Fields>(k: K, v: string) {
    setFields((prev) => ({ ...prev, [k]: v }));
  }

  async function startCheckout(leadId: string) {
    const body = { lead_id: leadId, plan };
    const token = getToken();
    try {
      return await api.post<{ checkout_url: string }>("/pricing/checkout", body, token);
    } catch (err) {
      // A stale session shouldn't block payment: drop it and continue as an
      // anonymous checkout tied to the lead's email.
      if (token && err instanceof ApiError && err.status === 401) {
        clearToken();
        return api.post<{ checkout_url: string }>("/pricing/checkout", body);
      }
      throw err;
    }
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitted(true);
    if (busy) return;
    if (Object.keys(errors).length > 0) {
      const firstBad = (["name", "email", "company", "phone"] as const).find((k) => errors[k]);
      if (firstBad) document.getElementById(`lead-${firstBad}`)?.focus();
      return;
    }
    setStep("submitting");
    setError(null);

    const payload = {
      name: fields.name.trim(),
      email: fields.email.trim(),
      company: fields.company.trim(),
      phone: fields.phone.trim(),
      plan,
    };
    const key = JSON.stringify(payload);
    let leadId = savedLead?.key === key ? savedLead.id : null;
    if (!leadId) {
      try {
        const lead = await api.post<{ id: string }>("/pricing/leads", payload);
        leadId = lead.id;
        setSavedLead({ id: lead.id, key });
      } catch (err) {
        setStep("error");
        setError(err instanceof ApiError ? friendlyDetail(err.detail) : "Could not save your details. Please try again.");
        return;
      }
    }

    setStep("checking-out");
    try {
      const checkout = await startCheckout(leadId);
      clearPendingCheckout();
      window.location.href = checkout.checkout_url;
    } catch (err) {
      // The lead is already stored; remember it so signing in (the callback
      // resumes pending checkouts) or retrying here needs no re-entry.
      savePendingCheckout({ leadId, plan });
      setStep("error");
      const reason = (err instanceof ApiError ? friendlyDetail(err.detail) : "Something went wrong.").replace(/\.?\s*$/, ".");
      setError(`We've saved your details, but couldn't start checkout: ${reason} Please try again.`);
    }
  }

  const fieldDefs: { key: keyof Fields; label: string; type: string; autoComplete: string; placeholder: string }[] = [
    { key: "name", label: "Full name", type: "text", autoComplete: "name", placeholder: "Priya Raman" },
    { key: "email", label: "Work email", type: "email", autoComplete: "email", placeholder: "you@company.com" },
    { key: "company", label: "Company", type: "text", autoComplete: "organization", placeholder: "Acme Pvt Ltd" },
    { key: "phone", label: "Phone", type: "tel", autoComplete: "tel", placeholder: "+91 98765 43210" },
  ];

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !busy) onClose();
      }}
    >
      <div className="modal-panel max-w-md" role="dialog" aria-modal="true" aria-labelledby="lead-title" aria-describedby="lead-desc">
        <div className="flex items-start justify-between gap-4">
          <div className="flex items-center gap-3">
            <Logo variant="mark" height={40} decorative />
            <div>
              <h2 id="lead-title" className="text-lg font-bold">
                Upgrade to {planLabel}
              </h2>
              <p className="text-xs text-muted">Step 1 of 2: your details · Step 2: secure payment</p>
            </div>
          </div>
          <button type="button" onClick={onClose} aria-label="Close" className="icon-btn h-9 w-9 shrink-0" disabled={busy}>
            ✕
          </button>
        </div>

        <form onSubmit={onSubmit} noValidate className="mt-5 space-y-4">
          <p id="lead-desc" className="body-muted">
            Tell us a bit about you before checkout. Payment is handled on our secure hosted payment page; card details
            never touch our servers.
          </p>
          {fieldDefs.map((f, i) => {
            const err = showError(f.key);
            return (
              <div key={f.key}>
                <label className="label" htmlFor={`lead-${f.key}`}>
                  {f.label} <span className="text-danger" aria-hidden="true">*</span>
                </label>
                <input
                  ref={i === 0 ? firstFieldRef : undefined}
                  id={`lead-${f.key}`}
                  type={f.type}
                  className="input"
                  required
                  autoComplete={f.autoComplete}
                  placeholder={f.placeholder}
                  value={fields[f.key]}
                  disabled={busy}
                  aria-invalid={err ? true : undefined}
                  aria-describedby={err ? `lead-${f.key}-error` : undefined}
                  onChange={(e) => set(f.key, e.target.value)}
                  onBlur={() => setTouched((t) => ({ ...t, [f.key]: true }))}
                />
                {err && (
                  <p id={`lead-${f.key}-error`} className="field-error">
                    {err}
                  </p>
                )}
              </div>
            );
          })}

          {step === "error" && error && <Alert kind="error">{error}</Alert>}
          {step === "error" && savedLead && !getToken() && (
            <div className="rounded-md border border-line p-3">
              <SignInOptions
                label="Or sign in to resume checkout later"
                triggerClassName="btn-secondary w-full"
                optionsClassName="flex flex-col gap-3"
                helperText="Sign in with Google or Microsoft and we'll pick up checkout where you left off:"
              />
            </div>
          )}
          <button type="submit" className="btn-primary w-full" disabled={busy}>
            {busy && <span className="spinner" aria-hidden="true" />}
            {step === "checking-out" ? "Redirecting to checkout…" : step === "submitting" ? "Submitting…" : "Continue to checkout"}
          </button>
        </form>
      </div>
    </div>
  );
}

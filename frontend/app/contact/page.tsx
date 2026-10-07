"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, ApiError, friendlyDetail } from "@/lib/api";
import { Alert, ErrorState, PageHeader } from "@/components/States";
import { ADDRESS_LINES, BRAND_NAME, LEGAL_NAME } from "@/lib/brand";

type Status = "idle" | "submitting" | "success" | "error";
type FieldKey = "name" | "email" | "message";
type Errors = Partial<Record<FieldKey, string>>;

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;

function validate(name: string, email: string, message: string): Errors {
  const e: Errors = {};
  if (!name) e.name = "Please enter your name.";
  else if (name.length > 200) e.name = "Name must be 200 characters or fewer.";
  if (!email) e.email = "Please enter your email.";
  else if (!EMAIL_RE.test(email)) e.email = "Enter a valid email address, like you@company.com.";
  if (!message) e.message = "Please tell us how we can help.";
  else if (message.length > 5000) e.message = "Message must be 5000 characters or fewer.";
  return e;
}

export default function ContactPage() {
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Errors>({});
  // This form is server-rendered. Until React has hydrated it, the fields
  // and the submit button stay disabled: text typed into the server HTML can
  // be lost if hydration replaces that DOM, and an early click would fall
  // through to a native page reload. Hydration takes milliseconds; after it,
  // inputs are uncontrolled and read on submit.
  const [hydrated, setHydrated] = useState(false);
  useEffect(() => setHydrated(true), []);

  const [mapUrl, setMapUrl] = useState<string | null>(null);
  const [mapError, setMapError] = useState<string | null>(null);
  const [mapLoading, setMapLoading] = useState(true);

  const loadMap = useCallback(async () => {
    setMapLoading(true);
    setMapError(null);
    try {
      const res = await api.get<{ map_image_url: string }>("/contact/map");
      setMapUrl(res.map_image_url);
    } catch (err) {
      setMapError(err instanceof ApiError ? friendlyDetail(err.detail) : "Map is temporarily unavailable.");
    } finally {
      setMapLoading(false);
    }
  }, []);

  useEffect(() => {
    loadMap();
  }, [loadMap]);

  // Clear a field's error as soon as the visitor edits it.
  function clearFieldError(k: FieldKey) {
    if (fieldErrors[k]) setFieldErrors((prev) => ({ ...prev, [k]: undefined }));
  }

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (status === "submitting") return;
    const data = new FormData(e.currentTarget);
    const name = String(data.get("name") ?? "").trim();
    const email = String(data.get("email") ?? "").trim();
    const message = String(data.get("message") ?? "").trim();
    const errs = validate(name, email, message);
    setFieldErrors(errs);
    const firstBad = (["name", "email", "message"] as const).find((k) => errs[k]);
    if (firstBad) {
      setStatus("idle");
      setError(null);
      document.getElementById(firstBad)?.focus();
      return;
    }
    setStatus("submitting");
    setError(null);
    try {
      await api.post("/contact", { name, email, message });
      // The form unmounts on success, so "Send another message" starts empty.
      setStatus("success");
    } catch (err) {
      setStatus("error");
      if (err instanceof ApiError && (err.status === 404 || err.status === 405)) {
        setError("We couldn't send your message: the contact service is unavailable right now. Please try again later.");
      } else {
        setError(err instanceof ApiError ? friendlyDetail(err.detail) : "Could not submit your message.");
      }
    }
  }

  const fieldProps = (k: FieldKey) => ({
    id: k,
    name: k,
    className: "input",
    required: true,
    defaultValue: "",
    disabled: !hydrated || status === "submitting",
    "aria-invalid": fieldErrors[k] ? true : undefined,
    "aria-describedby": fieldErrors[k] ? `${k}-error` : undefined,
    onChange: () => clearFieldError(k),
  });

  const FieldError = ({ k }: { k: FieldKey }) =>
    fieldErrors[k] ? (
      <p id={`${k}-error`} className="field-error">
        {fieldErrors[k]}
      </p>
    ) : null;

  return (
    <div>
      <PageHeader
        eyebrow="Contact"
        title="Let's talk"
        intro={`Tell us about your project or question, and the ${BRAND_NAME} team in Chennai will follow up.`}
      />

      <section className="section">
        <div className="container-page grid gap-8 lg:grid-cols-[1.2fr_1fr]">
          <div className="card">
            {status === "success" ? (
              <div className="py-8 text-center" role="status">
                <span className="icon-tile mx-auto text-lg" aria-hidden="true">
                  ✓
                </span>
                <p className="mt-4 text-lg font-bold text-brand">Thank you!</p>
                <p className="body-muted mt-2">Your inquiry has been received. We&apos;ll get back to you shortly.</p>
                <button type="button" className="btn-secondary mt-6" onClick={() => setStatus("idle")}>
                  Send another message
                </button>
              </div>
            ) : (
              <form onSubmit={onSubmit} noValidate className="space-y-4">
                <h2 className="card-title">Send us a message</h2>
                <div>
                  <label className="label" htmlFor="name">
                    Name <span className="text-danger" aria-hidden="true">*</span>
                  </label>
                  <input {...fieldProps("name")} maxLength={200} autoComplete="name" />
                  <FieldError k="name" />
                </div>
                <div>
                  <label className="label" htmlFor="email">
                    Email <span className="text-danger" aria-hidden="true">*</span>
                  </label>
                  <input {...fieldProps("email")} type="email" autoComplete="email" />
                  <FieldError k="email" />
                </div>
                <div>
                  <label className="label" htmlFor="message">
                    Message <span className="text-danger" aria-hidden="true">*</span>
                  </label>
                  <textarea {...fieldProps("message")} rows={5} maxLength={5000} />
                  <FieldError k="message" />
                </div>
                {status === "error" && error && <Alert kind="error">{error}</Alert>}
                <button type="submit" className="btn-primary w-full" disabled={!hydrated || status === "submitting"}>
                  {status === "submitting" && <span className="spinner" aria-hidden="true" />}
                  {status === "submitting" ? "Sending…" : "Send message"}
                </button>
              </form>
            )}
          </div>

          <div className="space-y-6">
            <div className="card">
              <h2 className="card-title">Registered office</h2>
              <address className="body-muted mt-2 not-italic">
                <span className="font-semibold text-ink">{LEGAL_NAME}</span>
                <br />
                {ADDRESS_LINES.map((line) => (
                  <span key={line}>
                    {line}
                    <br />
                  </span>
                ))}
              </address>
            </div>
            <div className="card overflow-hidden p-0">
              {mapLoading && (
                <div className="flex min-h-[240px] flex-col items-center justify-center gap-3 p-6" role="status">
                  <span className="spinner" aria-hidden="true" />
                  <p className="body-muted">Loading map…</p>
                </div>
              )}
              {!mapLoading && mapError && (
                <div className="p-6">
                  <ErrorState title="Map unavailable" message={mapError} onRetry={loadMap} />
                </div>
              )}
              {!mapLoading && mapUrl && (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={mapUrl} alt={`Map of the ${LEGAL_NAME} office in Chennai`} className="block w-full" />
              )}
            </div>
          </div>
        </div>
      </section>
    </div>
  );
}

"use client";

/**
 * "Request a call" lead form (POST /leads). The product is prefilled and read-only; the
 * call-back consent is required (DPDP purpose `lead_contact`) and recorded with the lead.
 */
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { isEmail, isMobile, isPin, useForm } from "@/lib/form";
import type { Locale } from "@/lib/i18n";
import { Alert, Button, Checkbox, CopyButton, MockBadge, Modal, TextField } from "./ui";

export type LeadChannel = "web" | "mobile_web" | "calculator" | "assistant" | "branch_page";

export type RequestCallModalProps = {
  open: boolean;
  onClose: () => void;
  locale: Locale;
  /** CMS product slug, e.g. "personal-loan". */
  productSlug: string;
  /** Product name shown read-only in the form (defaults to the slug). */
  productName?: string;
  channel?: LeadChannel;
  /** Id of a shared calculator result to attach (from the calculators). */
  calculatorShareId?: string | null;
  /** UTM parameters; read from the page URL when omitted. */
  utm?: Record<string, string>;
};

type LeadOut = { reference_no: string; status: string; crm_ref: string | null; mock: unknown; message?: string };

type Values = { name: string; mobile: string; email: string; pin_code: string; consent_given: boolean };

const TX = {
  en: {
    title: "Request a call back",
    description: "Share your details and our team will call you within one working day.",
    name: "Full name",
    mobile: "Mobile number",
    email: "Email (optional)",
    pin: "PIN code (optional)",
    product: "Product",
    consent: "I agree that the bank may call, SMS or email me about this product.",
    consentDesc: "Purpose: contacting you about the product you asked for. You can withdraw this consent at any time from Privacy choices; we will then stop contacting you.",
    submit: "Request call back",
    cancel: "Cancel",
    close: "Close",
    successTitle: "Request received",
    successText: "Our team will call you within one working day. Keep this reference number:",
    reference: "Reference number",
    copy: "Copy",
    errName: "Enter your name (at least 2 letters)",
    errMobile: "Enter a valid 10-digit mobile number",
    errEmail: "Enter a valid email address or leave it blank",
    errPin: "Enter a valid 6-digit PIN code or leave it blank",
    errConsent: "Please agree to be contacted so we can call you back",
    failed: "We couldn't submit your request",
    mockNote: "CRM is a mock provider in this environment; no real call will be made.",
  },
  hi: {
    title: "कॉल बैक का अनुरोध करें",
    description: "अपना विवरण दें, हमारी टीम एक कार्य दिवस के भीतर आपको कॉल करेगी।",
    name: "पूरा नाम",
    mobile: "मोबाइल नंबर",
    email: "ईमेल (वैकल्पिक)",
    pin: "पिन कोड (वैकल्पिक)",
    product: "उत्पाद",
    consent: "मैं सहमत हूँ कि बैंक इस उत्पाद के बारे में मुझे कॉल, SMS या ईमेल कर सकता है।",
    consentDesc: "उद्देश्य: आपके द्वारा पूछे गए उत्पाद के बारे में आपसे संपर्क करना। आप गोपनीयता विकल्प से यह सहमति कभी भी वापस ले सकते हैं; तब हम संपर्क करना बंद कर देंगे।",
    submit: "कॉल बैक का अनुरोध करें",
    cancel: "रद्द करें",
    close: "बंद करें",
    successTitle: "अनुरोध प्राप्त हुआ",
    successText: "हमारी टीम एक कार्य दिवस के भीतर आपको कॉल करेगी। यह संदर्भ संख्या सँभाल कर रखें:",
    reference: "संदर्भ संख्या",
    copy: "कॉपी करें",
    errName: "अपना नाम दर्ज करें (कम से कम 2 अक्षर)",
    errMobile: "सही 10 अंकों का मोबाइल नंबर दर्ज करें",
    errEmail: "सही ईमेल दर्ज करें या खाली छोड़ दें",
    errPin: "सही 6 अंकों का पिन कोड दर्ज करें या खाली छोड़ दें",
    errConsent: "कॉल बैक के लिए संपर्क की सहमति दें",
    failed: "आपका अनुरोध जमा नहीं हो सका",
    mockNote: "इस वातावरण में CRM मॉक प्रदाता है; कोई वास्तविक कॉल नहीं होगी।",
  },
} as const;

function utmFromUrl(): Record<string, string> {
  if (typeof window === "undefined") return {};
  const out: Record<string, string> = {};
  try {
    const sp = new URLSearchParams(window.location.search);
    for (const k of ["utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"]) {
      const v = sp.get(k);
      if (v) out[k] = v.slice(0, 100);
    }
  } catch {
    /* ignore */
  }
  return out;
}

export function RequestCallModal({ open, onClose, locale, productSlug, productName, channel, calculatorShareId, utm }: RequestCallModalProps) {
  const tx = TX[locale] ?? TX.en;
  const [result, setResult] = useState<LeadOut | null>(null);
  const form = useForm<Values>({
    initial: { name: "", mobile: "", email: "", pin_code: "", consent_given: false },
    validate: (v) => {
      const e: Partial<Record<keyof Values, string>> = {};
      if (v.name.trim().length < 2) e.name = tx.errName;
      if (!isMobile(v.mobile)) e.mobile = tx.errMobile;
      if (v.email.trim() && !isEmail(v.email)) e.email = tx.errEmail;
      if (v.pin_code.trim() && !isPin(v.pin_code)) e.pin_code = tx.errPin;
      if (!v.consent_given) e.consent_given = tx.errConsent;
      return e;
    },
  });

  // Fresh form every time the modal opens.
  useEffect(() => {
    if (open) {
      setResult(null);
      form.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const submit = form.handleSubmit(async (v) => {
    const isMobileUa = typeof navigator !== "undefined" && /Mobi|Android/i.test(navigator.userAgent);
    const r = await api<LeadOut>("/leads", {
      method: "POST",
      auth: false,
      body: {
        name: v.name.trim(),
        mobile: v.mobile.trim(),
        email: v.email.trim() || null,
        pin_code: v.pin_code.trim() || null,
        product_slug: productSlug,
        channel: channel ?? (isMobileUa ? "mobile_web" : "web"),
        consent_given: v.consent_given,
        language: locale,
        utm: utm ?? utmFromUrl(),
        calculator_share_id: calculatorShareId || null,
      },
    });
    setResult(r);
  });

  const productLabel = productName || productSlug;

  return (
    <Modal open={open} onClose={onClose} title={result ? tx.successTitle : tx.title} description={result ? undefined : tx.description}
      footer={result ? <Button onClick={onClose}>{tx.close}</Button> : (
        <>
          <Button variant="secondary" onClick={onClose} disabled={form.submitting}>{tx.cancel}</Button>
          <Button type="submit" form="request-call-form" loading={form.submitting}>{tx.submit}</Button>
        </>
      )}>
      {result ? (
        <div className="space-y-4" role="status" aria-live="polite">
          <p className="text-fg">{tx.successText}</p>
          <div className="flex flex-wrap items-center gap-2 rounded-lg border border-success bg-success-soft p-3">
            <span className="text-sm text-fg-muted">{tx.reference}:</span>
            <strong className="font-mono text-lg">{result.reference_no}</strong>
            <CopyButton value={result.reference_no} label={tx.copy} />
          </div>
          {Boolean(result.mock) && (
            <p className="flex flex-wrap items-center gap-2 text-sm text-fg-muted">
              <MockBadge /> {tx.mockNote}
            </p>
          )}
        </div>
      ) : (
        <form id="request-call-form" noValidate onSubmit={submit} className="space-y-4">
          {form.formError && <Alert tone="danger" title={tx.failed}>{form.formError}</Alert>}
          <TextField id="name" label={tx.name} required autoComplete="name" value={form.values.name}
            onChange={(v) => form.set("name", v)} onBlur={() => form.blur("name")} error={form.errors.name} maxLength={100} />
          <TextField id="mobile" label={tx.mobile} required type="tel" inputMode="tel" autoComplete="tel" prefix="+91"
            value={form.values.mobile} onChange={(v) => form.set("mobile", v)} onBlur={() => form.blur("mobile")}
            error={form.errors.mobile} maxLength={16} />
          <div className="grid gap-4 sm:grid-cols-2">
            <TextField id="email" label={tx.email} type="email" autoComplete="email" value={form.values.email}
              onChange={(v) => form.set("email", v)} onBlur={() => form.blur("email")} error={form.errors.email} maxLength={254} />
            <TextField id="pin_code" label={tx.pin} inputMode="numeric" autoComplete="postal-code" value={form.values.pin_code}
              onChange={(v) => form.set("pin_code", v.replace(/\D/g, "").slice(0, 6))} onBlur={() => form.blur("pin_code")}
              error={form.errors.pin_code} />
          </div>
          <TextField id="product_slug" label={tx.product} value={productLabel} onChange={() => undefined} readOnly aria-readonly="true" />
          <Checkbox id="consent_given" checked={form.values.consent_given} onChange={(v) => form.set("consent_given", v)}
            label={tx.consent} description={tx.consentDesc} error={form.errors.consent_given} />
        </form>
      )}
    </Modal>
  );
}

export default RequestCallModal;

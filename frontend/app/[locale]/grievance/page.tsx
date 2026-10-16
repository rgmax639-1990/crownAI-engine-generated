"use client";

import { useParams, usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { OtpRequest } from "@/components/OtpRequest";
import {
  Alert, Badge, Button, Card, CardHeader, CopyButton, KeyValue, LoadingState, PageHeader, SelectField, TabPanel, Tabs,
  TextAreaField, TextField, TextLink, Tone,
} from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { formatDate, formatDateTime } from "@/lib/format";
import { isEmail, isMobile, isOtp, useForm } from "@/lib/form";
import { isLocale, Locale, lp } from "@/lib/i18n";

// ------------------------------------------------------------------ API shapes
export type EscalationStep = { level: string; label: string; name: string; contact: string; tat: string | null; current: boolean };
export type TimelineItem = { id: string; status: string; status_label: string; level: string | null; level_label: string | null; note: string | null; occurred_at: string; actor?: string };
export type ComplaintView = {
  ticket_id: string;
  category: string;
  status: string;
  status_label: string;
  current_level: string;
  current_level_label: string;
  created_at: string;
  updated_at: string;
  tat_due_at: string;
  tat_breached: boolean;
  resolved_at: string | null;
  resolution_note: string | null;
  rbi_ombudsman_eligible_at: string;
  rbi_ombudsman_eligible: boolean;
  rbi_cms_url: string;
  can_escalate: boolean;
  can_reopen: boolean;
  next_escalation: EscalationStep | null;
  escalation_path: EscalationStep[];
  timeline: TimelineItem[];
  mobile_masked: string;
  tat_days?: number;
  track_token?: string;
  message?: string;
};

const CATEGORIES = ["atm_card", "digital_payments", "account", "loan", "deposit", "charges", "fraud", "staff_behaviour", "other"] as const;

const TX = {
  en: {
    title: "Grievance redressal",
    intro: "File a complaint or track one you have filed. We acknowledge every complaint with a ticket ID and resolve it within the published timelines.",
    crumbHome: "Home",
    tabs: "Complaint options",
    fileTab: "File a complaint",
    trackTab: "Track a complaint",
    name: "Full name",
    mobile: "Registered mobile number",
    otp: "OTP",
    otpHint: "Send the OTP to your mobile first",
    email: "Email (optional)",
    category: "Category",
    choose: "Choose a category",
    last4: "Last 4 digits of account or card (optional)",
    description: "Describe your complaint",
    descHint: "At least 20 characters. Do not include your full card, Aadhaar or account number, OTP or PIN.",
    submit: "Submit complaint",
    ticketId: "Ticket ID",
    track: "Track complaint",
    errName: "Enter your full name",
    errMobile: "Enter a valid mobile number",
    errOtp: "Enter the 6-digit OTP",
    errEmail: "Enter a valid email or leave it blank",
    errCategory: "Choose a category",
    errLast4: "Enter exactly 4 digits",
    errDesc: "Describe the issue in at least 20 characters",
    errTicket: "Enter your ticket ID, for example CMP-20261012-7KQ2ZD",
    failed: "We couldn't submit your complaint",
    trackFailed: "We couldn't find the complaint",
    success: "Complaint registered",
    successText: "Keep your ticket ID. We have sent it to your mobile number.",
    tatDue: "We aim to resolve it by",
    rbiDate: "RBI Ombudsman option from",
    path: "Escalation path",
    trackThis: "Track this complaint",
    fileAnother: "File another complaint",
    status: "Status",
    level: "Current level",
    filed: "Filed on",
    tatBreached: "Past target date",
    timeline: "Progress",
    resolution: "Resolution",
    escalate: "Escalate",
    escalateTo: "Escalate to",
    reason: "Reason",
    reasonHint: "At least 10 characters",
    reopen: "Reopen complaint",
    reopenHint: "Not satisfied with the resolution? You can reopen within 30 days.",
    rbiTitle: "RBI Ombudsman",
    rbiEligible: "Your complaint has been open for 30 days or more without resolution. You can file it with the RBI Ombudsman.",
    rbiNotYet: "If the complaint is not resolved, you can approach the RBI Ombudsman from",
    rbiOpen: "Open cms.rbi.org.in",
    rbiRecord: "Record that I am approaching the RBI Ombudsman",
    confirm: "Confirm",
    cancel: "Cancel",
    errReason: "Give a reason of at least 10 characters",
    otpAgain: "Your verification expired. Request a new OTP to continue.",
    actionFailed: "The action could not be completed",
    trackAnother: "Track another complaint",
    copy: "Copy",
    cats: {
      atm_card: "ATM / debit or credit card", digital_payments: "UPI / internet / mobile banking", account: "Account operations",
      loan: "Loans", deposit: "Deposits", charges: "Charges and fees", fraud: "Fraud or unauthorised transaction",
      staff_behaviour: "Staff behaviour", other: "Other",
    } as Record<string, string>,
  },
  hi: {
    title: "शिकायत निवारण",
    intro: "शिकायत दर्ज करें या दर्ज की गई शिकायत की स्थिति देखें। हर शिकायत पर टिकट आईडी दी जाती है और प्रकाशित समय-सीमा में समाधान किया जाता है।",
    crumbHome: "होम",
    tabs: "शिकायत विकल्प",
    fileTab: "शिकायत दर्ज करें",
    trackTab: "शिकायत की स्थिति",
    name: "पूरा नाम",
    mobile: "पंजीकृत मोबाइल नंबर",
    otp: "OTP",
    otpHint: "पहले अपने मोबाइल पर OTP भेजें",
    email: "ईमेल (वैकल्पिक)",
    category: "श्रेणी",
    choose: "श्रेणी चुनें",
    last4: "खाता या कार्ड के अंतिम 4 अंक (वैकल्पिक)",
    description: "अपनी शिकायत का विवरण दें",
    descHint: "कम से कम 20 अक्षर। पूरा कार्ड, आधार या खाता नंबर, OTP या PIN न लिखें।",
    submit: "शिकायत जमा करें",
    ticketId: "टिकट आईडी",
    track: "स्थिति देखें",
    errName: "अपना पूरा नाम दर्ज करें",
    errMobile: "सही मोबाइल नंबर दर्ज करें",
    errOtp: "6 अंकों का OTP दर्ज करें",
    errEmail: "सही ईमेल दर्ज करें या खाली छोड़ दें",
    errCategory: "श्रेणी चुनें",
    errLast4: "ठीक 4 अंक दर्ज करें",
    errDesc: "कम से कम 20 अक्षरों में समस्या बताएं",
    errTicket: "अपनी टिकट आईडी दर्ज करें, जैसे CMP-20261012-7KQ2ZD",
    failed: "आपकी शिकायत जमा नहीं हो सकी",
    trackFailed: "शिकायत नहीं मिली",
    success: "शिकायत दर्ज हो गई",
    successText: "अपनी टिकट आईडी सँभाल कर रखें। इसे आपके मोबाइल पर भी भेजा गया है।",
    tatDue: "समाधान की लक्ष्य तिथि",
    rbiDate: "आरबीआई लोकपाल विकल्प की तिथि",
    path: "शिकायत आगे बढ़ाने का क्रम",
    trackThis: "इस शिकायत की स्थिति देखें",
    fileAnother: "एक और शिकायत दर्ज करें",
    status: "स्थिति",
    level: "वर्तमान स्तर",
    filed: "दर्ज करने की तिथि",
    tatBreached: "लक्ष्य तिथि बीत गई",
    timeline: "प्रगति",
    resolution: "समाधान",
    escalate: "आगे बढ़ाएँ",
    escalateTo: "आगे बढ़ाएँ:",
    reason: "कारण",
    reasonHint: "कम से कम 10 अक्षर",
    reopen: "शिकायत फिर से खोलें",
    reopenHint: "समाधान से संतुष्ट नहीं हैं? आप 30 दिनों के भीतर शिकायत फिर से खोल सकते हैं।",
    rbiTitle: "आरबीआई लोकपाल",
    rbiEligible: "आपकी शिकायत 30 दिन या अधिक से अनसुलझी है। आप इसे आरबीआई लोकपाल के पास दर्ज कर सकते हैं।",
    rbiNotYet: "यदि समाधान नहीं होता है, तो आप इस तिथि से आरबीआई लोकपाल से संपर्क कर सकते हैं:",
    rbiOpen: "cms.rbi.org.in खोलें",
    rbiRecord: "दर्ज करें कि मैं आरबीआई लोकपाल के पास जा रहा/रही हूँ",
    confirm: "पुष्टि करें",
    cancel: "रद्द करें",
    errReason: "कम से कम 10 अक्षरों में कारण बताएं",
    otpAgain: "आपका सत्यापन समाप्त हो गया है। जारी रखने के लिए नया OTP लें।",
    actionFailed: "कार्रवाई पूरी नहीं हो सकी",
    trackAnother: "दूसरी शिकायत की स्थिति देखें",
    copy: "कॉपी करें",
    cats: {
      atm_card: "एटीएम / डेबिट या क्रेडिट कार्ड", digital_payments: "UPI / इंटरनेट / मोबाइल बैंकिंग", account: "खाता संचालन",
      loan: "ऋण", deposit: "जमा", charges: "शुल्क", fraud: "धोखाधड़ी या अनधिकृत लेन-देन", staff_behaviour: "कर्मचारी व्यवहार", other: "अन्य",
    } as Record<string, string>,
  },
};

type Tx = (typeof TX)["en"];

function statusTone(status: string): Tone {
  if (status === "resolved" || status === "closed") return "success";
  if (status === "escalated" || status === "reopened") return "warning";
  if (status === "referred_rbi_ombudsman") return "info";
  return "primary";
}

// ------------------------------------------------------------------ page
export default function GrievancePage() {
  return (
    <Suspense fallback={<LoadingState rows={4} />}>
      <GrievanceInner />
    </Suspense>
  );
}

function GrievanceInner() {
  const params = useParams<{ locale: string }>();
  const locale: Locale = isLocale(params?.locale) ? params.locale : "en";
  const tx = TX[locale];
  const search = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const tab = search?.get("tab") === "track" ? "track" : "file";
  const [prefillTicket, setPrefillTicket] = useState<{ ticket: string; mobile: string } | null>(null);

  const setTab = (id: string) => {
    router.replace(`${pathname}?tab=${id}`, { scroll: false });
  };

  return (
    <>
      <PageHeader title={tx.title} description={tx.intro} breadcrumbs={[{ label: tx.crumbHome, href: lp(locale, "/") }, { label: tx.title }]} />
      <Tabs ariaLabel={tx.tabs} idPrefix="grv" value={tab} onChange={setTab}
        tabs={[{ id: "file", label: tx.fileTab }, { id: "track", label: tx.trackTab }]} />
      <TabPanel id="file" value={tab} idPrefix="grv">
        <FileComplaint locale={locale} tx={tx} onTrack={(ticket, mobile) => { setPrefillTicket({ ticket, mobile }); setTab("track"); }} />
      </TabPanel>
      <TabPanel id="track" value={tab} idPrefix="grv">
        <TrackComplaint locale={locale} tx={tx} prefill={prefillTicket} />
      </TabPanel>
    </>
  );
}

// ------------------------------------------------------------------ file
type FileValues = { name: string; mobile: string; otp: string; email: string; category: string; account_last4: string; description: string };

function FileComplaint({ locale, tx, onTrack }: { locale: Locale; tx: Tx; onTrack: (ticket: string, mobile: string) => void }) {
  const [done, setDone] = useState<ComplaintView | null>(null);
  const [mobileUsed, setMobileUsed] = useState("");
  const form = useForm<FileValues>({
    initial: { name: "", mobile: "", otp: "", email: "", category: "", account_last4: "", description: "" },
    validate: (v) => {
      const e: Partial<Record<keyof FileValues, string>> = {};
      if (v.name.trim().length < 2) e.name = tx.errName;
      if (!isMobile(v.mobile)) e.mobile = tx.errMobile;
      if (!isOtp(v.otp)) e.otp = tx.errOtp;
      if (v.email.trim() && !isEmail(v.email)) e.email = tx.errEmail;
      if (!v.category) e.category = tx.errCategory;
      if (v.account_last4 && !/^\d{4}$/.test(v.account_last4)) e.account_last4 = tx.errLast4;
      if (v.description.trim().length < 20) e.description = tx.errDesc;
      return e;
    },
  });

  const submit = form.handleSubmit(async (v) => {
    const r = await api<ComplaintView>("/complaints", {
      method: "POST",
      auth: false,
      body: {
        name: v.name.trim(), mobile: v.mobile.trim(), otp: v.otp.trim(), email: v.email.trim() || null, category: v.category,
        account_last4: v.account_last4 || null, description: v.description.trim(), language: locale,
      },
    });
    setMobileUsed(v.mobile.trim());
    setDone(r);
    if (typeof window !== "undefined") window.scrollTo({ top: 0, behavior: "smooth" });
  });

  if (done) {
    return (
      <Card as="section">
        <div role="status" aria-live="polite">
          <Alert tone="success" title={tx.success}>{tx.successText}</Alert>
        </div>
        <div className="mt-4 flex flex-wrap items-center gap-2 rounded-lg border border-border bg-surface-2 p-4">
          <span className="text-sm text-fg-muted">{tx.ticketId}:</span>
          <strong className="font-mono text-xl">{done.ticket_id}</strong>
          <CopyButton value={done.ticket_id} label={tx.copy} />
        </div>
        <div className="mt-4">
          <KeyValue items={[
            { label: tx.category, value: tx.cats[done.category] ?? done.category },
            { label: tx.level, value: done.current_level_label },
            { label: tx.tatDue, value: formatDate(done.tat_due_at) },
            { label: tx.rbiDate, value: formatDate(done.rbi_ombudsman_eligible_at) },
          ]} />
        </div>
        <EscalationPath tx={tx} steps={done.escalation_path} />
        <div className="mt-6 flex flex-wrap gap-2">
          <Button onClick={() => onTrack(done.ticket_id, mobileUsed)}>{tx.trackThis}</Button>
          <Button variant="secondary" onClick={() => { setDone(null); form.reset(); }}>{tx.fileAnother}</Button>
        </div>
      </Card>
    );
  }

  return (
    <Card as="section">
      <form noValidate onSubmit={submit} className="space-y-4" aria-describedby={form.formError ? "file-error" : undefined}>
        {form.formError && <div id="file-error"><Alert tone="danger" title={tx.failed}>{form.formError}</Alert></div>}
        <div className="grid gap-4 sm:grid-cols-2">
          <TextField id="name" label={tx.name} required autoComplete="name" maxLength={100} value={form.values.name}
            onChange={(v) => form.set("name", v)} onBlur={() => form.blur("name")} error={form.errors.name} />
          <TextField id="email" label={tx.email} type="email" autoComplete="email" maxLength={254} value={form.values.email}
            onChange={(v) => form.set("email", v)} onBlur={() => form.blur("email")} error={form.errors.email} />
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-2">
            <TextField id="mobile" label={tx.mobile} required type="tel" inputMode="tel" autoComplete="tel" maxLength={16}
              value={form.values.mobile} onChange={(v) => form.set("mobile", v)} onBlur={() => form.blur("mobile")} error={form.errors.mobile} />
            <OtpRequest mobile={form.values.mobile} purpose="complaint" locale={locale} />
          </div>
          <TextField id="otp" label={tx.otp} required inputMode="numeric" autoComplete="one-time-code" maxLength={6} hint={tx.otpHint}
            value={form.values.otp} onChange={(v) => form.set("otp", v.replace(/\D/g, "").slice(0, 6))} onBlur={() => form.blur("otp")}
            error={form.errors.otp} />
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <SelectField id="category" label={tx.category} required placeholder={tx.choose} value={form.values.category}
            onChange={(v) => form.set("category", v)} error={form.errors.category}
            options={CATEGORIES.map((c) => ({ value: c, label: tx.cats[c] }))} />
          <TextField id="account_last4" label={tx.last4} inputMode="numeric" maxLength={4} value={form.values.account_last4}
            onChange={(v) => form.set("account_last4", v.replace(/\D/g, "").slice(0, 4))} onBlur={() => form.blur("account_last4")}
            error={form.errors.account_last4} />
        </div>
        <TextAreaField id="description" label={tx.description} required rows={5} maxLength={2000} value={form.values.description}
          onChange={(v) => form.set("description", v)} error={form.errors.description}
          hint={`${tx.descHint} ${form.values.description.length}/2000`} />
        <Button type="submit" loading={form.submitting}>{tx.submit}</Button>
      </form>
    </Card>
  );
}

function EscalationPath({ tx, steps }: { tx: Tx; steps: EscalationStep[] }) {
  return (
    <section className="mt-6" aria-labelledby="esc-path">
      <h3 id="esc-path" className="font-semibold">{tx.path}</h3>
      <ol className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        {steps.map((s, i) => (
          <li key={s.level} aria-current={s.current ? "step" : undefined}
            className={s.current ? "rounded-lg border-2 border-primary bg-primary-soft p-3" : "rounded-lg border border-border p-3"}>
            <p className="text-xs font-semibold uppercase tracking-wide text-primary">{i + 1}. {s.label}</p>
            {s.name !== s.label && <p className="mt-1 text-sm font-medium">{s.name}</p>}
            <p className="break-words text-xs text-fg-muted">{s.contact}</p>
            {s.tat && <p className="mt-1 text-xs text-fg-muted">{s.tat}</p>}
          </li>
        ))}
      </ol>
    </section>
  );
}

// ------------------------------------------------------------------ track
type TrackValues = { ticket_id: string; mobile: string; otp: string };

function TrackComplaint({ locale, tx, prefill }: { locale: Locale; tx: Tx; prefill: { ticket: string; mobile: string } | null }) {
  const [view, setView] = useState<ComplaintView | null>(null);
  const [mobileUsed, setMobileUsed] = useState("");
  const form = useForm<TrackValues>({
    initial: { ticket_id: prefill?.ticket ?? "", mobile: prefill?.mobile ?? "", otp: "" },
    validate: (v) => {
      const e: Partial<Record<keyof TrackValues, string>> = {};
      if (!/^CMP-\d{8}-[A-Z0-9]{6}$/i.test(v.ticket_id.trim())) e.ticket_id = tx.errTicket;
      if (!isMobile(v.mobile)) e.mobile = tx.errMobile;
      if (!isOtp(v.otp)) e.otp = tx.errOtp;
      return e;
    },
  });

  const submit = form.handleSubmit(async (v) => {
    const r = await api<ComplaintView>("/complaints/track", {
      method: "POST",
      auth: false,
      body: { ticket_id: v.ticket_id.trim().toUpperCase(), mobile: v.mobile.trim(), otp: v.otp.trim() },
    });
    setMobileUsed(v.mobile.trim());
    setView(r);
  });

  if (view) {
    return <TrackResult locale={locale} tx={tx} view={view} mobile={mobileUsed} onChange={setView}
      onReset={() => { setView(null); form.reset({ ticket_id: "", mobile: "", otp: "" }); }} />;
  }

  return (
    <Card as="section">
      <form noValidate onSubmit={submit} className="space-y-4">
        {form.formError && <Alert tone="danger" title={tx.trackFailed}>{form.formError}</Alert>}
        <TextField id="ticket_id" label={tx.ticketId} required autoComplete="off" maxLength={20} placeholder="CMP-20261012-7KQ2ZD"
          value={form.values.ticket_id} onChange={(v) => form.set("ticket_id", v.toUpperCase())} onBlur={() => form.blur("ticket_id")}
          error={form.errors.ticket_id} />
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-2">
            <TextField id="mobile" label={tx.mobile} required type="tel" inputMode="tel" autoComplete="tel" maxLength={16}
              value={form.values.mobile} onChange={(v) => form.set("mobile", v)} onBlur={() => form.blur("mobile")} error={form.errors.mobile} />
            <OtpRequest mobile={form.values.mobile} purpose="complaint_track" locale={locale} />
          </div>
          <TextField id="otp" label={tx.otp} required inputMode="numeric" autoComplete="one-time-code" maxLength={6} hint={tx.otpHint}
            value={form.values.otp} onChange={(v) => form.set("otp", v.replace(/\D/g, "").slice(0, 6))} onBlur={() => form.blur("otp")}
            error={form.errors.otp} />
        </div>
        <Button type="submit" loading={form.submitting}>{tx.track}</Button>
      </form>
    </Card>
  );
}

type Action = "escalate" | "reopen" | "refer_rbi_ombudsman";

function TrackResult({ locale, tx, view, mobile, onChange, onReset }: {
  locale: Locale; tx: Tx; view: ComplaintView; mobile: string; onChange: (v: ComplaintView) => void; onReset: () => void;
}) {
  const [action, setAction] = useState<Action | null>(null);
  const [reason, setReason] = useState("");
  const [reasonError, setReasonError] = useState<string | null>(null);
  const [otp, setOtp] = useState("");
  const [needOtp, setNeedOtp] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [token, setToken] = useState<string | undefined>(view.track_token);

  const run = async (a: Action) => {
    setError(null);
    setNotice(null);
    if (a !== "refer_rbi_ombudsman" && reason.trim().length < 10) {
      setReasonError(tx.errReason);
      document.getElementById("action-reason")?.focus();
      return;
    }
    if (needOtp && !isOtp(otp)) {
      setError(tx.errOtp);
      return;
    }
    setBusy(true);
    try {
      const r = await api<ComplaintView>("/complaints/escalate", {
        method: "POST",
        auth: false,
        body: { ticket_id: view.ticket_id, mobile, action: a, reason: reason.trim() || null, track_token: needOtp ? null : token, otp: needOtp ? otp : null },
      });
      onChange({ ...r, track_token: token });
      setNotice(r.message ?? null);
      setAction(null);
      setReason("");
      setOtp("");
      setNeedOtp(false);
      if (a === "refer_rbi_ombudsman") window.open(view.rbi_cms_url, "_blank", "noopener,noreferrer");
    } catch (e) {
      if (e instanceof ApiError && e.fieldErrors.otp) {
        setNeedOtp(true);
        setToken(undefined);
        setError(tx.otpAgain);
      } else if (e instanceof ApiError && e.fieldErrors.reason) {
        setReasonError(e.fieldErrors.reason);
      } else {
        setError(e instanceof ApiError ? e.message : tx.actionFailed);
      }
    } finally {
      setBusy(false);
    }
  };

  const actionForm = (a: Action, label: string) => (
    <div className="mt-3 space-y-3 rounded-lg border border-border bg-surface-2 p-3">
      {a !== "refer_rbi_ombudsman" && (
        <TextAreaField id="action-reason" label={tx.reason} required rows={3} maxLength={1000} value={reason} hint={tx.reasonHint}
          onChange={(v) => { setReason(v); setReasonError(null); }} error={reasonError ?? undefined} />
      )}
      {needOtp && (
        <div className="grid gap-3 sm:grid-cols-2">
          <OtpRequest mobile={mobile} purpose="complaint_track" locale={locale} />
          <TextField id="action-otp" label={tx.otp} required inputMode="numeric" maxLength={6} value={otp}
            onChange={(v) => setOtp(v.replace(/\D/g, "").slice(0, 6))} />
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        <Button onClick={() => void run(a)} loading={busy}>{label}</Button>
        <Button variant="secondary" onClick={() => { setAction(null); setError(null); setReasonError(null); }} disabled={busy}>{tx.cancel}</Button>
      </div>
    </div>
  );

  return (
    <div className="space-y-4">
      <Card as="section">
        <CardHeader title={<span className="font-mono">{view.ticket_id}</span>}
          actions={<Badge tone={statusTone(view.status)}>{view.status_label}</Badge>} />
        <KeyValue items={[
          { label: tx.category, value: tx.cats[view.category] ?? view.category },
          { label: tx.level, value: view.current_level_label },
          { label: tx.filed, value: formatDateTime(view.created_at) },
          { label: tx.tatDue, value: <span>{formatDate(view.tat_due_at)} {view.tat_breached && <Badge tone="danger">{tx.tatBreached}</Badge>}</span> },
        ]} />
        {view.resolution_note && (
          <div className="mt-4">
            <Alert tone="success" title={tx.resolution}>{view.resolution_note}</Alert>
          </div>
        )}
        <div aria-live="polite" className="mt-3">
          {notice && <Alert tone="success">{notice}</Alert>}
          {error && <Alert tone="danger" title={tx.actionFailed}>{error}</Alert>}
        </div>
      </Card>

      <Card as="section">
        <CardHeader title={tx.timeline} level={3} />
        <ol className="relative space-y-4 border-l-2 border-border pl-5">
          {view.timeline.map((ev) => (
            <li key={ev.id} className="relative">
              <span aria-hidden="true" className="absolute -left-[1.6rem] top-1 h-3 w-3 rounded-full border-2 border-primary bg-surface" />
              <p className="text-sm font-semibold">
                {ev.status_label}
                {ev.level_label && <span className="font-normal text-fg-muted"> · {ev.level_label}</span>}
              </p>
              {ev.note && <p className="text-sm text-fg-muted">{ev.note}</p>}
              <p className="text-xs text-fg-subtle">{formatDateTime(ev.occurred_at)}</p>
            </li>
          ))}
        </ol>
      </Card>

      {(view.can_escalate || view.can_reopen) && (
        <Card as="section">
          {view.can_escalate && view.next_escalation && view.next_escalation.level !== "rbi_ombudsman" && (
            <div>
              <CardHeader level={3} title={`${tx.escalateTo} ${view.next_escalation.label}`} description={view.next_escalation.contact} />
              {action === "escalate" ? actionForm("escalate", tx.confirm) : (
                <Button variant="outline" onClick={() => { setAction("escalate"); setError(null); }}>{tx.escalate}</Button>
              )}
            </div>
          )}
          {view.can_reopen && (
            <div>
              <CardHeader level={3} title={tx.reopen} description={tx.reopenHint} />
              {action === "reopen" ? actionForm("reopen", tx.confirm) : (
                <Button variant="outline" onClick={() => { setAction("reopen"); setError(null); }}>{tx.reopen}</Button>
              )}
            </div>
          )}
        </Card>
      )}

      <Card as="section">
        <CardHeader level={3} title={tx.rbiTitle} />
        {view.rbi_ombudsman_eligible ? (
          <div className="space-y-3">
            <p className="text-sm">{tx.rbiEligible}</p>
            <div className="flex flex-wrap gap-2">
              <a href={view.rbi_cms_url} target="_blank" rel="noopener noreferrer"
                className="inline-flex min-h-tap items-center rounded-md bg-primary px-4 text-sm font-semibold text-primary-fg hover:bg-primary-hover">
                {tx.rbiOpen}<span className="sr-only"> (opens in a new tab)</span>
              </a>
              {action !== "refer_rbi_ombudsman" && (
                <Button variant="outline" onClick={() => { setAction("refer_rbi_ombudsman"); setError(null); }}>{tx.rbiRecord}</Button>
              )}
            </div>
            {action === "refer_rbi_ombudsman" && actionForm("refer_rbi_ombudsman", tx.confirm)}
          </div>
        ) : (
          <p className="text-sm text-fg-muted">
            {view.status === "referred_rbi_ombudsman" ? view.status_label : <>{tx.rbiNotYet} <strong>{formatDate(view.rbi_ombudsman_eligible_at)}</strong>. </>}{" "}
            <TextLink href={view.rbi_cms_url} external>cms.rbi.org.in</TextLink>
          </p>
        )}
      </Card>

      <EscalationPath tx={tx} steps={view.escalation_path} />
      <Button variant="secondary" onClick={onReset}>{tx.trackAnother}</Button>
    </div>
  );
}

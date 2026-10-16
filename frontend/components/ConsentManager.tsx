"use client";

/**
 * DPDP consent manager for the public site.
 *
 * - First visit: a non-blocking bottom sheet (role="dialog", aria-modal="false") in the visitor's
 *   language lists each purpose with a switch. Essential is fixed on; analytics, marketing and
 *   personalisation are OFF until the visitor opts in.
 * - The visitor gets an anonymous id in the first-party cookie `asr_vid` (SameSite=Lax, 1 year);
 *   every choice is recorded server-side against the published notice version (POST /consents).
 * - The "Privacy choices" button (bottom-left) reopens the sheet. Withdrawal takes two clicks:
 *   open the sheet, switch a purpose off (saved at once through POST /consents/{id}/withdraw).
 * - The choice is mirrored in localStorage `asr.consent` and announced with a `consent:changed`
 *   window event so any optional script can wait for consent via `hasConsent(purpose)`.
 *
 * NOTE: this app loads no analytics, marketing or personalisation scripts at all. If one is ever
 * added it must be gated with `hasConsent("analytics")` (etc.) and listen for `consent:changed`.
 */
import Link from "next/link";
import { KeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { Locale, lp, t } from "@/lib/i18n";
import { Button, Spinner, Switch, cx } from "./ui";

export type ConsentPurpose = "essential" | "analytics" | "marketing" | "personalisation";
type Optional3 = Exclude<ConsentPurpose, "essential">;
type Choices = Record<Optional3, boolean>;

/** Shape of GET /public/consent-notice/{locale}. */
export type ConsentNotice = {
  locale: Locale;
  version_id: string;
  published_at: string | null;
  needs_compliance_verification?: boolean;
  title: string;
  summary: string;
  purposes: Array<{ purpose: ConsentPurpose; label: string; description: string; required?: boolean; default?: boolean }>;
  rights: string[];
  grievance_officer: { title?: string; name?: string; email?: string; phone?: string; address?: string; response_time?: string };
  retention?: string;
  withdrawal?: string;
  full_notice_href?: string;
  labels?: Record<string, string>;
};

type Stored = { subject_id: string; version_id: string | null; choices: Choices; recorded_at: string };

const STORAGE_KEY = "asr.consent";
const COOKIE = "asr_vid";
const OPTIONAL: Optional3[] = ["analytics", "marketing", "personalisation"];
const NONE: Choices = { analytics: false, marketing: false, personalisation: false };
const ALL: Choices = { analytics: true, marketing: true, personalisation: true };

const TX = {
  en: {
    title: "Your privacy choices",
    loading: "Loading the privacy notice…",
    loadError: "We couldn't load the privacy notice.",
    retry: "Try again",
    acceptAll: "Accept all",
    rejectAll: "Reject all",
    save: "Save choices",
    fullNotice: "Read the full privacy notice",
    alwaysOn: "Always on",
    close: "Close privacy choices",
    saved: "Your choices are saved.",
    withdrawn: "Consent withdrawn. It takes effect immediately.",
    saveError: "We couldn't save your choices. Please try again.",
    lastSaved: "Last saved",
    rights: "Your rights",
    grievance: "Grievance Officer",
    retention: "Retention",
  },
  hi: {
    title: "आपकी गोपनीयता के विकल्प",
    loading: "गोपनीयता सूचना लोड हो रही है…",
    loadError: "गोपनीयता सूचना लोड नहीं हो सकी।",
    retry: "फिर से कोशिश करें",
    acceptAll: "सभी स्वीकार करें",
    rejectAll: "सभी अस्वीकार करें",
    save: "विकल्प सहेजें",
    fullNotice: "पूरी गोपनीयता सूचना पढ़ें",
    alwaysOn: "हमेशा चालू",
    close: "गोपनीयता विकल्प बंद करें",
    saved: "आपके विकल्प सहेज लिए गए हैं।",
    withdrawn: "सहमति वापस ले ली गई। यह तुरंत लागू है।",
    saveError: "आपके विकल्प सहेजे नहीं जा सके। कृपया फिर से कोशिश करें।",
    lastSaved: "अंतिम बार सहेजा गया",
    rights: "आपके अधिकार",
    grievance: "शिकायत अधिकारी",
    retention: "डेटा अवधि",
  },
} as const;

// ------------------------------------------------------------------ storage helpers
function readStored(): Stored | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const v = JSON.parse(raw) as Stored;
    if (!v || typeof v !== "object" || !v.choices) return null;
    return v;
  } catch {
    return null;
  }
}

function writeStored(v: Stored) {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(v));
  } catch {
    /* storage unavailable (private mode): the server record still holds the choice */
  }
}

/** True only when the visitor has opted in to `purpose` (essential is always true). Safe on the server. */
export function hasConsent(purpose: ConsentPurpose): boolean {
  if (purpose === "essential") return true;
  if (typeof window === "undefined") return false;
  const s = readStored();
  return !!s?.choices?.[purpose];
}

function readCookie(name: string): string | null {
  try {
    const m = document.cookie.split("; ").find((c) => c.startsWith(`${name}=`));
    return m ? decodeURIComponent(m.split("=")[1]) : null;
  } catch {
    return null;
  }
}

/** Anonymous visitor id, created once and kept in a first-party cookie for one year. */
export function getVisitorId(): string {
  const existing = readCookie(COOKIE);
  if (existing && /^[A-Za-z0-9-]{8,64}$/.test(existing)) return existing;
  const id =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `v-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
  try {
    const secure = window.location.protocol === "https:" ? "; Secure" : "";
    document.cookie = `${COOKIE}=${encodeURIComponent(id)}; Max-Age=31536000; Path=/; SameSite=Lax${secure}`;
  } catch {
    /* cookies blocked: the id lives for this page view only */
  }
  return id;
}

function announce(stored: Stored) {
  try {
    window.dispatchEvent(new CustomEvent("consent:changed", { detail: { ...stored.choices, essential: true, subject_id: stored.subject_id } }));
  } catch {
    /* very old browsers */
  }
}

// ------------------------------------------------------------------ component
export function ConsentManager({ locale }: { locale: Locale }) {
  const tx = TX[locale] ?? TX.en;
  const [mounted, setMounted] = useState(false);
  const [open, setOpen] = useState(false);
  const [visitorId, setVisitorId] = useState<string>("");
  const [stored, setStored] = useState<Stored | null>(null);
  const [choices, setChoices] = useState<Choices>(NONE);
  const [notice, setNotice] = useState<ConsentNotice | null>(null);
  const [noticeLoading, setNoticeLoading] = useState(false);
  const [noticeError, setNoticeError] = useState<string | null>(null);
  const [saving, setSaving] = useState<"accept" | "reject" | "save" | Optional3 | null>(null);
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const sheetRef = useRef<HTMLDivElement>(null);
  const openerRef = useRef<HTMLButtonElement>(null);

  // Visitor id + first-visit detection.
  useEffect(() => {
    setMounted(true);
    const vid = getVisitorId();
    setVisitorId(vid);
    const s = readStored();
    if (s && s.subject_id === vid) {
      setStored(s);
      setChoices(s.choices);
      return;
    }
    // No local record (new device, cleared storage): ask the server before prompting again.
    let alive = true;
    api<{ has_choices: boolean; state: Record<string, boolean>; recorded_at: string | null; notice_version_id: string | null }>(
      `/consents/${encodeURIComponent(vid)}`,
      { auth: false },
    )
      .then((r) => {
        if (!alive) return;
        if (r.has_choices) {
          const c: Choices = { analytics: !!r.state.analytics, marketing: !!r.state.marketing, personalisation: !!r.state.personalisation };
          const next: Stored = { subject_id: vid, version_id: r.notice_version_id, choices: c, recorded_at: r.recorded_at ?? new Date().toISOString() };
          writeStored(next);
          setStored(next);
          setChoices(c);
          announce(next);
        } else {
          setOpen(true);
        }
      })
      .catch(() => alive && setOpen(true));
    return () => {
      alive = false;
    };
  }, []);

  const loadNotice = useCallback(async () => {
    setNoticeLoading(true);
    setNoticeError(null);
    try {
      const n = await api<ConsentNotice>(`/public/consent-notice/${locale}`, { auth: false });
      setNotice(n);
    } catch (e) {
      setNoticeError(e instanceof ApiError ? e.message : tx.loadError);
    } finally {
      setNoticeLoading(false);
    }
  }, [locale, tx.loadError]);

  // Load (or reload on language change) the notice whenever the sheet is open.
  useEffect(() => {
    if (open && (!notice || notice.locale !== locale)) void loadNotice();
  }, [open, locale, notice, loadNotice]);

  // Move focus into the sheet when it opens (it does not trap focus: the page stays usable).
  useEffect(() => {
    if (open) setTimeout(() => sheetRef.current?.focus(), 0);
  }, [open]);

  const close = useCallback(() => {
    setOpen(false);
    setMessage(null);
    setTimeout(() => openerRef.current?.focus(), 0);
  }, []);

  const persist = (c: Choices, recordedAt: string, versionId: string | null) => {
    const next: Stored = { subject_id: visitorId, version_id: versionId, choices: c, recorded_at: recordedAt };
    writeStored(next);
    setStored(next);
    setChoices(c);
    announce(next);
  };

  const save = async (c: Choices, kind: "accept" | "reject" | "save") => {
    if (!notice) {
      void loadNotice();
      return;
    }
    setSaving(kind);
    setMessage(null);
    try {
      const r = await api<{ recorded_at: string; notice_version_id: string }>("/consents", {
        method: "POST",
        auth: false,
        body: { subject_id: visitorId, notice_version_id: notice.version_id, language: locale, choices: { essential: true, ...c } },
      });
      persist(c, r.recorded_at, r.notice_version_id);
      setMessage({ tone: "ok", text: tx.saved });
      setTimeout(() => close(), 900);
    } catch (e) {
      setMessage({ tone: "error", text: e instanceof ApiError ? e.message : tx.saveError });
    } finally {
      setSaving(null);
    }
  };

  const toggle = async (purpose: Optional3, on: boolean) => {
    const wasGranted = !!stored?.choices?.[purpose];
    if (!on && wasGranted) {
      // Withdrawal is saved at once (no extra "Save" click needed).
      setSaving(purpose);
      setMessage(null);
      try {
        const r = await api<{ withdrawn_at: string }>(`/consents/${encodeURIComponent(visitorId)}/withdraw`, {
          method: "POST",
          auth: false,
          body: { purpose },
        });
        persist({ ...(stored?.choices ?? NONE), [purpose]: false }, r.withdrawn_at, stored?.version_id ?? null);
        setMessage({ tone: "ok", text: tx.withdrawn });
      } catch (e) {
        setMessage({ tone: "error", text: e instanceof ApiError ? e.message : tx.saveError });
      } finally {
        setSaving(null);
      }
      return;
    }
    setChoices((c) => ({ ...c, [purpose]: on }));
  };

  // Escape closes the sheet (it is non-modal, so this is a convenience, not a trap).
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      close();
    }
  };

  if (!mounted) return null;

  const labels = notice?.labels ?? {};
  const optionalPurposes = (notice?.purposes ?? []).filter((p) => p.purpose !== "essential");
  const essential = notice?.purposes.find((p) => p.purpose === "essential");
  const fullHref = lp(locale, notice?.full_notice_href || "/pages/privacy-notice");

  return (
    <>
      {!open && (
        <button
          ref={openerRef}
          type="button"
          onClick={() => setOpen(true)}
          aria-haspopup="dialog"
          className="fixed bottom-4 left-4 z-40 inline-flex min-h-tap items-center gap-2 rounded-pill border border-border bg-surface px-3 text-sm font-semibold text-fg shadow-md hover:bg-surface-2"
        >
          <span aria-hidden="true">⚿</span>
          <span className="hidden sm:inline">{t(locale, "privacyChoices")}</span>
          <span className="sr-only sm:hidden">{t(locale, "privacyChoices")}</span>
        </button>
      )}

      {open && (
        <div
          ref={sheetRef}
          role="dialog"
          aria-modal="false"
          aria-labelledby="consent-title"
          aria-describedby="consent-summary"
          tabIndex={-1}
          lang={locale}
          onKeyDown={onKeyDown}
          className="fixed inset-x-0 bottom-0 z-50 max-h-[85vh] overflow-y-auto rounded-t-xl border border-border bg-surface p-4 text-fg shadow-lg sm:inset-x-auto sm:bottom-4 sm:left-4 sm:w-[30rem] sm:rounded-xl sm:p-5"
        >
          <div className="mb-3 flex items-start justify-between gap-3">
            <h2 id="consent-title" className="text-lg font-semibold">
              {notice?.title || tx.title}
            </h2>
            <button
              type="button"
              onClick={close}
              aria-label={tx.close}
              className="inline-flex min-h-tap min-w-tap items-center justify-center rounded-md text-xl text-fg-muted hover:bg-surface-2 hover:text-fg"
            >
              ×
            </button>
          </div>

          {noticeLoading && !notice && (
            <div className="py-4">
              <Spinner label={tx.loading} />
            </div>
          )}

          {noticeError && !notice && (
            <div role="alert" className="space-y-3 rounded-lg border border-danger bg-danger-soft p-3">
              <p id="consent-summary" className="text-sm">{noticeError}</p>
              <div className="flex flex-wrap gap-2">
                <Button size="sm" variant="secondary" onClick={() => void loadNotice()}>
                  {tx.retry}
                </Button>
                <Link href={fullHref} className="inline-flex min-h-tap items-center text-sm font-semibold text-primary underline">
                  {tx.fullNotice}
                </Link>
              </div>
            </div>
          )}

          {notice && (
            <div className="space-y-4">
              <p id="consent-summary" className="text-sm text-fg-muted">
                {notice.summary}
              </p>

              <ul className="divide-y divide-border rounded-lg border border-border">
                {essential && (
                  <li className="p-3">
                    <Switch id="consent-essential" checked onChange={() => undefined} disabled
                      label={<span>{essential.label} <span className="text-xs font-normal text-fg-muted">({labels.always_on || tx.alwaysOn})</span></span>}
                      description={essential.description} />
                  </li>
                )}
                {optionalPurposes.map((p) => {
                  const key = p.purpose as Optional3;
                  if (!OPTIONAL.includes(key)) return null;
                  return (
                    <li key={key} className="p-3">
                      <Switch id={`consent-${key}`} checked={!!choices[key]} disabled={saving !== null}
                        onChange={(v) => void toggle(key, v)} label={p.label} description={p.description} />
                    </li>
                  );
                })}
              </ul>

              <details className="rounded-lg bg-surface-2 p-3 text-sm">
                <summary className="cursor-pointer font-medium">{tx.rights}</summary>
                <ul className="mt-2 list-disc space-y-1 pl-5 text-fg-muted">
                  {notice.rights.map((r) => (
                    <li key={r}>{r}</li>
                  ))}
                </ul>
                {notice.retention && (
                  <p className="mt-2 text-fg-muted">
                    <span className="font-medium text-fg">{tx.retention}: </span>
                    {notice.retention}
                  </p>
                )}
                {notice.grievance_officer?.email && (
                  <p className="mt-2 text-fg-muted">
                    <span className="font-medium text-fg">{notice.grievance_officer.title || tx.grievance}: </span>
                    {[notice.grievance_officer.name, notice.grievance_officer.email, notice.grievance_officer.phone].filter(Boolean).join(" · ")}
                  </p>
                )}
              </details>

              <div aria-live="polite" className="min-h-5 text-sm">
                {message && <p className={cx("font-medium", message.tone === "ok" ? "text-success" : "text-danger")}>{message.text}</p>}
                {!message && stored && (
                  <p className="text-fg-muted">
                    {tx.lastSaved}: {formatDateTime(stored.recorded_at)}
                  </p>
                )}
              </div>

              <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                <Button variant="secondary" onClick={() => void save(NONE, "reject")} loading={saving === "reject"} disabled={saving !== null}>
                  {labels.reject_all || tx.rejectAll}
                </Button>
                <Button variant="outline" onClick={() => void save(choices, "save")} loading={saving === "save"} disabled={saving !== null}>
                  {labels.save || tx.save}
                </Button>
                <Button onClick={() => void save(ALL, "accept")} loading={saving === "accept"} disabled={saving !== null}>
                  {labels.accept_all || tx.acceptAll}
                </Button>
              </div>

              <p className="text-sm">
                <Link href={fullHref} className="font-medium text-primary underline underline-offset-2 hover:no-underline">
                  {labels.full_notice || tx.fullNotice}
                </Link>
              </p>
            </div>
          )}
        </div>
      )}
    </>
  );
}

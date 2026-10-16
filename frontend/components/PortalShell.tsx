"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { ReactNode, useCallback, useEffect, useState } from "react";
import { api, clearToken, getToken } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import type { Locale } from "@/lib/i18n";
import { usePrefs } from "@/lib/prefs";
import { SessionProvider, useSession } from "@/lib/session";
import { AssistantWidget } from "./AssistantWidget";
import { MockBanner } from "./MockBanner";
import { SessionTimeout } from "./SessionTimeout";
import { StepUpProvider } from "./StepUp";
import { Logo, SeniorToggle, ThemeToggle } from "./Toggles";
import { Badge, Button, cx, LoadingState } from "./ui";

type NavItem = { href: string; en: string; hi: string; icon: string; types: Array<"retail" | "corporate"> };

const NAV: NavItem[] = [
  { href: "/portal", en: "Dashboard", hi: "डैशबोर्ड", icon: "⌂", types: ["retail"] },
  { href: "/portal/accounts", en: "Accounts", hi: "खाते", icon: "▤", types: ["retail"] },
  { href: "/portal/transfers", en: "Transfers", hi: "फ़ंड ट्रांसफ़र", icon: "⇄", types: ["retail"] },
  { href: "/portal/beneficiaries", en: "Beneficiaries", hi: "लाभार्थी", icon: "☺", types: ["retail"] },
  { href: "/portal/cards", en: "Cards", hi: "कार्ड", icon: "▭", types: ["retail"] },
  { href: "/portal/payments", en: "Bills & taxes", hi: "बिल और कर", icon: "₹", types: ["retail"] },
  { href: "/portal/deposits", en: "Deposits", hi: "जमा", icon: "◔", types: ["retail"] },
  { href: "/portal/cheques", en: "Cheques", hi: "चेक", icon: "✎", types: ["retail"] },
  { href: "/portal/loans", en: "Loans", hi: "ऋण", icon: "◇", types: ["retail"] },
  { href: "/portal/wealth", en: "Wealth", hi: "वेल्थ", icon: "◆", types: ["retail"] },
  { href: "/portal/nri", en: "NRI / LRS", hi: "एनआरआई / LRS", icon: "✈", types: ["retail"] },
  { href: "/portal/corporate", en: "Bulk payments", hi: "बल्क भुगतान", icon: "▦", types: ["corporate"] },
  { href: "/portal/service-requests", en: "Service requests", hi: "सेवा अनुरोध", icon: "☰", types: ["retail"] },
  { href: "/portal/complaints", en: "Complaints", hi: "शिकायतें", icon: "✉", types: ["retail"] },
  { href: "/portal/profile", en: "Profile", hi: "प्रोफ़ाइल", icon: "☻", types: ["retail", "corporate"] },
  { href: "/portal/privacy", en: "Privacy & accessibility", hi: "गोपनीयता और सुगम्यता", icon: "⚿", types: ["retail", "corporate"] },
];

type Notification = { id: string; title: string; body: string; triggered_at: string; read_at: string | null };

export function PortalShell({ children }: { children: ReactNode }) {
  return (
    <SessionProvider>
      <StepUpProvider>
        <PortalFrame>{children}</PortalFrame>
      </StepUpProvider>
    </SessionProvider>
  );
}

function PortalFrame({ children }: { children: ReactNode }) {
  const { session, loading, error, logout } = useSession();
  const { locale, setLocale, setSenior } = usePrefs();
  const router = useRouter();
  const pathname = usePathname() || "/portal";
  const [navOpen, setNavOpen] = useState(false);
  const [alertsOpen, setAlertsOpen] = useState(false);
  const [alerts, setAlerts] = useState<Notification[] | null>(null);
  const [mounted, setMounted] = useState(false);

  useEffect(() => setMounted(true), []);
  useEffect(() => setNavOpen(false), [pathname]);

  const toLogin = useCallback(
    (reason?: string) => {
      clearToken();
      const next = encodeURIComponent(pathname);
      router.replace(`/${locale}/login?next=${next}${reason ? `&reason=${reason}` : ""}`);
    },
    [pathname, router, locale],
  );

  // Guard: signed in, MFA complete, right user type.
  useEffect(() => {
    if (!mounted || loading) return;
    if (!getToken() || (!session && error)) {
      toLogin(error?.code === "session_superseded" ? "superseded" : error ? "expired" : undefined);
      return;
    }
    if (!session) return;
    if (session.user_type === "staff") {
      router.replace("/staff");
      return;
    }
    if (session.mfa_level !== "mfa_complete") {
      router.replace(`/${locale}/login?step=mfa&next=${encodeURIComponent(pathname)}`);
      return;
    }
    if (session.user_type === "corporate" && !pathname.startsWith("/portal/corporate") && !pathname.startsWith("/portal/profile") && !pathname.startsWith("/portal/privacy")) {
      router.replace("/portal/corporate");
    }
  }, [mounted, loading, session, error, toLogin, router, locale, pathname]);

  // Apply the customer's saved preferences once.
  useEffect(() => {
    if (!session) return;
    if (session.preferred_language === "hi" || session.preferred_language === "en") setLocale(session.preferred_language);
    if (session.senior_mode) setSenior(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session?.session_id]);

  useEffect(() => {
    const onExpired = (e: Event) => {
      const code = (e as CustomEvent<{ code?: string }>).detail?.code;
      toLogin(code === "session_superseded" ? "superseded" : "expired");
    };
    window.addEventListener("auth:expired", onExpired);
    return () => window.removeEventListener("auth:expired", onExpired);
  }, [toLogin]);

  const doLogout = useCallback(async () => {
    await logout();
    router.replace(`/${locale}/login?reason=logged_out`);
  }, [logout, router, locale]);

  const savePrefs = (body: { preferred_language?: string; senior_mode?: boolean }) => {
    api("/me/preferences", { method: "PUT", body }).catch(() => undefined);
  };

  const openAlerts = async () => {
    setAlertsOpen((o) => !o);
    if (!alerts) {
      try {
        const r = await api<{ items: Notification[] }>("/me/notifications?limit=6");
        setAlerts(r.items.slice(0, 6));
      } catch {
        setAlerts([]);
      }
    }
  };

  if (!mounted || loading || !session || session.mfa_level !== "mfa_complete" || session.user_type === "staff") {
    return (
      <div className="page-container py-16">
        <LoadingState label="Checking your secure session…" rows={4} />
      </div>
    );
  }

  const L: Locale = locale;
  const items = NAV.filter((n) => n.types.includes(session.user_type as "retail" | "corporate"));
  const isActive = (href: string) => (href === "/portal" ? pathname === "/portal" : pathname.startsWith(href));

  const nav = (
    <nav aria-label="Internet banking">
      <ul className="space-y-0.5">
        {items.map((n) => (
          <li key={n.href}>
            <Link href={n.href} aria-current={isActive(n.href) ? "page" : undefined}
              className={cx("flex min-h-tap items-center gap-3 rounded-md px-3 text-sm font-medium",
                isActive(n.href) ? "bg-primary-soft text-primary" : "text-fg hover:bg-surface-2")}>
              <span aria-hidden="true" className="w-5 text-center">{n.icon}</span>
              {n[L]}
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );

  return (
    <div lang={L} className="flex min-h-screen flex-col">
      <a href="#main" className="skip-link">Skip to main content</a>
      <header className="sticky top-0 z-40 border-b border-border bg-surface">
        <div className="flex min-h-16 items-center gap-2 px-3 sm:px-5">
          <button type="button" className="inline-flex min-h-tap min-w-tap items-center justify-center rounded-md hover:bg-surface-2 lg:hidden"
            aria-expanded={navOpen} aria-controls="portal-nav" onClick={() => setNavOpen((o) => !o)}>
            <span className="sr-only">{navOpen ? "Close menu" : "Open menu"}</span>
            <svg aria-hidden="true" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              {navOpen ? <path d="M6 6l12 12M18 6 6 18" /> : <path d="M4 7h16M4 12h16M4 17h16" />}
            </svg>
          </button>
          <Link href={session.user_type === "corporate" ? "/portal/corporate" : "/portal"} className="rounded-md" aria-label="Internet banking home">
            <Logo locale={L} />
          </Link>
          <div className="ml-auto flex items-center gap-1">
            <button type="button" className="inline-flex min-h-tap items-center rounded-md px-2 text-sm font-semibold hover:bg-surface-2"
              onClick={() => {
                const next: Locale = L === "en" ? "hi" : "en";
                setLocale(next);
                savePrefs({ preferred_language: next });
              }}
              aria-label={L === "en" ? "हिन्दी में देखें" : "View in English"}>
              {L === "en" ? "हिं" : "EN"}
            </button>
            <SeniorToggle locale={L} onChange={(on) => savePrefs({ senior_mode: on })} />
            <ThemeToggle locale={L} />
            {session.user_type === "retail" && (
              <div className="relative">
                <button type="button" onClick={openAlerts} aria-expanded={alertsOpen} aria-controls="alerts-panel"
                  className="inline-flex min-h-tap min-w-tap items-center justify-center rounded-md hover:bg-surface-2">
                  <span aria-hidden="true" className="text-lg">🔔</span>
                  <span className="sr-only">Alerts</span>
                </button>
                {alertsOpen && (
                  <div id="alerts-panel" className="absolute right-0 top-full z-50 mt-2 w-[min(22rem,calc(100vw-1.5rem))] rounded-lg border border-border bg-surface p-3 shadow-lg">
                    <p className="mb-2 font-semibold">Recent alerts</p>
                    {!alerts && <LoadingState rows={2} />}
                    {alerts && alerts.length === 0 && <p className="text-sm text-fg-muted">No alerts yet.</p>}
                    <ul className="space-y-2">
                      {alerts?.map((a) => (
                        <li key={a.id} className="rounded-md bg-surface-2 p-2 text-sm">
                          <p className="font-medium">{a.title}</p>
                          <p className="text-fg-muted">{a.body}</p>
                          <p className="text-xs text-fg-subtle">{formatDateTime(a.triggered_at)}</p>
                        </li>
                      ))}
                    </ul>
                    <Link href="/portal/service-requests#alerts" className="mt-2 inline-flex min-h-tap items-center text-sm font-semibold text-primary hover:underline">
                      View all alerts
                    </Link>
                  </div>
                )}
              </div>
            )}
            <Button variant="secondary" size="sm" onClick={doLogout}>{L === "hi" ? "लॉग आउट" : "Log out"}</Button>
          </div>
        </div>
      </header>
      <MockBanner locale={L} />

      <div className="flex flex-1">
        <aside id="portal-nav"
          className={cx("w-64 shrink-0 border-r border-border bg-surface p-3", navOpen ? "fixed inset-y-0 left-0 z-50 overflow-y-auto pt-20 shadow-lg" : "hidden", "lg:static lg:block lg:shadow-none lg:pt-3")}>
          <div className="mb-3 rounded-md bg-surface-2 p-3 text-sm">
            <p className="font-semibold text-fg">{session.display_name}</p>
            <p className="text-xs text-fg-muted">
              {session.user_type === "corporate" ? <Badge tone="primary">{session.corporate_role ?? "corporate"}</Badge> : session.email_masked}
            </p>
            {session.last_login_at && <p className="mt-1 text-xs text-fg-muted">Last login {formatDateTime(session.last_login_at)}</p>}
          </div>
          {nav}
        </aside>
        {navOpen && <button type="button" aria-label="Close menu" className="fixed inset-0 z-40 bg-overlay lg:hidden" onClick={() => setNavOpen(false)} />}
        <main id="main" tabIndex={-1} className="min-w-0 flex-1 px-4 py-6 sm:px-6 lg:px-8">
          <div className="mx-auto max-w-6xl">{children}</div>
        </main>
      </div>
      <footer className="border-t border-border bg-surface px-4 py-4 text-xs text-fg-muted sm:px-6">
        <div className="mx-auto flex max-w-6xl flex-wrap justify-between gap-2">
          <span>Never share your OTP, PIN, CVV or password. The bank never asks for them. Cyber fraud: call 1930 or visit cybercrime.gov.in.</span>
          <Link href={`/${L}/report-fraud`} className="font-semibold text-danger hover:underline">Report fraud</Link>
        </div>
      </footer>
      <SessionTimeout idleSeconds={session.idle_timeout_seconds} warningSeconds={session.warning_seconds}
        onExpire={() => toLogin("expired")} onLogout={doLogout} />
      <AssistantWidget locale={L} authenticated />
    </div>
  );
}

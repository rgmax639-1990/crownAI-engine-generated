"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { ReactNode, useCallback, useEffect, useState } from "react";
import { clearToken, getToken } from "@/lib/api";
import { formatDateTime, humanize } from "@/lib/format";
import { SessionProvider, useSession } from "@/lib/session";
import { MockBanner } from "./MockBanner";
import { SessionTimeout } from "./SessionTimeout";
import { StepUpProvider } from "./StepUp";
import { Logo, SeniorToggle, ThemeToggle } from "./Toggles";
import { Alert, Badge, Button, cx, LoadingState } from "./ui";

/** Staff console navigation. `roles` empty = any staff user. */
export const STAFF_NAV: Array<{ href: string; label: string; icon: string; roles: string[]; description: string }> = [
  { href: "/staff", label: "Overview", icon: "⌂", roles: [], description: "Your queues and shortcuts" },
  { href: "/staff/cms", label: "Content (CMS)", icon: "✎", roles: ["content_maker", "content_checker", "legal_approver", "compliance_reviewer"], description: "Create and edit pages, rates, notices and disclosures" },
  { href: "/staff/cms/approvals", label: "Approval queue", icon: "✓", roles: ["content_checker", "legal_approver"], description: "Checker and legal sign-off of submitted versions" },
  { href: "/staff/grievance", label: "Grievances", icon: "✉", roles: ["grievance_officer", "nodal_officer", "principal_nodal_officer", "internal_ombudsman"], description: "Complaint queue with TAT countdown and escalation" },
  { href: "/staff/vcip", label: "V-CIP console", icon: "◉", roles: ["vcip_officer"], description: "Video KYC queue and decisions" },
  { href: "/staff/reconciliation", label: "Reconciliation", icon: "⇄", roles: ["ops_admin"], description: "Payment mismatches between rails and Finacle" },
  { href: "/staff/integrations", label: "Integrations", icon: "⚙", roles: ["ops_admin", "integration_approver"], description: "Mock / live adapters and approvals" },
  { href: "/staff/soc", label: "Audit & incidents", icon: "⚑", roles: ["soc_analyst", "ops_admin"], description: "Immutable audit log and CERT-In countdown" },
  { href: "/staff/evidence", label: "Evidence register", icon: "▤", roles: ["compliance_reviewer", "ops_admin", "soc_analyst"], description: "Executed load tests, DR drills, VAPT and test runs" },
  { href: "/staff/maintenance", label: "Maintenance notices", icon: "◷", roles: ["ops_admin"], description: "Planned downtime (at least 48 hours ahead)" },
  { href: "/staff/users", label: "Staff users", icon: "☺", roles: ["ops_admin"], description: "Staff accounts and roles" },
];

export function staffCanSee(roles: string[], item: { roles: string[] }): boolean {
  return item.roles.length === 0 || item.roles.some((r) => roles.includes(r));
}

export function StaffShell({ children }: { children: ReactNode }) {
  return (
    <SessionProvider>
      <StepUpProvider>
        <StaffFrame>{children}</StaffFrame>
      </StepUpProvider>
    </SessionProvider>
  );
}

function StaffFrame({ children }: { children: ReactNode }) {
  const { session, loading, error, logout } = useSession();
  const router = useRouter();
  const pathname = usePathname() || "/staff";
  const [mounted, setMounted] = useState(false);
  const [navOpen, setNavOpen] = useState(false);

  useEffect(() => setMounted(true), []);
  useEffect(() => setNavOpen(false), [pathname]);

  const toLogin = useCallback(
    (reason?: string) => {
      clearToken();
      router.replace(`/en/login?next=${encodeURIComponent(pathname)}${reason ? `&reason=${reason}` : ""}`);
    },
    [pathname, router],
  );

  useEffect(() => {
    if (!mounted || loading) return;
    if (!getToken() || (!session && error)) {
      toLogin(error?.code === "session_superseded" ? "superseded" : error ? "expired" : undefined);
      return;
    }
    if (!session) return;
    if (session.mfa_level !== "mfa_complete") {
      router.replace(`/en/login?step=mfa&next=${encodeURIComponent(pathname)}`);
      return;
    }
    if (session.user_type !== "staff") router.replace(session.user_type === "corporate" ? "/portal/corporate" : "/portal");
  }, [mounted, loading, session, error, toLogin, router, pathname]);

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
    router.replace("/en/login?reason=logged_out");
  }, [logout, router]);

  if (!mounted || loading || !session || session.mfa_level !== "mfa_complete" || session.user_type !== "staff") {
    return (
      <div className="page-container py-16">
        <LoadingState label="Checking your staff session…" rows={4} />
      </div>
    );
  }

  const items = STAFF_NAV.filter((n) => staffCanSee(session.staff_roles, n));
  // Most specific match wins (so /staff/cms/approvals does not also highlight /staff/cms).
  const active = items
    .filter((n) => (n.href === "/staff" ? pathname === "/staff" : pathname === n.href || pathname.startsWith(n.href + "/")))
    .sort((a, b) => b.href.length - a.href.length)[0]?.href;
  const current = STAFF_NAV.filter((n) => n.href !== "/staff" && (pathname === n.href || pathname.startsWith(n.href + "/")))
    .sort((a, b) => b.href.length - a.href.length)[0];
  const allowed = !current || staffCanSee(session.staff_roles, current);

  return (
    <div className="flex min-h-screen flex-col">
      <a href="#main" className="skip-link">Skip to main content</a>
      <header className="sticky top-0 z-40 border-b border-border bg-surface">
        <div className="flex min-h-16 items-center gap-2 px-3 sm:px-5">
          <button type="button" className="inline-flex min-h-tap min-w-tap items-center justify-center rounded-md hover:bg-surface-2 lg:hidden"
            aria-expanded={navOpen} aria-controls="staff-nav" onClick={() => setNavOpen((o) => !o)}>
            <span className="sr-only">{navOpen ? "Close menu" : "Open menu"}</span>
            <svg aria-hidden="true" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              {navOpen ? <path d="M6 6l12 12M18 6 6 18" /> : <path d="M4 7h16M4 12h16M4 17h16" />}
            </svg>
          </button>
          <Link href="/staff" className="flex items-center gap-2 rounded-md" aria-label="Staff console home">
            <Logo compact />
            <span className="leading-tight">
              <span className="block font-bold text-fg">Staff console</span>
              <span className="hidden text-xs text-fg-muted sm:block">Restricted · MFA · IP allow-listed in production</span>
            </span>
          </Link>
          <div className="ml-auto flex items-center gap-1">
            <SeniorToggle />
            <ThemeToggle />
            <Button variant="secondary" size="sm" onClick={doLogout}>Log out</Button>
          </div>
        </div>
      </header>
      <MockBanner />

      <div className="flex flex-1">
        <aside id="staff-nav"
          className={cx("w-64 shrink-0 border-r border-border bg-surface p-3", navOpen ? "fixed inset-y-0 left-0 z-50 overflow-y-auto pt-20 shadow-lg" : "hidden", "lg:static lg:block lg:pt-3 lg:shadow-none")}>
          <div className="mb-3 rounded-md bg-surface-2 p-3 text-sm">
            <p className="font-semibold text-fg">{session.display_name}</p>
            <ul className="mt-1 flex flex-wrap gap-1" aria-label="Your roles">
              {session.staff_roles.map((r) => (
                <li key={r}><Badge tone="primary">{humanize(r)}</Badge></li>
              ))}
            </ul>
            {session.last_login_at && <p className="mt-2 text-xs text-fg-muted">Last login {formatDateTime(session.last_login_at)}</p>}
          </div>
          <nav aria-label="Staff console">
            <ul className="space-y-0.5">
              {items.map((n) => (
                <li key={n.href}>
                  <Link href={n.href} aria-current={active === n.href ? "page" : undefined}
                    className={cx("flex min-h-tap items-center gap-3 rounded-md px-3 text-sm font-medium",
                      active === n.href ? "bg-primary-soft text-primary" : "text-fg hover:bg-surface-2")}>
                    <span aria-hidden="true" className="w-5 text-center">{n.icon}</span>
                    {n.label}
                  </Link>
                </li>
              ))}
            </ul>
          </nav>
        </aside>
        {navOpen && <button type="button" aria-label="Close menu" className="fixed inset-0 z-40 bg-overlay lg:hidden" onClick={() => setNavOpen(false)} />}
        <main id="main" tabIndex={-1} className="min-w-0 flex-1 px-4 py-6 sm:px-6 lg:px-8">
          <div className="mx-auto max-w-7xl">
            {allowed ? children : (
              <Alert tone="warning" title="You don't have access to this area">
                {current?.label} needs one of these roles: {current?.roles.map(humanize).join(", ")}. Ask an operations admin if you need access.
              </Alert>
            )}
          </div>
        </main>
      </div>
      <footer className="border-t border-border bg-surface px-4 py-3 text-xs text-fg-muted sm:px-6">
        Every action in this console is recorded in the immutable audit log. Synthetic data only outside production.
      </footer>
      <SessionTimeout idleSeconds={session.idle_timeout_seconds} warningSeconds={session.warning_seconds}
        onExpire={() => toLogin("expired")} onLogout={doLogout} />
    </div>
  );
}

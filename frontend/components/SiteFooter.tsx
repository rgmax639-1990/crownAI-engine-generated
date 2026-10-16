"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useApi } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { Locale, LOCALE_NAMES, LOCALES, lp, switchLocalePath, t } from "@/lib/i18n";
import { Logo } from "./Toggles";
import { Button, Skeleton } from "./ui";

/** Shape of GET /public/footer/{locale} (all content is CMS-managed). */
export type FooterLink = { label: string; href: string };
export type FooterData = {
  locale: Locale;
  version_id: string;
  published_at: string | null;
  sections: {
    dicgc: { title: string; text: string };
    regulatory_notice: { text: string };
    escalation_matrix: { title: string; intro?: string; levels: Array<{ level: string; name: string; contact: string; tat?: string }> };
    principal_nodal_officer: { title: string; name: string; designation?: string; email?: string; phone?: string; address?: string };
    rbi_ombudsman: { title: string; text: string; url: string };
    safe_banking: { title: string; text: string; links: FooterLink[] };
    link_groups: Array<{ title: string; links: FooterLink[] }>;
    copyright: string;
  };
};

function FLink({ link, locale }: { link: FooterLink; locale: Locale }) {
  const cls = "inline-flex min-h-9 items-center text-sm text-fg-muted underline-offset-2 hover:text-fg hover:underline";
  if (/^(https?:|tel:|mailto:)/.test(link.href)) {
    const ext = link.href.startsWith("http");
    return (
      <a href={link.href} className={cls} target={ext ? "_blank" : undefined} rel={ext ? "noopener noreferrer" : undefined}>
        {link.label}
        {ext && <span className="sr-only"> (opens in a new tab)</span>}
      </a>
    );
  }
  return (
    <Link href={lp(locale, link.href)} className={cls}>
      {link.label}
    </Link>
  );
}

export function SiteFooter({ locale }: { locale: Locale }) {
  const state = useApi<FooterData>(`/public/footer/${locale}`, { auth: false });
  const pathname = usePathname() || `/${locale}`;
  const s = state.data?.sections;

  return (
    <footer className="mt-12 border-t border-border bg-surface" aria-labelledby="footer-heading">
      <h2 id="footer-heading" className="sr-only">{t(locale, "footerRegulatory")}</h2>
      <div className="page-container py-10">
        {state.loading && !s && (
          <div className="grid gap-6 md:grid-cols-3" aria-hidden="true">
            <Skeleton className="h-32" />
            <Skeleton className="h-32" />
            <Skeleton className="h-32" />
          </div>
        )}
        {state.error && !s && (
          <div role="alert" className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-danger bg-danger-soft p-4">
            <p className="text-sm">{t(locale, "footerUnavailable")}</p>
            <Button size="sm" variant="secondary" onClick={state.reload}>{t(locale, "retry")}</Button>
          </div>
        )}
        {s && (
          <div className="space-y-8">
            <div className="grid gap-6 lg:grid-cols-3">
              <section className="rounded-lg border border-border bg-surface-2 p-4" aria-labelledby="ft-dicgc">
                <h3 id="ft-dicgc" className="font-semibold text-fg">{s.dicgc.title}</h3>
                <p className="mt-2 text-sm text-fg-muted">{s.dicgc.text}</p>
              </section>
              <section className="rounded-lg border border-border bg-surface-2 p-4" aria-labelledby="ft-safe">
                <h3 id="ft-safe" className="font-semibold text-fg">{s.safe_banking.title}</h3>
                <p className="mt-2 text-sm text-fg-muted">{s.safe_banking.text}</p>
                <ul className="mt-2 flex flex-wrap gap-x-4">
                  {s.safe_banking.links.map((l) => (
                    <li key={l.href}><FLink link={l} locale={locale} /></li>
                  ))}
                </ul>
              </section>
              <section className="rounded-lg border border-border bg-surface-2 p-4" aria-labelledby="ft-rbio">
                <h3 id="ft-rbio" className="font-semibold text-fg">{s.rbi_ombudsman.title}</h3>
                <p className="mt-2 text-sm text-fg-muted">{s.rbi_ombudsman.text}</p>
                <FLink link={{ label: s.rbi_ombudsman.url.replace(/^https?:\/\//, ""), href: s.rbi_ombudsman.url }} locale={locale} />
              </section>
            </div>

            <section aria-labelledby="ft-escalation">
              <h3 id="ft-escalation" className="font-semibold text-fg">{s.escalation_matrix.title}</h3>
              {s.escalation_matrix.intro && <p className="mt-1 text-sm text-fg-muted">{s.escalation_matrix.intro}</p>}
              <ol className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
                {s.escalation_matrix.levels.map((lv, i) => (
                  <li key={lv.level} className="rounded-lg border border-border p-3">
                    <p className="text-xs font-semibold uppercase tracking-wide text-primary">
                      {i + 1}. {lv.level}
                    </p>
                    <p className="mt-1 text-sm font-medium text-fg">{lv.name}</p>
                    <p className="break-words text-xs text-fg-muted">{lv.contact}</p>
                    {lv.tat && <p className="mt-1 text-xs text-fg-muted">{lv.tat}</p>}
                  </li>
                ))}
              </ol>
              <div className="mt-3 rounded-lg border border-border p-3 text-sm">
                <p className="font-semibold text-fg">{s.principal_nodal_officer.title}</p>
                <p className="text-fg-muted">
                  {[s.principal_nodal_officer.name, s.principal_nodal_officer.designation, s.principal_nodal_officer.email,
                    s.principal_nodal_officer.phone, s.principal_nodal_officer.address].filter(Boolean).join(" · ")}
                </p>
              </div>
            </section>

            <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
              {s.link_groups.map((g) => (
                <nav key={g.title} aria-label={g.title}>
                  <h3 className="text-sm font-semibold text-fg">{g.title}</h3>
                  <ul className="mt-2">
                    {g.links.map((l) => (
                      <li key={l.href + l.label}><FLink link={l} locale={locale} /></li>
                    ))}
                  </ul>
                </nav>
              ))}
            </div>
          </div>
        )}

        <div className="mt-10 flex flex-col gap-4 border-t border-border pt-6 md:flex-row md:items-center md:justify-between">
          <div className="space-y-1">
            <Logo locale={locale} />
            {s && <p className="max-w-2xl text-xs text-fg-muted">{s.regulatory_notice.text}</p>}
            {s && <p className="text-xs text-fg-muted">{s.copyright}{state.data?.published_at ? ` · Updated ${formatDate(state.data.published_at)}` : ""}</p>}
          </div>
          <nav aria-label={t(locale, "language")}>
            <ul className="flex gap-3 text-sm">
              {LOCALES.map((l) => (
                <li key={l}>
                  <Link href={switchLocalePath(pathname, l)} lang={l} hrefLang={l} aria-current={l === locale ? "true" : undefined}
                    className={l === locale ? "font-semibold text-fg" : "text-fg-muted hover:text-fg hover:underline"}>
                    {LOCALE_NAMES[l]}
                  </Link>
                </li>
              ))}
            </ul>
          </nav>
        </div>
      </div>
    </footer>
  );
}

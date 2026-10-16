"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { FormEvent, useEffect, useState } from "react";
import { Locale, lp, t } from "@/lib/i18n";
import { LanguageSwitch, Logo, SeniorToggle, ThemeToggle } from "./Toggles";
import { buttonClass, cx } from "./ui";

export function SiteHeader({ locale }: { locale: Locale }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const pathname = usePathname();
  const router = useRouter();

  useEffect(() => setOpen(false), [pathname]);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const nav = [
    { href: lp(locale, "/products?category=accounts"), label: t(locale, "navAccounts") },
    { href: lp(locale, "/products?category=deposits"), label: t(locale, "navDeposits") },
    { href: lp(locale, "/products?category=loans"), label: t(locale, "navLoans") },
    { href: lp(locale, "/products?category=cards"), label: t(locale, "navCards") },
    { href: lp(locale, "/products?category=nri"), label: t(locale, "navNri") },
    { href: lp(locale, "/products?category=wealth"), label: t(locale, "navWealth") },
  ];
  const secondary = [
    { href: lp(locale, "/rates"), label: t(locale, "navRates") },
    { href: lp(locale, "/tools"), label: t(locale, "navTools") },
    { href: lp(locale, "/branches"), label: t(locale, "navLocator") },
    { href: lp(locale, "/grievance"), label: t(locale, "navHelp") },
  ];

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (q.trim()) router.push(lp(locale, `/search?q=${encodeURIComponent(q.trim())}`));
  };

  return (
    <header className="sticky top-0 z-40 border-b border-border bg-surface/95 backdrop-blur">
      {/* Utility strip: fraud reporting always one tap away. */}
      <div className="bg-primary text-primary-fg">
        <div className="page-container flex min-h-9 flex-wrap items-center justify-between gap-x-4 gap-y-1 py-1 text-xs sm:text-sm">
          <Link href={lp(locale, "/report-fraud")} className="inline-flex min-h-tap items-center gap-2 rounded-md px-1 font-semibold underline-offset-2 hover:underline">
            <span aria-hidden="true" className="inline-flex h-5 w-5 items-center justify-center rounded-full bg-danger text-danger-fg">!</span>
            {t(locale, "reportFraud247")}
          </Link>
          <span className="hidden items-center gap-3 md:flex">
            <a href="tel:1930" className="hover:underline">Cyber fraud helpline 1930</a>
            <Link href={lp(locale, "/status")} className="hover:underline">{t(locale, "statusPage")}</Link>
          </span>
        </div>
      </div>

      <div className="page-container flex min-h-16 items-center gap-2 py-2">
        <Link href={lp(locale)} className="mr-2 shrink-0 rounded-md" aria-label={`${t(locale, "brand")} home`}>
          <Logo locale={locale} />
        </Link>

        <nav aria-label={t(locale, "mainNav")} className="hidden min-w-0 flex-1 lg:block">
          <ul className="flex items-center gap-1">
            {nav.map((n) => (
              <li key={n.href}>
                <Link href={n.href} className="inline-flex min-h-tap items-center rounded-md px-3 text-sm font-medium text-fg hover:bg-surface-2">
                  {n.label}
                </Link>
              </li>
            ))}
          </ul>
        </nav>

        <div className="ml-auto flex items-center gap-1">
          <form role="search" onSubmit={submit} className="hidden md:block">
            <label htmlFor="site-search" className="sr-only">{t(locale, "search")}</label>
            <input id="site-search" type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t(locale, "searchPlaceholder")}
              className="h-10 w-48 rounded-md border border-border-strong bg-surface px-3 text-sm text-fg placeholder:text-fg-subtle xl:w-64" />
          </form>
          <Link href={lp(locale, "/search")} className="inline-flex min-h-tap min-w-tap items-center justify-center rounded-md hover:bg-surface-2 md:hidden" aria-label={t(locale, "search")}>
            <svg aria-hidden="true" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
          </Link>
          <LanguageSwitch locale={locale} />
          <div className="hidden sm:flex">
            <SeniorToggle locale={locale} />
            <ThemeToggle locale={locale} />
          </div>
          <Link href={lp(locale, "/login")} className={buttonClass("ghost", "sm", false, "hidden sm:inline-flex")}>
            {t(locale, "login")}
          </Link>
          <Link href={lp(locale, "/open-account")} className={buttonClass("primary", "sm", false, "hidden sm:inline-flex")}>
            {t(locale, "openAccount")}
          </Link>
          <button type="button" className="inline-flex min-h-tap min-w-tap items-center justify-center rounded-md hover:bg-surface-2 lg:hidden"
            aria-expanded={open} aria-controls="mobile-menu" onClick={() => setOpen((o) => !o)}>
            <span className="sr-only">{open ? t(locale, "closeMenu") : t(locale, "menu")}</span>
            <svg aria-hidden="true" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              {open ? <path d="M6 6l12 12M18 6 6 18" /> : <path d="M4 7h16M4 12h16M4 17h16" />}
            </svg>
          </button>
        </div>
      </div>

      <div id="mobile-menu" hidden={!open} className={cx("border-t border-border bg-surface lg:hidden")}>
        <div className="page-container space-y-4 py-4">
          <form role="search" onSubmit={submit}>
            <label htmlFor="mobile-search" className="sr-only">{t(locale, "search")}</label>
            <input id="mobile-search" type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t(locale, "searchPlaceholder")}
              className="h-11 w-full rounded-md border border-border-strong bg-surface px-3 text-fg placeholder:text-fg-subtle" />
          </form>
          <nav aria-label={`${t(locale, "mainNav")} (mobile)`}>
            <ul className="grid grid-cols-2 gap-1">
              {[...nav, ...secondary].map((n) => (
                <li key={n.href}>
                  <Link href={n.href} className="flex min-h-tap items-center rounded-md px-3 font-medium text-fg hover:bg-surface-2">
                    {n.label}
                  </Link>
                </li>
              ))}
            </ul>
          </nav>
          <div className="flex flex-wrap items-center gap-2 border-t border-border pt-4">
            <SeniorToggle locale={locale} />
            <ThemeToggle locale={locale} />
          </div>
          <div className="grid grid-cols-2 gap-2">
            <Link href={lp(locale, "/login")} className={buttonClass("secondary", "md", true)}>{t(locale, "login")}</Link>
            <Link href={lp(locale, "/open-account")} className={buttonClass("primary", "md", true)}>{t(locale, "openAccount")}</Link>
          </div>
        </div>
      </div>

      <nav aria-label="Secondary" className="hidden border-t border-border lg:block">
        <ul className="page-container flex items-center gap-1 py-1 text-sm">
          {secondary.map((n) => (
            <li key={n.href}>
              <Link href={n.href} className="inline-flex min-h-9 items-center rounded-md px-3 text-fg-muted hover:bg-surface-2 hover:text-fg">
                {n.label}
              </Link>
            </li>
          ))}
        </ul>
      </nav>
    </header>
  );
}

"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { usePrefs, type ThemePref } from "@/lib/prefs";
import { Locale, LOCALE_NAMES, LOCALE_SHORT, switchLocalePath, t } from "@/lib/i18n";
import { cx } from "./ui";

const iconBtn =
  "inline-flex min-h-tap min-w-tap items-center justify-center gap-1.5 rounded-md px-2 text-sm font-medium text-fg hover:bg-surface-2";

export function Logo({ locale = "en", compact = false }: { locale?: Locale; compact?: boolean }) {
  return (
    <span className="inline-flex items-center gap-2">
      <svg aria-hidden="true" width="32" height="32" viewBox="0 0 32 32" className="shrink-0">
        <rect width="32" height="32" rx="8" fill="var(--color-primary)" />
        <path d="M8 22V12l8-4 8 4v10" fill="none" stroke="var(--color-primary-fg)" strokeWidth="2" strokeLinejoin="round" />
        <path d="M11 22v-7M16 22v-7M21 22v-7M7 23h18" stroke="var(--color-accent)" strokeWidth="2" strokeLinecap="round" />
      </svg>
      {!compact && (
        <span className="leading-tight">
          <span className="block text-base font-bold text-fg">{t(locale, "brand")}</span>
          <span className="hidden text-xs text-fg-muted sm:block">{t(locale, "brandTagline")}</span>
        </span>
      )}
    </span>
  );
}

/** Cycles System -> Light -> Dark. The label always states the current theme. */
export function ThemeToggle({ locale = "en" }: { locale?: Locale }) {
  const { theme, setTheme } = usePrefs();
  const next: Record<ThemePref, ThemePref> = { system: "light", light: "dark", dark: "system" };
  const name = theme === "light" ? t(locale, "themeLight") : theme === "dark" ? t(locale, "themeDark") : t(locale, "themeSystem");
  const icon = theme === "light" ? "☀" : theme === "dark" ? "☾" : "◐";
  return (
    <button type="button" className={iconBtn} onClick={() => setTheme(next[theme])} aria-label={`${t(locale, "theme")}: ${name}`}
      title={`${t(locale, "theme")}: ${name}`}>
      <span aria-hidden="true" className="text-lg">{icon}</span>
      <span className="hidden xl:inline">{name}</span>
    </button>
  );
}

export function SeniorToggle({ locale = "en", onChange }: { locale?: Locale; onChange?: (on: boolean) => void }) {
  const { senior, setSenior } = usePrefs();
  return (
    <button type="button" role="switch" aria-checked={senior} className={cx(iconBtn, senior && "bg-primary-soft text-primary")}
      onClick={() => {
        setSenior(!senior);
        onChange?.(!senior);
      }}
      title={t(locale, "seniorMode")}>
      <span aria-hidden="true" className="font-bold">A+</span>
      <span className="hidden lg:inline">{t(locale, "seniorMode")}</span>
      <span className="sr-only lg:hidden">{t(locale, "seniorMode")}</span>
    </button>
  );
}

/** Links to the same page in the other language (public pages). */
export function LanguageSwitch({ locale }: { locale: Locale }) {
  const pathname = usePathname() || `/${locale}`;
  const other: Locale = locale === "en" ? "hi" : "en";
  // Keep the query string (e.g. ?category=loans) so the switch lands on the same view.
  const [query, setQuery] = useState("");
  useEffect(() => {
    const update = () => setQuery(window.location.search);
    update();
    const timer = setInterval(update, 1000);
    return () => clearInterval(timer);
  }, [pathname]);
  return (
    <Link href={switchLocalePath(pathname, other) + query} hrefLang={other} lang={other} className={iconBtn}
      aria-label={`${t(locale, "language")}: ${LOCALE_NAMES[other]}`}>
      <span aria-hidden="true">{LOCALE_SHORT[locale]}/</span>
      <span className="font-semibold">{LOCALE_SHORT[other]}</span>
    </Link>
  );
}

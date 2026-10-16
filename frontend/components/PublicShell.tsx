"use client";

import { ReactNode, useEffect } from "react";
import { Locale, t } from "@/lib/i18n";
import { AssistantWidget } from "./AssistantWidget";
import { ConsentManager } from "./ConsentManager";
import { MockBanner } from "./MockBanner";
import { SiteFooter } from "./SiteFooter";
import { SiteHeader } from "./SiteHeader";

/** Public site shell: header, environment banner, page, regulatory footer, consent manager and assistant. */
export function PublicShell({ locale, children }: { locale: Locale; children: ReactNode }) {
  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);

  return (
    <div lang={locale} className="flex min-h-screen flex-col">
      <a href="#main" className="skip-link">{t(locale, "skipToContent")}</a>
      <SiteHeader locale={locale} />
      <MockBanner locale={locale} />
      <main id="main" tabIndex={-1} className="page-container flex-1 py-6 sm:py-8">
        {children}
      </main>
      <SiteFooter locale={locale} />
      <ConsentManager locale={locale} />
      <AssistantWidget locale={locale} />
    </div>
  );
}

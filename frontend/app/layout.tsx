import type { Metadata, Viewport } from "next";
import { ReactNode } from "react";
import { PrefsProvider, PREFS_BOOT_SCRIPT } from "@/lib/prefs";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Example Bank — Premium digital banking", template: "%s · Example Bank" },
  description:
    "Savings, deposits, loans, cards, NRI and wealth services with bilingual (English/Hindi) support. Placeholder brand; synthetic data only.",
  applicationName: "Example Bank",
  robots: { index: true, follow: true },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#1b3a7a" },
    { media: "(prefers-color-scheme: dark)", color: "#0a1120" },
  ],
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: PREFS_BOOT_SCRIPT }} />
      </head>
      <body>
        <PrefsProvider>{children}</PrefsProvider>
      </body>
    </html>
  );
}

"use client";

import { useEffect, useState } from "react";
import { applyTheme, readStoredTheme, systemTheme, Theme } from "@/lib/theme";

export default function ThemeToggle({ className = "" }: { className?: string }) {
  // null until mounted: the server can't know the visitor's theme.
  const [theme, setTheme] = useState<Theme | null>(null);

  useEffect(() => {
    const stored = readStoredTheme();
    setTheme(stored ?? systemTheme());
    if (stored || !window.matchMedia) return;
    // No explicit choice yet: keep tracking the OS setting live.
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = (e: MediaQueryListEvent) => {
      if (!readStoredTheme()) setTheme(e.matches ? "dark" : "light");
    };
    mq.addEventListener?.("change", onChange);
    return () => mq.removeEventListener?.("change", onChange);
  }, []);

  const isDark = theme === "dark";
  const next: Theme = isDark ? "light" : "dark";

  return (
    <button
      type="button"
      className={`icon-btn ${className}`}
      onClick={() => {
        applyTheme(next, true);
        setTheme(next);
      }}
      aria-label={theme ? `Switch to ${next} theme` : "Toggle colour theme"}
      title={theme ? `Switch to ${next} theme` : "Toggle colour theme"}
    >
      {isDark ? (
        // Sun: shown in dark mode, switches to light.
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
        </svg>
      ) : (
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
        </svg>
      )}
    </button>
  );
}

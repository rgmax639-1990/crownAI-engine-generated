"use client";

import { createContext, ReactNode, useCallback, useContext, useEffect, useState } from "react";
import type { Locale } from "./i18n";

export type ThemePref = "system" | "light" | "dark";

type Prefs = {
  theme: ThemePref;
  setTheme: (t: ThemePref) => void;
  senior: boolean;
  setSenior: (on: boolean) => void;
  /** UI language for the portal and staff console (public pages use the URL locale). */
  locale: Locale;
  setLocale: (l: Locale) => void;
};

const PrefsContext = createContext<Prefs | null>(null);

function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}
function write(key: string, value: string) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* storage unavailable (private mode) */
  }
}

/** Inline script for <head>: applies theme and senior mode before first paint (no flash). */
export const PREFS_BOOT_SCRIPT = `(function(){try{var t=localStorage.getItem('asr.theme');if(t==='light'||t==='dark'){document.documentElement.setAttribute('data-theme',t);}if(localStorage.getItem('asr.senior')==='1'){document.documentElement.classList.add('senior');}}catch(e){}})();`;

export function PrefsProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<ThemePref>("system");
  const [senior, setSeniorState] = useState(false);
  const [locale, setLocaleState] = useState<Locale>("en");

  useEffect(() => {
    const t = read("asr.theme");
    if (t === "light" || t === "dark") setThemeState(t);
    setSeniorState(read("asr.senior") === "1");
    const l = read("asr.locale");
    if (l === "en" || l === "hi") setLocaleState(l);
  }, []);

  const setTheme = useCallback((t: ThemePref) => {
    setThemeState(t);
    write("asr.theme", t);
    const el = document.documentElement;
    if (t === "system") el.removeAttribute("data-theme");
    else el.setAttribute("data-theme", t);
  }, []);

  const setSenior = useCallback((on: boolean) => {
    setSeniorState(on);
    write("asr.senior", on ? "1" : "0");
    document.documentElement.classList.toggle("senior", on);
  }, []);

  const setLocale = useCallback((l: Locale) => {
    setLocaleState(l);
    write("asr.locale", l);
  }, []);

  return (
    <PrefsContext.Provider value={{ theme, setTheme, senior, setSenior, locale, setLocale }}>{children}</PrefsContext.Provider>
  );
}

export function usePrefs(): Prefs {
  const ctx = useContext(PrefsContext);
  if (!ctx) throw new Error("usePrefs must be used inside PrefsProvider");
  return ctx;
}

/**
 * Locales. English and Hindi ship at launch; the other Indian languages are wired as
 * "ready" (dictionaries and CMS locale variants can be added without code changes).
 * Page-specific strings live next to each page as `{ en: {...}, hi: {...} }` objects;
 * this file holds the shell strings only.
 */
export type Locale = "en" | "hi";
export const LOCALES: Locale[] = ["en", "hi"];
export const READY_LOCALES = ["ta", "te", "kn", "mr", "bn", "gu", "ml"] as const;
export const LOCALE_NAMES: Record<Locale, string> = { en: "English", hi: "हिन्दी" };
export const LOCALE_SHORT: Record<Locale, string> = { en: "EN", hi: "हिं" };

export function isLocale(v: unknown): v is Locale {
  return v === "en" || v === "hi";
}

/** Prefix a public path with the locale: lp("hi", "/rates") -> "/hi/rates". */
export function lp(locale: Locale, path = ""): string {
  const p = path.startsWith("/") ? path : `/${path}`;
  return `/${locale}${p === "/" ? "" : p}`;
}

/** Same page in the other language: swaps the first path segment. */
export function switchLocalePath(pathname: string, to: Locale): string {
  const segs = pathname.split("/");
  if (isLocale(segs[1])) {
    segs[1] = to;
    return segs.join("/") || `/${to}`;
  }
  return `/${to}${pathname === "/" ? "" : pathname}`;
}

const dict = {
  en: {
    brand: "Example Bank",
    brandTagline: "Premium banking, made simple",
    skipToContent: "Skip to main content",
    menu: "Menu",
    closeMenu: "Close menu",
    mainNav: "Main navigation",
    navAccounts: "Accounts",
    navDeposits: "Deposits",
    navLoans: "Loans",
    navCards: "Cards",
    navNri: "NRI",
    navWealth: "Wealth",
    navRates: "Rates",
    navTools: "Calculators",
    navLocator: "Branches & ATMs",
    navHelp: "Help & grievance",
    search: "Search",
    searchPlaceholder: "Search products, rates, IFSC, branches…",
    login: "Login",
    openAccount: "Open account",
    reportFraud: "Report fraud",
    reportFraud247: "Report fraud / block card 24x7",
    theme: "Theme",
    themeLight: "Light",
    themeDark: "Dark",
    themeSystem: "System",
    seniorMode: "Senior mode",
    seniorModeOn: "Senior mode on: larger text and simpler screens",
    language: "Language",
    mockBanner: "Demo environment: integrations run on MOCK providers with synthetic data. No real money moves.",
    mockOtp: "Mock OTP",
    details: "Details",
    privacyChoices: "Privacy choices",
    loading: "Loading…",
    retry: "Try again",
    footerRegulatory: "Regulatory disclosures",
    footerGrievance: "Grievance redressal",
    footerSafe: "Safe banking",
    footerPolicies: "Policies & disclosures",
    footerQuickLinks: "Quick links",
    statusPage: "Service status",
    sitemap: "Sitemap",
    footerUnavailable: "Regulatory disclosures could not be loaded.",
  },
  hi: {
    brand: "एग्ज़ाम्पल बैंक",
    brandTagline: "प्रीमियम बैंकिंग, आसान तरीके से",
    skipToContent: "मुख्य सामग्री पर जाएँ",
    menu: "मेन्यू",
    closeMenu: "मेन्यू बंद करें",
    mainNav: "मुख्य नेविगेशन",
    navAccounts: "खाते",
    navDeposits: "जमा",
    navLoans: "ऋण",
    navCards: "कार्ड",
    navNri: "एनआरआई",
    navWealth: "वेल्थ",
    navRates: "ब्याज दरें",
    navTools: "कैलकुलेटर",
    navLocator: "शाखा और एटीएम",
    navHelp: "सहायता और शिकायत",
    search: "खोजें",
    searchPlaceholder: "उत्पाद, दरें, IFSC, शाखा खोजें…",
    login: "लॉगिन",
    openAccount: "खाता खोलें",
    reportFraud: "धोखाधड़ी रिपोर्ट करें",
    reportFraud247: "धोखाधड़ी रिपोर्ट / कार्ड ब्लॉक 24x7",
    theme: "थीम",
    themeLight: "लाइट",
    themeDark: "डार्क",
    themeSystem: "सिस्टम",
    seniorMode: "वरिष्ठ नागरिक मोड",
    seniorModeOn: "वरिष्ठ नागरिक मोड चालू: बड़ा टेक्स्ट और सरल स्क्रीन",
    language: "भाषा",
    mockBanner: "डेमो वातावरण: सभी इंटीग्रेशन MOCK प्रदाताओं और कृत्रिम डेटा पर चल रहे हैं। कोई वास्तविक राशि ट्रांसफ़र नहीं होती।",
    mockOtp: "मॉक OTP",
    details: "विवरण",
    privacyChoices: "गोपनीयता विकल्प",
    loading: "लोड हो रहा है…",
    retry: "फिर से कोशिश करें",
    footerRegulatory: "नियामक प्रकटीकरण",
    footerGrievance: "शिकायत निवारण",
    footerSafe: "सुरक्षित बैंकिंग",
    footerPolicies: "नीतियाँ और प्रकटीकरण",
    footerQuickLinks: "त्वरित लिंक",
    statusPage: "सेवा स्थिति",
    sitemap: "साइटमैप",
    footerUnavailable: "नियामक प्रकटीकरण लोड नहीं हो सके।",
  },
} as const;

export type ShellKey = keyof (typeof dict)["en"];

export function t(locale: Locale, key: ShellKey): string {
  return dict[locale]?.[key] ?? dict.en[key];
}

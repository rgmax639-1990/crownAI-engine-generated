"use client";

/**
 * Calculators (US-8): EMI, FD/RD, SIP, income tax (old vs new) and LRS/TCS.
 * Every number is calculated by the backend (POST /public/calculators/{calculator}), debounced.
 * Calculations can be shared (POST /public/calculators/shares) and reopened with ?share=<id>.
 */
import { useParams, useSearchParams } from "next/navigation";
import { ReactNode, Suspense, useEffect, useMemo, useState } from "react";
import { RequestCallModal } from "@/components/RequestCallModal";
import {
  Accordion, Alert, Badge, Button, Card, CardHeader, Column, CopyButton, DataTable, Field, Input, KeyValue, LinkButton,
  LoadingState, PageHeader, SelectField, Spinner, StatTile, Switch, TabPanel, Tabs, Tone, cx,
} from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { formatDate, formatINR, formatLakhCrore, formatNumber, paiseToRupeesInput, rupeesToPaise } from "@/lib/format";
import { isLocale, Locale, lp } from "@/lib/i18n";

// ------------------------------------------------------------------ types
type CalcId = "emi" | "fd" | "rd" | "sip" | "income_tax" | "lrs_tcs";
type TabId = "emi" | "deposits" | "sip" | "income_tax" | "lrs_tcs";
type L = { en: string; hi: string };
type NumDef = { kind: "money" | "number"; key: string; label: L; min: number; max: number; step: number; suffix?: L; optional?: boolean; hint?: L };
type BoolDef = { kind: "bool"; key: string; label: L; description?: L };
type ChoiceDef = { kind: "select"; key: string; label: L; options: Array<{ value: string; label: L }> };
type FieldDef = NumDef | BoolDef | ChoiceDef;
type Values = Record<string, string | boolean>;
type Row = Record<string, unknown>;

type CalcResult = {
  calculator: CalcId;
  inputs: Record<string, unknown>;
  results: Record<string, unknown>;
  schedule: Row[];
  yearly_schedule?: Row[];
  assumptions: string[];
  rule_sources: Array<{ rule: string; as_on: string | null }>;
  disclaimer?: string;
  calculated_at?: string;
};
type ShareOut = { share_id: string; url: string; calculator: CalcId; inputs: Record<string, unknown>; expires_at: string };

// ------------------------------------------------------------------ strings
const T = {
  en: {
    title: "Calculators",
    description: "Plan loans, deposits, investments and taxes. Results are calculated by the bank's servers using today's published rates and rules.",
    tablist: "Calculators",
    emi: "Loan EMI", deposits: "FD / RD", sip: "SIP", income_tax: "Income tax", lrs_tcs: "LRS / TCS",
    fd: "Fixed deposit", rd: "Recurring deposit", depositType: "Deposit type",
    inputs: "Your inputs", results: "Your results", calculating: "Calculating…", updated: "Results updated",
    fixErrors: "Correct the highlighted values to see results.", calcError: "We couldn't calculate this",
    share: "Share", shareTitle: "Share this calculation", shareLink: "Link valid until", shareNote: "The link holds only the calculator values — never your personal details.",
    copyLink: "Copy link", shareFailed: "Could not create a share link.",
    loadingShare: "Loading the shared calculation…", sharedLoaded: "You are viewing a shared calculation. Change any value to recalculate.",
    shareExpired: "This shared link has expired (links are kept for 30 days). The calculator shows default values.",
    shareNotFound: "We couldn't find this shared calculation. The calculator shows default values.",
    apply: "Apply with these values", requestCall: "Request a call",
    assumptions: "Assumptions", ruleSources: "Rules and sources", asOn: "as on",
    required: "This field is required", invalidNumber: "Enter a valid number", between: "Enter a value between {min} and {max}",
    rateUsed: "Rate used", publishedRate: "today's published rate card", userRate: "the rate you entered", maturityOn: "Maturity date",
    tdsApplied: "TDS estimated", tdsNone: "No TDS in this estimate", perAnnum: "p.a.",
    regimeOld: "Old regime", regimeNew: "New regime", recommended: "Recommended", either: "Same tax in both",
    taxableIncome: "Taxable income", standardDeduction: "Standard deduction", deductions: "Deductions", taxOnSlabs: "Tax on slabs",
    rebate: "Rebate u/s 87A", marginalRelief: "Marginal relief", surcharge: "Surcharge", cess: "Health & education cess", totalTax: "Total tax",
    effectiveRate: "Effective rate", tcsRate: "TCS rate",
    yearly: "Year-wise summary", monthly: "Month-by-month amortisation schedule", quarterly: "Quarter-wise growth", monthlyGrowth: "Month-wise growth",
    yearlyGrowth: "Year-wise growth", slabs: "Slab-wise tax",
    month: "Month", year: "Year", quarter: "Quarter", emiCol: "EMI", principal: "Principal", interest: "Interest", balance: "Balance",
    opening: "Opening", closing: "Closing", deposit: "Deposit", deposited: "Total deposited", value: "Value", invested: "Invested",
    returns: "Returns", slab: "Income slab", rate: "Rate", inSlab: "Income in slab", tax: "Tax", above: "and above",
    days: "days", months: "months", leaveBlank: "Leave blank to use today's published rate.",
  },
  hi: {
    title: "कैलकुलेटर",
    description: "ऋण, जमा, निवेश और कर की योजना बनाएँ। परिणाम बैंक के सर्वर पर आज की प्रकाशित दरों और नियमों से निकाले जाते हैं।",
    tablist: "कैलकुलेटर",
    emi: "लोन EMI", deposits: "FD / RD", sip: "SIP", income_tax: "आयकर", lrs_tcs: "LRS / TCS",
    fd: "सावधि जमा (FD)", rd: "आवर्ती जमा (RD)", depositType: "जमा का प्रकार",
    inputs: "आपके मान", results: "आपके परिणाम", calculating: "गणना हो रही है…", updated: "परिणाम अपडेट हो गए",
    fixErrors: "परिणाम देखने के लिए चिह्नित मान ठीक करें।", calcError: "गणना नहीं हो सकी",
    share: "शेयर करें", shareTitle: "यह गणना शेयर करें", shareLink: "लिंक इस तिथि तक मान्य", shareNote: "लिंक में केवल कैलकुलेटर के मान होते हैं — आपकी व्यक्तिगत जानकारी कभी नहीं।",
    copyLink: "लिंक कॉपी करें", shareFailed: "शेयर लिंक नहीं बन सका।",
    loadingShare: "शेयर की गई गणना लोड हो रही है…", sharedLoaded: "आप शेयर की गई गणना देख रहे हैं। दोबारा गणना के लिए कोई भी मान बदलें।",
    shareExpired: "यह शेयर लिंक समाप्त हो गया है (लिंक 30 दिन तक रखे जाते हैं)। कैलकुलेटर डिफ़ॉल्ट मान दिखा रहा है।",
    shareNotFound: "यह शेयर की गई गणना नहीं मिली। कैलकुलेटर डिफ़ॉल्ट मान दिखा रहा है।",
    apply: "इन मानों के साथ आवेदन करें", requestCall: "कॉल का अनुरोध करें",
    assumptions: "मान्यताएँ", ruleSources: "नियम और स्रोत", asOn: "दिनांक",
    required: "यह फ़ील्ड आवश्यक है", invalidNumber: "मान्य संख्या दर्ज करें", between: "{min} और {max} के बीच मान दर्ज करें",
    rateUsed: "प्रयुक्त दर", publishedRate: "आज की प्रकाशित दर सूची", userRate: "आपके द्वारा दर्ज दर", maturityOn: "परिपक्वता तिथि",
    tdsApplied: "अनुमानित TDS", tdsNone: "इस अनुमान में TDS नहीं", perAnnum: "प्रति वर्ष",
    regimeOld: "पुरानी कर व्यवस्था", regimeNew: "नई कर व्यवस्था", recommended: "अनुशंसित", either: "दोनों में समान कर",
    taxableIncome: "कर योग्य आय", standardDeduction: "मानक कटौती", deductions: "कटौतियाँ", taxOnSlabs: "स्लैब पर कर",
    rebate: "धारा 87A छूट", marginalRelief: "मार्जिनल राहत", surcharge: "अधिभार", cess: "स्वास्थ्य और शिक्षा उपकर", totalTax: "कुल कर",
    effectiveRate: "प्रभावी दर", tcsRate: "TCS दर",
    yearly: "वर्षवार सारांश", monthly: "मासिक ऋण परिशोधन अनुसूची", quarterly: "तिमाही वृद्धि", monthlyGrowth: "मासिक वृद्धि",
    yearlyGrowth: "वर्षवार वृद्धि", slabs: "स्लैब-वार कर",
    month: "महीना", year: "वर्ष", quarter: "तिमाही", emiCol: "EMI", principal: "मूलधन", interest: "ब्याज", balance: "शेष राशि",
    opening: "प्रारंभिक", closing: "अंतिम", deposit: "जमा", deposited: "कुल जमा", value: "मूल्य", invested: "निवेश",
    returns: "रिटर्न", slab: "आय स्लैब", rate: "दर", inSlab: "स्लैब में आय", tax: "कर", above: "और अधिक",
    days: "दिन", months: "महीने", leaveBlank: "आज की प्रकाशित दर के लिए खाली छोड़ें।",
  },
} as const;
type Strings = { [K in keyof (typeof T)["en"]]: string };

// ------------------------------------------------------------------ calculator definitions
const PCT: L = { en: "% p.a.", hi: "% प्रति वर्ष" };
const MONTHS: L = { en: "months", hi: "महीने" };
const SENIOR: BoolDef = { kind: "bool", key: "senior_citizen", label: { en: "Senior citizen (60+)", hi: "वरिष्ठ नागरिक (60+)" },
  description: { en: "Higher rate and higher TDS threshold", hi: "अधिक ब्याज दर और अधिक TDS सीमा" } };
const TDS: BoolDef = { kind: "bool", key: "apply_tds", label: { en: "Apply TDS", hi: "TDS लागू करें" },
  description: { en: "Turn off if you submit Form 15G/15H", hi: "फ़ॉर्म 15G/15H जमा करने पर बंद करें" } };
const OPTIONAL_RATE: NumDef = { kind: "number", key: "annual_rate_percent", label: { en: "Interest rate (optional)", hi: "ब्याज दर (वैकल्पिक)" },
  min: 0, max: 20, step: 0.05, suffix: PCT, optional: true, hint: { en: "Leave blank to use today's published rate.", hi: "आज की प्रकाशित दर के लिए खाली छोड़ें।" } };

const FIELDS: Record<CalcId, FieldDef[]> = {
  emi: [
    { kind: "money", key: "principal_paise", label: { en: "Loan amount", hi: "ऋण राशि" }, min: 10000, max: 50000000, step: 10000 },
    { kind: "number", key: "annual_rate_percent", label: { en: "Interest rate", hi: "ब्याज दर" }, min: 1, max: 20, step: 0.05, suffix: PCT },
    { kind: "number", key: "tenure_months", label: { en: "Tenure", hi: "अवधि" }, min: 6, max: 360, step: 6, suffix: MONTHS },
  ],
  fd: [
    { kind: "money", key: "principal_paise", label: { en: "Deposit amount", hi: "जमा राशि" }, min: 1000, max: 100000000, step: 1000 },
    { kind: "number", key: "tenure_days", label: { en: "Tenure", hi: "अवधि" }, min: 7, max: 3650, step: 1, suffix: { en: "days", hi: "दिन" } },
    SENIOR, TDS, OPTIONAL_RATE,
  ],
  rd: [
    { kind: "money", key: "monthly_investment_paise", label: { en: "Monthly instalment", hi: "मासिक किस्त" }, min: 100, max: 1000000, step: 100 },
    { kind: "number", key: "tenure_months", label: { en: "Tenure", hi: "अवधि" }, min: 6, max: 120, step: 1, suffix: MONTHS },
    SENIOR, TDS, OPTIONAL_RATE,
  ],
  sip: [
    { kind: "money", key: "monthly_investment_paise", label: { en: "Monthly investment", hi: "मासिक निवेश" }, min: 500, max: 1000000, step: 500 },
    { kind: "number", key: "expected_return_percent", label: { en: "Expected return", hi: "अपेक्षित रिटर्न" }, min: 1, max: 30, step: 0.5, suffix: PCT },
    { kind: "number", key: "tenure_months", label: { en: "Investment period", hi: "निवेश अवधि" }, min: 12, max: 480, step: 12, suffix: MONTHS },
  ],
  income_tax: [
    { kind: "money", key: "annual_income_paise", label: { en: "Annual income", hi: "वार्षिक आय" }, min: 0, max: 50000000, step: 10000 },
    { kind: "money", key: "deductions_paise", label: { en: "Deductions (80C, 80D, home-loan interest…)", hi: "कटौतियाँ (80C, 80D, गृह ऋण ब्याज…)" },
      min: 0, max: 1000000, step: 5000, hint: { en: "Used only in the old regime.", hi: "केवल पुरानी व्यवस्था में उपयोग होती हैं।" } },
    { kind: "select", key: "age_band", label: { en: "Age", hi: "आयु" }, options: [
      { value: "below_60", label: { en: "Below 60", hi: "60 से कम" } },
      { value: "60_80", label: { en: "60 to 80", hi: "60 से 80" } },
      { value: "above_80", label: { en: "Above 80", hi: "80 से अधिक" } },
    ] },
    { kind: "select", key: "financial_year", label: { en: "Financial year", hi: "वित्त वर्ष" }, options: [
      { value: "2026-27", label: { en: "FY 2026-27", hi: "वित्त वर्ष 2026-27" } },
      { value: "2025-26", label: { en: "FY 2025-26", hi: "वित्त वर्ष 2025-26" } },
    ] },
    { kind: "bool", key: "salaried", label: { en: "Salaried / pensioner", hi: "वेतनभोगी / पेंशनभोगी" },
      description: { en: "Applies the standard deduction", hi: "मानक कटौती लागू होती है" } },
  ],
  lrs_tcs: [
    { kind: "money", key: "remittance_paise", label: { en: "Amount to send abroad", hi: "विदेश भेजी जाने वाली राशि" }, min: 1000, max: 20000000, step: 1000 },
    { kind: "money", key: "already_remitted_paise", label: { en: "Already sent this financial year", hi: "इस वित्त वर्ष में पहले भेजी गई राशि" },
      min: 0, max: 20000000, step: 1000, hint: { en: "Include remittances through other banks.", hi: "अन्य बैंकों से भेजी गई राशि भी शामिल करें।" } },
    { kind: "select", key: "lrs_purpose", label: { en: "Purpose", hi: "उद्देश्य" }, options: [
      { value: "education_loan", label: { en: "Education (funded by an education loan)", hi: "शिक्षा (शिक्षा ऋण से)" } },
      { value: "education", label: { en: "Education (own funds)", hi: "शिक्षा (स्वयं के धन से)" } },
      { value: "medical", label: { en: "Medical treatment", hi: "चिकित्सा उपचार" } },
      { value: "other", label: { en: "Other (travel, gifts, investment…)", hi: "अन्य (यात्रा, उपहार, निवेश…)" } },
    ] },
  ],
};

const DEFAULTS: Record<CalcId, Values> = {
  emi: { principal_paise: "2500000", annual_rate_percent: "8.5", tenure_months: "240" },
  fd: { principal_paise: "500000", tenure_days: "365", senior_citizen: false, apply_tds: true, annual_rate_percent: "" },
  rd: { monthly_investment_paise: "10000", tenure_months: "36", senior_citizen: false, apply_tds: true, annual_rate_percent: "" },
  sip: { monthly_investment_paise: "10000", expected_return_percent: "12", tenure_months: "120" },
  income_tax: { annual_income_paise: "1500000", deductions_paise: "150000", age_band: "below_60", financial_year: "2026-27", salaried: true },
  lrs_tcs: { remittance_paise: "1500000", already_remitted_paise: "0", lrs_purpose: "education" },
};

const TABS: TabId[] = ["emi", "deposits", "sip", "income_tax", "lrs_tcs"];
const CALC_IDS: CalcId[] = ["emi", "fd", "rd", "sip", "income_tax", "lrs_tcs"];

/** Lead product for "Request a call" per calculator. */
const CALL_PRODUCT: Record<CalcId, string> = {
  emi: "personal-loan", fd: "fixed-deposit", rd: "recurring-deposit", sip: "mutual-funds", income_tax: "tax-saver-fd", lrs_tcs: "outward-remittance",
};

/**
 * "Apply with these values" hand-off into the journeys owned by other slices.
 * Query parameters carry only calculator values (paise / days / months), never personal data.
 */
function handoffUrl(calc: CalcId, p: Record<string, unknown>, shareId: string | null): string | null {
  const q = new URLSearchParams();
  const add = (k: string, v: unknown) => {
    if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
  };
  if (shareId) add("calculator_share_id", shareId);
  switch (calc) {
    case "emi":
      add("amount_paise", p.principal_paise);
      add("tenure_months", p.tenure_months);
      return `/portal/loans/apply?${q.toString()}`;
    case "fd":
      add("principal_paise", p.principal_paise);
      add("tenure_days", p.tenure_days);
      add("senior_citizen", p.senior_citizen);
      return `/portal/deposits?${q.toString()}`;
    case "rd":
      add("type", "rd");
      add("monthly_investment_paise", p.monthly_investment_paise);
      add("tenure_months", p.tenure_months);
      return `/portal/deposits?${q.toString()}`;
    case "sip":
      add("sip_amount_paise", p.monthly_investment_paise);
      return `/portal/wealth?${q.toString()}`;
    case "lrs_tcs":
      add("inr_amount_paise", p.remittance_paise);
      add("purpose", p.lrs_purpose);
      return `/portal/lrs?${q.toString()}`;
    default:
      return null;
  }
}

// ------------------------------------------------------------------ value helpers
function fmt(s: string, vars: Record<string, string>): string {
  return s.replace(/\{(\w+)\}/g, (_, k: string) => vars[k] ?? "");
}

function parseNum(def: NumDef, raw: string): number {
  const s = raw.trim();
  if (!s) return NaN;
  if (def.kind === "money") {
    const p = rupeesToPaise(s);
    return p === null ? NaN : p / 100;
  }
  const cleaned = s.replace(/,/g, "");
  return /^-?\d+(\.\d+)?$/.test(cleaned) ? Number(cleaned) : NaN;
}

function boundLabel(def: NumDef, n: number, locale: Locale): string {
  return def.kind === "money" ? formatLakhCrore(Math.round(n * 100), locale) : formatNumber(n, Number.isInteger(n) ? 0 : 2);
}

function validate(calc: CalcId, v: Values, s: Strings, locale: Locale): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const f of FIELDS[calc]) {
    if (f.kind !== "money" && f.kind !== "number") continue;
    const raw = String(v[f.key] ?? "").trim();
    if (!raw) {
      if (!f.optional) errors[f.key] = s.required;
      continue;
    }
    const n = parseNum(f, raw);
    if (!Number.isFinite(n)) {
      errors[f.key] = s.invalidNumber;
      continue;
    }
    if (n < f.min || n > f.max) errors[f.key] = fmt(s.between, { min: boundLabel(f, f.min, locale), max: boundLabel(f, f.max, locale) });
  }
  return errors;
}

function toPayload(calc: CalcId, v: Values): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const f of FIELDS[calc]) {
    const val = v[f.key];
    if (f.kind === "bool") out[f.key] = Boolean(val);
    else if (f.kind === "select") out[f.key] = String(val ?? "");
    else {
      const raw = String(val ?? "").trim();
      if (!raw) continue;
      const n = parseNum(f, raw);
      if (!Number.isFinite(n)) continue;
      out[f.key] = f.kind === "money" ? Math.round(n * 100) : n;
    }
  }
  return out;
}

function fromInputs(calc: CalcId, inputs: Record<string, unknown>): Values {
  const out: Values = { ...DEFAULTS[calc] };
  for (const f of FIELDS[calc]) {
    if (!(f.key in inputs)) continue;
    const val = inputs[f.key];
    if (f.kind === "bool") out[f.key] = Boolean(val);
    else if (f.kind === "select") out[f.key] = val === null || val === undefined ? String(out[f.key]) : String(val);
    else if (val === null || val === undefined) out[f.key] = "";
    else if (f.kind === "money") out[f.key] = typeof val === "number" ? paiseToRupeesInput(val) : "";
    else out[f.key] = String(val);
  }
  return out;
}

function calcForTab(tab: TabId, depositMode: "fd" | "rd"): CalcId {
  return tab === "deposits" ? depositMode : tab;
}

function num(obj: Record<string, unknown> | undefined, key: string): number | null {
  const v = obj?.[key];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

// ------------------------------------------------------------------ page
export default function ToolsPage() {
  return (
    <Suspense fallback={<LoadingState rows={4} />}>
      <Tools />
    </Suspense>
  );
}

function Tools() {
  const params = useParams<{ locale: string }>();
  const rawLocale = params?.locale;
  const locale: Locale = isLocale(rawLocale) ? rawLocale : "en";
  const s: Strings = T[locale];
  const searchParams = useSearchParams();
  const shareParam = searchParams.get("share");

  const [tab, setTab] = useState<TabId>("emi");
  const [depositMode, setDepositMode] = useState<"fd" | "rd">("fd");
  const [values, setValues] = useState<Record<CalcId, Values>>(() => ({ ...DEFAULTS }));
  const [results, setResults] = useState<Partial<Record<CalcId, CalcResult>>>({});
  const [loading, setLoading] = useState(false);
  const [calcError, setCalcError] = useState<{ key: string; error: ApiError } | null>(null);
  const [serverErrors, setServerErrors] = useState<{ key: string; errors: Record<string, string> } | null>(null);
  const [nonce, setNonce] = useState(0);
  const [share, setShare] = useState<{ key: string; id: string; url: string; expires_at: string } | null>(null);
  const [shareBusy, setShareBusy] = useState(false);
  const [shareError, setShareError] = useState<string | null>(null);
  const [sharedLoad, setSharedLoad] = useState<{ status: "idle" | "loading" | "loaded" | "error"; message?: string }>({ status: "idle" });
  const [callOpen, setCallOpen] = useState(false);
  const [origin, setOrigin] = useState("");

  useEffect(() => setOrigin(window.location.origin), []);

  const calc = calcForTab(tab, depositMode);
  const current = values[calc];
  const clientErrors = useMemo(() => validate(calc, current, s, locale), [calc, current, s, locale]);
  const payload = useMemo(() => toPayload(calc, current), [calc, current]);
  const payloadKey = `${calc}:${JSON.stringify(payload)}`;
  const hasClientErrors = Object.keys(clientErrors).length > 0;

  // Load a shared calculation (?share=<id>).
  useEffect(() => {
    if (!shareParam) return;
    const ctrl = new AbortController();
    setSharedLoad({ status: "loading" });
    api<ShareOut>(`/public/calculators/shares/${encodeURIComponent(shareParam)}?locale=${locale}`, { auth: false, signal: ctrl.signal })
      .then((r) => {
        if (!CALC_IDS.includes(r.calculator)) throw new ApiError(404, "not_found", s.shareNotFound, null, {});
        const next = fromInputs(r.calculator, r.inputs || {});
        setValues((prev) => ({ ...prev, [r.calculator]: next }));
        if (r.calculator === "fd" || r.calculator === "rd") {
          setTab("deposits");
          setDepositMode(r.calculator);
        } else {
          setTab(r.calculator);
        }
        setShare({ key: `${r.calculator}:${JSON.stringify(toPayload(r.calculator, next))}`, id: r.share_id, url: r.url, expires_at: r.expires_at });
        setSharedLoad({ status: "loaded" });
      })
      .catch((e: unknown) => {
        if ((e as Error)?.name === "AbortError") return;
        const status = e instanceof ApiError ? e.status : 0;
        setSharedLoad({
          status: "error",
          message: status === 410 ? s.shareExpired : status === 404 ? s.shareNotFound : e instanceof ApiError ? e.message : s.shareNotFound,
        });
      });
    return () => ctrl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shareParam, locale]);

  // Debounced recalculation on the server.
  useEffect(() => {
    if (hasClientErrors) {
      setLoading(false);
      return;
    }
    const ctrl = new AbortController();
    const key = payloadKey;
    const target = calc;
    setLoading(true);
    const timer = setTimeout(() => {
      api<CalcResult>(`/public/calculators/${target}`, { method: "POST", body: payload, auth: false, signal: ctrl.signal })
        .then((r) => {
          setResults((prev) => ({ ...prev, [target]: r }));
          setServerErrors(null);
          setCalcError(null);
        })
        .catch((e: unknown) => {
          if ((e as Error)?.name === "AbortError") return;
          const err = e instanceof ApiError ? e : new ApiError(0, "error", String(e), null, {});
          const fe = err.fieldErrors || {};
          const known = Object.keys(fe).filter((k) => FIELDS[target].some((f) => f.key === k));
          setServerErrors(known.length ? { key, errors: fe } : null);
          setCalcError(known.length ? null : { key, error: err });
        })
        .finally(() => {
          if (!ctrl.signal.aborted) setLoading(false);
        });
    }, 400);
    return () => {
      clearTimeout(timer);
      ctrl.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [payloadKey, hasClientErrors, nonce]);

  const setField = (key: string, val: string | boolean) => {
    setValues((prev) => ({ ...prev, [calc]: { ...prev[calc], [key]: val } }));
    setShareError(null);
  };

  const fieldError = (key: string): string | undefined =>
    clientErrors[key] || (serverErrors && serverErrors.key === payloadKey ? serverErrors.errors[key] : undefined);

  const currentShare = share && share.key === payloadKey ? share : null;
  const shareUrl = currentShare ? `${origin}${currentShare.url}` : "";
  const result = results[calc];
  const resultIsCurrent = !!result && !hasClientErrors;
  const currentError = calcError && calcError.key === payloadKey ? calcError.error : null;
  const applyHref = handoffUrl(calc, payload, currentShare?.id ?? null);

  const doShare = async () => {
    setShareBusy(true);
    setShareError(null);
    try {
      const r = await api<ShareOut>("/public/calculators/shares", {
        method: "POST", auth: false, body: { calculator: calc, inputs_json: payload, locale },
      });
      setShare({ key: payloadKey, id: r.share_id, url: r.url, expires_at: r.expires_at });
    } catch (e) {
      setShareError(e instanceof ApiError ? e.message : s.shareFailed);
    } finally {
      setShareBusy(false);
    }
  };

  const tabs = TABS.map((id) => ({ id, label: s[id] }));

  return (
    <div>
      <PageHeader
        title={s.title}
        description={s.description}
        breadcrumbs={[{ label: locale === "hi" ? "होम" : "Home", href: lp(locale) }, { label: s.title }]}
      />

      {sharedLoad.status === "loading" && (
        <div className="mb-4"><Spinner label={s.loadingShare} /></div>
      )}
      {sharedLoad.status === "loaded" && <Alert tone="info" className="mb-4">{s.sharedLoaded}</Alert>}
      {sharedLoad.status === "error" && <Alert tone="warning" className="mb-4">{sharedLoad.message}</Alert>}

      <Tabs tabs={tabs} value={tab} onChange={(id) => setTab(id as TabId)} ariaLabel={s.tablist} idPrefix="calc" />

      {TABS.map((id) => (
        <TabPanel key={id} id={id} value={tab} idPrefix="calc">
          <div className="grid gap-6 lg:grid-cols-5">
            <Card as="section" className="lg:col-span-2">
              <CardHeader title={s.inputs} />
              {id === "deposits" && (
                <div role="group" aria-label={s.depositType} className="mb-5 inline-flex rounded-md border border-border bg-surface-2 p-1">
                  {(["fd", "rd"] as const).map((m) => (
                    <button key={m} type="button" aria-pressed={depositMode === m} onClick={() => setDepositMode(m)}
                      className={cx("min-h-tap rounded px-4 text-sm font-semibold",
                        depositMode === m ? "bg-primary text-primary-fg shadow-sm" : "text-fg-muted hover:text-fg")}>
                      {s[m]}
                    </button>
                  ))}
                </div>
              )}
              <form className="space-y-5" noValidate onSubmit={(e) => { e.preventDefault(); setNonce((n) => n + 1); }}>
                {FIELDS[calc].map((f) => {
                  const fid = `calc-${calc}-${f.key}`;
                  if (f.kind === "bool") {
                    return (
                      <Switch key={f.key} id={fid} checked={Boolean(current[f.key])} onChange={(v) => setField(f.key, v)}
                        label={f.label[locale]} description={f.description?.[locale]} />
                    );
                  }
                  if (f.kind === "select") {
                    return (
                      <SelectField key={f.key} id={fid} label={f.label[locale]} value={String(current[f.key] ?? "")}
                        onChange={(v) => setField(f.key, v)} error={fieldError(f.key)}
                        options={f.options.map((o) => ({ value: o.value, label: o.label[locale] }))} />
                    );
                  }
                  return (
                    <NumberSlider key={f.key} id={fid} def={f} value={String(current[f.key] ?? "")} locale={locale}
                      error={fieldError(f.key)} onChange={(v) => setField(f.key, v)} />
                  );
                })}
              </form>
            </Card>

            <div className="space-y-6 lg:col-span-3">
              <Card as="section" className={cx("transition-opacity", loading && resultIsCurrent && "opacity-70")}>
                <div aria-busy={loading}>
                  <CardHeader
                    title={s.results}
                    actions={loading ? <Spinner size="sm" label={s.calculating} /> : undefined}
                  />
                  <p className="sr-only" aria-live="polite">{!loading && resultIsCurrent ? s.updated : ""}</p>
                  {hasClientErrors ? (
                    <Alert tone="warning">{s.fixErrors}</Alert>
                  ) : currentError ? (
                    <Alert tone="danger" title={s.calcError}
                      action={<Button size="sm" variant="secondary" onClick={() => setNonce((n) => n + 1)}>{locale === "hi" ? "फिर से कोशिश करें" : "Try again"}</Button>}>
                      {currentError.message}
                    </Alert>
                  ) : !result ? (
                    <LoadingState label={s.calculating} rows={2} />
                  ) : (
                    <ResultSummary calc={calc} result={result} locale={locale} s={s} />
                  )}

                  {!hasClientErrors && result && (
                    <div className="mt-5 flex flex-wrap gap-2 border-t border-border pt-4">
                      {applyHref && <LinkButton href={applyHref}>{s.apply}</LinkButton>}
                      <Button variant="secondary" onClick={() => setCallOpen(true)}>{s.requestCall}</Button>
                      <Button variant="outline" onClick={doShare} loading={shareBusy} disabled={!!currentShare}>{s.share}</Button>
                    </div>
                  )}
                  {shareError && <p role="alert" className="mt-3 text-sm font-medium text-danger">{shareError}</p>}
                  {currentShare && (
                    <div className="mt-4 rounded-md border border-border bg-surface-2 p-3">
                      <p className="text-sm font-semibold text-fg">{s.shareTitle}</p>
                      <div className="mt-2 flex flex-wrap items-center gap-2">
                        <code className="min-w-0 flex-1 break-all rounded bg-surface px-2 py-1 text-sm">{shareUrl}</code>
                        <CopyButton value={shareUrl} label={s.copyLink} />
                      </div>
                      <p className="mt-2 text-xs text-fg-muted">
                        {s.shareLink} {formatDate(currentShare.expires_at)}. {s.shareNote}
                      </p>
                    </div>
                  )}
                </div>
              </Card>

              {result && !hasClientErrors && <Schedules calc={calc} result={result} locale={locale} s={s} />}
              {result && !hasClientErrors && <AssumptionsNote result={result} s={s} />}
            </div>
          </div>
        </TabPanel>
      ))}

      <RequestCallModal
        open={callOpen}
        onClose={() => setCallOpen(false)}
        locale={locale}
        channel="calculator"
        productSlug={CALL_PRODUCT[calc]}
        calculatorShareId={currentShare?.id}
      />
    </div>
  );
}

// ------------------------------------------------------------------ input: slider + number field
function NumberSlider({ id, def, value, onChange, error, locale }: {
  id: string; def: NumDef; value: string; onChange: (v: string) => void; error?: string; locale: Locale;
}) {
  const label = def.label[locale];
  const n = parseNum(def, value);
  const sliderVal = Number.isFinite(n) ? Math.min(def.max, Math.max(def.min, n)) : def.min;
  const valueText = def.kind === "money"
    ? formatLakhCrore(Math.round(sliderVal * 100), locale)
    : `${formatNumber(sliderVal, Number.isInteger(sliderVal) ? 0 : 2)} ${def.suffix?.[locale] ?? ""}`.trim();
  const hintParts = [
    def.kind === "money" && Number.isFinite(n) ? formatLakhCrore(Math.round(n * 100), locale) : null,
    def.hint?.[locale] ?? null,
  ].filter(Boolean);
  const hint = hintParts.length ? hintParts.join(" · ") : undefined;
  const describedBy = error ? `${id}-error` : hint ? `${id}-hint` : undefined;
  const toSliderString = (raw: string) => (def.kind === "money" ? String(Math.round(Number(raw))) : raw);
  return (
    <Field id={id} label={label} hint={hint} error={error} required={!def.optional}>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-4">
        {!def.optional && (
          <input type="range" id={`${id}-range`} min={def.min} max={def.max} step={def.step} value={sliderVal}
            onChange={(e) => onChange(toSliderString(e.target.value))}
            aria-label={label} aria-valuetext={valueText} aria-controls={id}
            className="w-full min-w-0 flex-1 cursor-pointer" />
        )}
        <div className={cx("flex items-stretch", def.optional ? "w-full sm:w-56" : "w-full sm:w-48 sm:shrink-0")}>
          {def.kind === "money" && (
            <span aria-hidden="true" className="inline-flex items-center rounded-l-md border border-r-0 border-border-strong bg-surface-2 px-3 text-fg-muted">₹</span>
          )}
          <Input id={id} name={id} inputMode="decimal" autoComplete="off" value={value} invalid={!!error}
            aria-describedby={describedBy} onChange={(e) => onChange(e.target.value)}
            className={cx("tabular-nums", def.kind === "money" && "rounded-l-none", def.suffix && "rounded-r-none")} />
          {def.suffix && (
            <span className="inline-flex items-center whitespace-nowrap rounded-r-md border border-l-0 border-border-strong bg-surface-2 px-2 text-sm text-fg-muted">
              {def.suffix[locale]}
            </span>
          )}
        </div>
      </div>
    </Field>
  );
}

// ------------------------------------------------------------------ results
const TILE_LABELS: Record<string, L> = {
  emi: { en: "Monthly EMI", hi: "मासिक EMI" },
  total_interest: { en: "Total interest", hi: "कुल ब्याज" },
  total_payment: { en: "Total amount payable", hi: "कुल देय राशि" },
  principal: { en: "Principal", hi: "मूलधन" },
  maturity: { en: "Maturity amount", hi: "परिपक्वता राशि" },
  interest: { en: "Interest earned", hi: "अर्जित ब्याज" },
  tds: { en: "Estimated TDS", hi: "अनुमानित TDS" },
  net_maturity: { en: "Maturity after TDS", hi: "TDS के बाद परिपक्वता राशि" },
  total_deposit: { en: "Total deposited", hi: "कुल जमा" },
  value: { en: "Estimated value", hi: "अनुमानित मूल्य" },
  invested: { en: "Amount invested", hi: "निवेश की गई राशि" },
  estimated_returns: { en: "Estimated returns", hi: "अनुमानित रिटर्न" },
  old_tax: { en: "Tax — old regime", hi: "कर — पुरानी व्यवस्था" },
  new_tax: { en: "Tax — new regime", hi: "कर — नई व्यवस्था" },
  savings: { en: "You save", hi: "आपकी बचत" },
  tcs: { en: "TCS collected", hi: "संग्रहित TCS" },
  amount_above_threshold: { en: "Amount above TCS threshold", hi: "TCS सीमा से अधिक राशि" },
  total_outflow: { en: "Total debit (amount + TCS)", hi: "कुल डेबिट (राशि + TCS)" },
  remittance: { en: "Amount sent", hi: "भेजी गई राशि" },
};

const TILES: Record<CalcId, Array<{ key: string; tone?: Tone }>> = {
  emi: [{ key: "emi", tone: "primary" }, { key: "total_interest" }, { key: "total_payment" }, { key: "principal" }],
  fd: [{ key: "maturity", tone: "primary" }, { key: "interest", tone: "success" }, { key: "tds", tone: "warning" }, { key: "net_maturity" }],
  rd: [{ key: "maturity", tone: "primary" }, { key: "total_deposit" }, { key: "interest", tone: "success" }, { key: "net_maturity" }],
  sip: [{ key: "value", tone: "primary" }, { key: "invested" }, { key: "estimated_returns", tone: "success" }],
  income_tax: [{ key: "old_tax" }, { key: "new_tax" }, { key: "savings", tone: "success" }],
  lrs_tcs: [{ key: "tcs", tone: "primary" }, { key: "amount_above_threshold" }, { key: "total_outflow" }, { key: "remittance" }],
};

function ResultSummary({ calc, result, locale, s }: { calc: CalcId; result: CalcResult; locale: Locale; s: Strings }) {
  const res = result.results;
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2">
        {TILES[calc].map((t) => {
          const paise = num(res, `${t.key}_paise`);
          return (
            <StatTile key={t.key} label={TILE_LABELS[t.key]?.[locale] ?? t.key} tone={t.tone}
              value={formatLakhCrore(paise, locale)} hint={formatINR(paise, { decimals: 0 })} />
          );
        })}
      </div>
      {(calc === "fd" || calc === "rd") && <DepositFacts res={res} locale={locale} s={s} calc={calc} />}
      {calc === "income_tax" && <TaxComparison res={res} locale={locale} s={s} />}
      {calc === "lrs_tcs" && (
        <KeyValue items={[
          { label: s.tcsRate, value: `${num(res, "tcs_rate_percent") ?? "—"}%` },
          { label: s.effectiveRate, value: `${num(res, "effective_tcs_percent") ?? "—"}%` },
        ]} />
      )}
      {calc === "emi" && (
        <KeyValue items={[
          { label: locale === "hi" ? "कुल भुगतान में ब्याज का हिस्सा" : "Interest share of total payment", value: `${num(res, "interest_share_percent") ?? "—"}%` },
        ]} />
      )}
    </div>
  );
}

function DepositFacts({ res, locale, s, calc }: { res: Record<string, unknown>; locale: Locale; s: Strings; calc: "fd" | "rd" }) {
  const rate = num(res, "rate_percent");
  const fromCard = res.rate_source === "published_rate";
  const tdsApplicable = res.tds_applicable === true;
  const items = [
    { label: s.rateUsed, value: `${rate !== null ? rate.toFixed(2) : "—"}% ${s.perAnnum} (${fromCard ? s.publishedRate : s.userRate})` },
    { label: s.tdsApplied, value: tdsApplicable ? `${formatINR(num(res, "tds_paise"), { decimals: 0 })} @ ${num(res, "tds_rate_percent")}%` : s.tdsNone },
  ];
  if (calc === "fd" && typeof res.maturity_date === "string") items.push({ label: s.maturityOn, value: formatDate(res.maturity_date) });
  if (calc === "fd" && num(res, "effective_annual_yield_percent") !== null) {
    items.push({ label: locale === "hi" ? "प्रभावी वार्षिक प्रतिफल" : "Effective annual yield", value: `${num(res, "effective_annual_yield_percent")}%` });
  }
  return <KeyValue items={items} />;
}

function TaxComparison({ res, locale, s }: { res: Record<string, unknown>; locale: Locale; s: Strings }) {
  const rec = String(res.recommended_regime ?? "");
  const regimes: Array<{ id: "old" | "new"; title: string; data: Record<string, unknown> | undefined }> = [
    { id: "old", title: s.regimeOld, data: res.old_regime as Record<string, unknown> | undefined },
    { id: "new", title: s.regimeNew, data: res.new_regime as Record<string, unknown> | undefined },
  ];
  const rows: Array<[string, string]> = [
    [s.taxableIncome, "taxable_income_paise"], [s.standardDeduction, "standard_deduction_paise"], [s.deductions, "deductions_paise"],
    [s.taxOnSlabs, "tax_on_slabs_paise"], [s.rebate, "rebate_87a_paise"], [s.marginalRelief, "marginal_relief_paise"],
    [s.surcharge, "surcharge_paise"], [s.cess, "cess_paise"], [s.totalTax, "total_tax_paise"],
  ];
  return (
    <div className="space-y-3">
      <Alert tone={rec === "either" ? "info" : "success"} title={rec === "either" ? s.either : `${s.recommended}: ${rec === "old" ? s.regimeOld : s.regimeNew}`}>
        {String(res.recommendation ?? "")}
      </Alert>
      <div className="grid gap-3 sm:grid-cols-2">
        {regimes.map((r) => (
          <div key={r.id} className={cx("rounded-lg border p-3", rec === r.id ? "border-success" : "border-border")}>
            <p className="mb-2 flex items-center gap-2 font-semibold text-fg">
              {r.title}
              {rec === r.id && <Badge tone="success">{s.recommended}</Badge>}
            </p>
            <dl className="space-y-1 text-sm">
              {rows.map(([label, key]) => (
                <div key={key} className={cx("flex justify-between gap-3", key === "total_tax_paise" && "border-t border-border pt-1 font-semibold")}>
                  <dt className="text-fg-muted">{label}</dt>
                  <dd className="tabular-nums text-fg">{formatINR(num(r.data, key), { decimals: 0 })}</dd>
                </div>
              ))}
              <div className="flex justify-between gap-3">
                <dt className="text-fg-muted">{s.effectiveRate}</dt>
                <dd className="tabular-nums text-fg">{num(r.data, "effective_rate_percent") ?? "—"}%</dd>
              </div>
            </dl>
          </div>
        ))}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ schedules
function moneyCol(key: string, header: string): Column<Row> {
  return { key, header, align: "right", render: (r) => formatINR(typeof r[key] === "number" ? (r[key] as number) : null, { decimals: 0 }) };
}
function plainCol(key: string, header: string): Column<Row> {
  return { key, header, render: (r) => String(r[key] ?? "—") };
}

function Schedules({ calc, result, locale, s }: { calc: CalcId; result: CalcResult; locale: Locale; s: Strings }) {
  const items: Array<{ id: string; title: string; content: ReactNode }> = [];
  const sched = result.schedule || [];
  if (calc === "emi") {
    items.push({
      id: "yearly", title: s.yearly,
      content: <DataTable caption={s.yearly} rows={result.yearly_schedule || []} rowKey={(r) => String(r.year)}
        columns={[plainCol("year", s.year), moneyCol("principal_paise", s.principal), moneyCol("interest_paise", s.interest), moneyCol("balance_paise", s.balance)]} />,
    });
    items.push({
      id: "monthly", title: s.monthly,
      content: <DataTable caption={s.monthly} rows={sched} rowKey={(r) => String(r.month)}
        columns={[plainCol("month", s.month), moneyCol("emi_paise", s.emiCol), moneyCol("principal_paise", s.principal), moneyCol("interest_paise", s.interest), moneyCol("balance_paise", s.balance)]} />,
    });
  } else if (calc === "fd" && sched.length) {
    items.push({
      id: "quarterly", title: s.quarterly,
      content: <DataTable caption={s.quarterly} rows={sched} rowKey={(r) => String(r.period)}
        columns={[plainCol("period", s.quarter), moneyCol("opening_paise", s.opening), moneyCol("interest_paise", s.interest), moneyCol("closing_paise", s.closing)]} />,
    });
  } else if (calc === "rd" && sched.length) {
    items.push({
      id: "monthly", title: s.monthlyGrowth,
      content: <DataTable caption={s.monthlyGrowth} rows={sched} rowKey={(r) => String(r.month)}
        columns={[plainCol("month", s.month), moneyCol("deposit_paise", s.deposit), moneyCol("total_deposited_paise", s.deposited), moneyCol("value_paise", s.value)]} />,
    });
  } else if (calc === "sip" && sched.length) {
    items.push({
      id: "yearly", title: s.yearlyGrowth,
      content: <DataTable caption={s.yearlyGrowth} rows={sched} rowKey={(r) => String(r.year)}
        columns={[plainCol("year", s.year), moneyCol("invested_paise", s.invested), moneyCol("returns_paise", s.returns), moneyCol("value_paise", s.value)]} />,
    });
  } else if (calc === "income_tax") {
    for (const [id, title] of [["old_regime", s.regimeOld], ["new_regime", s.regimeNew]] as const) {
      const regime = result.results[id] as { slabs?: Row[] } | undefined;
      const slabs = regime?.slabs || [];
      items.push({
        id, title: `${s.slabs} — ${title}`,
        content: <DataTable caption={`${s.slabs} — ${title}`} rows={slabs} rowKey={(r) => `${id}-${String(r.from_paise)}`}
          columns={[
            { key: "slab", header: s.slab, render: (r) => `${formatINR(r.from_paise as number, { decimals: 0 })} – ${r.to_paise === null || r.to_paise === undefined ? s.above : formatINR(r.to_paise as number, { decimals: 0 })}` },
            { key: "rate_percent", header: s.rate, align: "right", render: (r) => `${String(r.rate_percent)}%` },
            moneyCol("taxable_paise", s.inSlab),
            moneyCol("tax_paise", s.tax),
          ]} />,
      });
    }
  }
  if (!items.length) return null;
  return (
    <section aria-label={locale === "hi" ? "विस्तृत अनुसूची" : "Detailed schedule"}>
      <Accordion items={items} />
    </section>
  );
}

function AssumptionsNote({ result, s }: { result: CalcResult; s: Strings }) {
  return (
    <Card as="aside" className="text-sm">
      <h2 className="font-semibold text-fg">{s.assumptions}</h2>
      <ul className="mt-2 list-disc space-y-1 pl-5 text-fg-muted">
        {result.assumptions.map((a, i) => <li key={i}>{a}</li>)}
      </ul>
      {result.rule_sources?.length > 0 && (
        <>
          <h3 className="mt-4 font-semibold text-fg">{s.ruleSources}</h3>
          <ul className="mt-2 list-disc space-y-1 pl-5 text-fg-muted">
            {result.rule_sources.map((r, i) => (
              <li key={i}>{r.rule}{r.as_on ? ` — ${s.asOn} ${formatDate(r.as_on)}` : ""}</li>
            ))}
          </ul>
        </>
      )}
      {result.disclaimer && <p className="mt-4 text-xs text-fg-muted">{result.disclaimer}</p>}
    </Card>
  );
}

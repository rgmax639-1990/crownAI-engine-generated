/**
 * Indian locale formatting: rupees with lakh/crore grouping, DD/MM/YYYY dates and IST times.
 * All money from the API is in paise (integers).
 */
import type { Locale } from "./i18n";

const TZ = "Asia/Kolkata";

const inr0 = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });
const inr2 = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", minimumFractionDigits: 2, maximumFractionDigits: 2 });
const num = new Intl.NumberFormat("en-IN");

/** ₹12,34,567.89 (2 decimals by default, Indian digit grouping). */
export function formatINR(paise: number | null | undefined, opts: { decimals?: 0 | 2 } = {}): string {
  if (paise === null || paise === undefined || Number.isNaN(paise)) return "—";
  const rupees = paise / 100;
  return (opts.decimals === 0 ? inr0 : inr2).format(rupees);
}

/** Amount in another currency from minor units (e.g. USD cents). */
export function formatCurrency(minor: number | null | undefined, currency: string): string {
  if (minor === null || minor === undefined) return "—";
  try {
    return new Intl.NumberFormat("en-IN", { style: "currency", currency, minimumFractionDigits: 2 }).format(minor / 100);
  } catch {
    return `${currency} ${(minor / 100).toFixed(2)}`;
  }
}

/** Compact lakh/crore: ₹12.35 lakh, ₹1.2 crore (Hindi: लाख / करोड़). Below 1 lakh shows full rupees. */
export function formatLakhCrore(paise: number | null | undefined, locale: Locale = "en"): string {
  if (paise === null || paise === undefined || Number.isNaN(paise)) return "—";
  const rupees = paise / 100;
  const abs = Math.abs(rupees);
  const sign = rupees < 0 ? "-" : "";
  const trim = (n: number) => n.toFixed(2).replace(/\.?0+$/, "");
  if (abs >= 1e7) return `${sign}₹${trim(abs / 1e7)} ${locale === "hi" ? "करोड़" : "crore"}`;
  if (abs >= 1e5) return `${sign}₹${trim(abs / 1e5)} ${locale === "hi" ? "लाख" : "lakh"}`;
  return inr0.format(rupees);
}

export function formatNumber(n: number | null | undefined, fractionDigits = 0): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return fractionDigits
    ? new Intl.NumberFormat("en-IN", { minimumFractionDigits: fractionDigits, maximumFractionDigits: fractionDigits }).format(n)
    : num.format(n);
}

export function formatPercent(n: number | null | undefined, digits = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `${n.toFixed(digits)}%`;
}

function toDate(v: string | number | Date | null | undefined): Date | null {
  if (v === null || v === undefined || v === "") return null;
  if (v instanceof Date) return v;
  // Plain dates (YYYY-MM-DD) are calendar dates: keep them as-is regardless of timezone.
  if (typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v)) {
    const [y, m, d] = v.split("-").map(Number);
    return new Date(Date.UTC(y, m - 1, d, 6, 30));
  }
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? null : d;
}

function parts(d: Date) {
  const p = new Intl.DateTimeFormat("en-GB", {
    timeZone: TZ, day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(d);
  const get = (t: string) => p.find((x) => x.type === t)?.value ?? "";
  return { dd: get("day"), mm: get("month"), yyyy: get("year"), hh: get("hour") === "24" ? "00" : get("hour"), min: get("minute") };
}

/** DD/MM/YYYY in IST. */
export function formatDate(v: string | number | Date | null | undefined): string {
  const d = toDate(v);
  if (!d) return "—";
  const p = parts(d);
  return `${p.dd}/${p.mm}/${p.yyyy}`;
}

/** DD/MM/YYYY, HH:mm IST */
export function formatDateTime(v: string | number | Date | null | undefined): string {
  const d = toDate(v);
  if (!d) return "—";
  const p = parts(d);
  return `${p.dd}/${p.mm}/${p.yyyy}, ${p.hh}:${p.min} IST`;
}

export function formatTime(v: string | number | Date | null | undefined): string {
  const d = toDate(v);
  if (!d) return "—";
  const p = parts(d);
  return `${p.hh}:${p.min} IST`;
}

/** Today's date in IST as YYYY-MM-DD (for <input type="date"> defaults). */
export function todayISO(offsetDays = 0): string {
  const d = new Date(Date.now() + offsetDays * 86400000);
  const p = parts(d);
  return `${p.yyyy}-${p.mm}-${p.dd}`;
}

/**
 * Parse a rupee amount typed by a user ("12,500.50") into paise.
 * Returns null when empty or invalid (more than 2 decimals, negative, not a number).
 */
export function rupeesToPaise(input: string): number | null {
  const s = String(input ?? "").replace(/[,\s₹]/g, "");
  if (!s) return null;
  if (!/^\d+(\.\d{1,2})?$/.test(s)) return null;
  const [r, p = ""] = s.split(".");
  return Number(r) * 100 + Number((p + "00").slice(0, 2));
}

export function paiseToRupeesInput(paise: number | null | undefined): string {
  if (paise === null || paise === undefined) return "";
  return (paise / 100).toFixed(paise % 100 === 0 ? 0 : 2);
}

/** Remaining time until `iso`, e.g. "4h 12m", "3d 2h", or "overdue by 1h 5m". */
export function timeUntil(iso: string | null | undefined, now: number = Date.now()): { ms: number; label: string; overdue: boolean } {
  const d = toDate(iso ?? null);
  if (!d) return { ms: 0, label: "—", overdue: false };
  const ms = d.getTime() - now;
  const abs = Math.abs(ms);
  const days = Math.floor(abs / 86400000);
  const hours = Math.floor((abs % 86400000) / 3600000);
  const mins = Math.floor((abs % 3600000) / 60000);
  const secs = Math.floor((abs % 60000) / 1000);
  const label = days > 0 ? `${days}d ${hours}h` : hours > 0 ? `${hours}h ${mins}m` : mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
  return { ms, label: ms < 0 ? `overdue by ${label}` : label, overdue: ms < 0 };
}

export function maskMiddle(value: string, visible = 4): string {
  if (!value) return "";
  return value.length <= visible ? value : `${"X".repeat(Math.max(0, value.length - visible))}${value.slice(-visible)}`;
}

/** Humanise an enum value: "fd_general" -> "Fd general". Prefer explicit labels where they exist. */
export function humanize(v: string | null | undefined): string {
  if (!v) return "—";
  const s = v.replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

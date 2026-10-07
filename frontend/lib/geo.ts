"use client";

// Best-effort jurisdiction detection in the browser, strongest signal first:
//   1. the region of the visitor's primary locale ("en-IN" -> "IN");
//   2. the device time zone ("Asia/Kolkata" -> "IN"), which covers locales
//      with no region such as "hi" or "en";
//   3. the region of any other preferred locale.
// Returns null when nothing usable is found, so the caller can let the
// backend decide from its geo-IP/CDN header instead of assuming a country.

// IANA time zones (including legacy aliases) -> ISO 3166-1 alpha-2.
const TIME_ZONE_COUNTRY: Record<string, string> = {
  "Asia/Kolkata": "IN",
  "Asia/Calcutta": "IN",
  "Europe/Vienna": "AT",
  "Europe/Brussels": "BE",
  "Europe/Sofia": "BG",
  "Europe/Zagreb": "HR",
  "Asia/Nicosia": "CY",
  "Europe/Nicosia": "CY",
  "Europe/Prague": "CZ",
  "Europe/Copenhagen": "DK",
  "Europe/Tallinn": "EE",
  "Europe/Helsinki": "FI",
  "Europe/Paris": "FR",
  "Europe/Berlin": "DE",
  "Europe/Busingen": "DE",
  "Europe/Athens": "GR",
  "Europe/Budapest": "HU",
  "Europe/Dublin": "IE",
  "Europe/Rome": "IT",
  "Europe/Riga": "LV",
  "Europe/Vilnius": "LT",
  "Europe/Luxembourg": "LU",
  "Europe/Malta": "MT",
  "Europe/Amsterdam": "NL",
  "Europe/Warsaw": "PL",
  "Europe/Lisbon": "PT",
  "Atlantic/Azores": "PT",
  "Atlantic/Madeira": "PT",
  "Europe/Bucharest": "RO",
  "Europe/Bratislava": "SK",
  "Europe/Ljubljana": "SI",
  "Europe/Madrid": "ES",
  "Atlantic/Canary": "ES",
  "Europe/Stockholm": "SE",
  "Atlantic/Reykjavik": "IS",
  "Europe/Vaduz": "LI",
  "Europe/Oslo": "NO",
  "Europe/London": "GB",
  "Europe/Zurich": "CH",
  "America/New_York": "US",
  "America/Detroit": "US",
  "America/Chicago": "US",
  "America/Denver": "US",
  "America/Phoenix": "US",
  "America/Los_Angeles": "US",
  "America/Anchorage": "US",
  "Pacific/Honolulu": "US",
  "US/Eastern": "US",
  "US/Central": "US",
  "US/Mountain": "US",
  "US/Pacific": "US",
  "America/Toronto": "CA",
  "America/Vancouver": "CA",
  "America/Sao_Paulo": "BR",
  "Asia/Tokyo": "JP",
  "Asia/Singapore": "SG",
  "Asia/Dubai": "AE",
  "Australia/Sydney": "AU",
  "Australia/Melbourne": "AU",
};

function regionOf(locale: string | undefined): string | null {
  if (!locale) return null;
  const parts = locale.split(/[-_]/).slice(1);
  for (let i = parts.length - 1; i >= 0; i--) {
    if (/^[a-z]{2}$/i.test(parts[i])) return parts[i].toUpperCase();
  }
  return null;
}

export function countryFromTimeZone(timeZone: string | undefined): string | null {
  return (timeZone && TIME_ZONE_COUNTRY[timeZone]) || null;
}

export function guessCountryCode(): string | null {
  try {
    const locales = navigator.languages?.length ? Array.from(navigator.languages) : [navigator.language];
    const primary = regionOf(locales[0]);
    if (primary) return primary;

    let timeZone: string | undefined;
    try {
      timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    } catch {
      timeZone = undefined;
    }
    const fromZone = countryFromTimeZone(timeZone);
    if (fromZone) return fromZone;

    for (const locale of locales.slice(1)) {
      const region = regionOf(locale);
      if (region) return region;
    }
  } catch {
    // ignore
  }
  return null;
}

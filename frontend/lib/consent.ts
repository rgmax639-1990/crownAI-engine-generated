export type ConsentPolicy = {
  country: string;
  regime: string;
  regime_name: string;
  requires_opt_in: boolean;
  banner_text: string;
  rights: string[];
};

// EU + EEA. Mirrors EU_COUNTRIES in backend/routers/consent.py.
export const EU_COUNTRIES = new Set([
  "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU",
  "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES",
  "SE", "IS", "LI", "NO",
]);

// Client-side copy of the backend's jurisdiction rules
// (backend/routers/consent.py `_policy_for_country`). The backend stays the
// source of truth; this is used when the consent API can't be reached so a
// visitor still gets the rules for *their* jurisdiction (e.g. GDPR opt-in
// with a Reject option) instead of a one-size-fits-all notice.
export function policyForCountry(countryCode: string): ConsentPolicy {
  const country = (countryCode || "").toUpperCase();
  if (country === "IN") {
    return {
      country,
      regime: "DPDP",
      regime_name: "Digital Personal Data Protection Act, 2023 (India)",
      requires_opt_in: true,
      banner_text:
        "We use cookies and process personal data in accordance with India's DPDP Act. You can withdraw consent at any time.",
      rights: ["access", "correction", "erasure", "grievance_redressal", "consent_withdrawal"],
    };
  }
  if (EU_COUNTRIES.has(country)) {
    return {
      country,
      regime: "GDPR",
      regime_name: "General Data Protection Regulation (EU/EEA)",
      requires_opt_in: true,
      banner_text:
        "We use cookies. Under GDPR you may accept or reject non-essential cookies, and you have the right to access, rectify, or erase your data.",
      rights: ["access", "rectification", "erasure", "portability", "objection", "restriction"],
    };
  }
  if (country === "US") {
    return {
      country,
      regime: "CCPA",
      regime_name: "California Consumer Privacy Act (and other US state privacy laws)",
      requires_opt_in: false,
      banner_text:
        "We use cookies. California and other US residents may opt out of the sale/sharing of personal information and request data deletion.",
      rights: ["know", "delete", "opt_out_of_sale", "non_discrimination"],
    };
  }
  return {
    country: country || "US",
    regime: "GENERIC",
    regime_name: "General data protection best practices",
    requires_opt_in: false,
    banner_text: "We use cookies to improve your experience. See our Privacy Policy for details.",
    rights: ["access", "deletion"],
  };
}

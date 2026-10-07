from typing import Optional

from fastapi import APIRouter, Query, Request

router = APIRouter(tags=["consent"])

EU_COUNTRIES = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU",
    "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES",
    "SE", "IS", "LI", "NO",
}


def _policy_for_country(country: str) -> dict:
    country = (country or "").upper()
    if country == "IN":
        return {
            "regime": "DPDP",
            "regime_name": "Digital Personal Data Protection Act, 2023 (India)",
            "requires_opt_in": True,
            "banner_text": (
                "We use cookies and process personal data in accordance with India's "
                "DPDP Act. You can withdraw consent at any time."
            ),
            "rights": ["access", "correction", "erasure", "grievance_redressal", "consent_withdrawal"],
        }
    if country in EU_COUNTRIES:
        return {
            "regime": "GDPR",
            "regime_name": "General Data Protection Regulation (EU/EEA)",
            "requires_opt_in": True,
            "banner_text": (
                "We use cookies. Under GDPR you may accept or reject non-essential cookies, "
                "and you have the right to access, rectify, or erase your data."
            ),
            "rights": ["access", "rectification", "erasure", "portability", "objection", "restriction"],
        }
    if country == "US":
        return {
            "regime": "CCPA",
            "regime_name": "California Consumer Privacy Act (and other US state privacy laws)",
            "requires_opt_in": False,
            "banner_text": (
                "We use cookies. California and other US residents may opt out of the "
                "sale/sharing of personal information and request data deletion."
            ),
            "rights": ["know", "delete", "opt_out_of_sale", "non_discrimination"],
        }
    return {
        "regime": "GENERIC",
        "regime_name": "General data protection best practices",
        "requires_opt_in": False,
        "banner_text": "We use cookies to improve your experience. See our Privacy Policy for details.",
        "rights": ["access", "deletion"],
    }


# Geo-IP headers set by common CDNs / edge proxies in front of the API, in
# order of preference. Used only when the caller doesn't pass ?country.
GEO_COUNTRY_HEADERS = (
    "cf-ipcountry",
    "x-country-code",
    "cloudfront-viewer-country",
    "x-vercel-ip-country",
    "x-appengine-country",
    "fastly-geo-country-code",
)
DEFAULT_COUNTRY = "US"


def country_from_headers(request: Request) -> Optional[str]:
    for name in GEO_COUNTRY_HEADERS:
        value = (request.headers.get(name) or "").strip().upper()
        # "XX" / "T1" are Cloudflare's unknown / Tor markers, not countries.
        if len(value) == 2 and value.isalpha() and value != "XX":
            return value
    return None


@router.get("/consent/policy")
def get_consent_policy(
    request: Request,
    country: Optional[str] = Query(default=None, min_length=2, max_length=2),
):
    resolved = (country or country_from_headers(request) or DEFAULT_COUNTRY).upper()
    policy = _policy_for_country(resolved)
    policy["country"] = resolved
    return policy

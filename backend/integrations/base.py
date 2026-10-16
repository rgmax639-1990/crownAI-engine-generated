"""Adapter registry for every external institution.

Each provider has a mock implementation (the default in every environment) and a live
implementation. A live adapter is only selected when the provider's
``integration_adapters`` row for the current environment is in mode ``live``. That mode
can only be set through a recorded, approved ``IntegrationApproval`` (see routers/admin.py).

Live adapters read their credentials from the environment variables listed in
``PROVIDERS``. If they are missing, the adapter raises ``IntegrationNotConfigured`` when
it is actually used, never at import time.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any, Callable

PROVIDERS: dict[str, dict[str, Any]] = {
    "finacle": {"label": "Finacle core banking (via ESB)", "env": ["FINACLE_ESB_BASE_URL", "FINACLE_MTLS_CERT_PATH", "FINACLE_MTLS_KEY_PATH"]},
    "uidai": {"label": "UIDAI AUA/KUA (Aadhaar OTP eKYC)", "env": ["UIDAI_AUA_CODE", "UIDAI_ASA_URL", "UIDAI_LICENSE_KEY"]},
    "nsdl_pan": {"label": "NSDL/Protean PAN verification", "env": ["PAN_VERIFY_URL", "PAN_VERIFY_USER_ID", "PAN_VERIFY_SIGNING_CERT_PATH"]},
    "digilocker": {"label": "DigiLocker", "env": ["DIGILOCKER_CLIENT_ID", "DIGILOCKER_CLIENT_SECRET", "DIGILOCKER_REDIRECT_URI"]},
    "ckycr": {"label": "CKYC Registry (CERSAI)", "env": ["CKYCR_FI_CODE", "CKYCR_API_URL", "CKYCR_SIGNING_CERT_PATH"]},
    "npci_upi": {"label": "NPCI UPI", "env": ["NPCI_UPI_SWITCH_URL", "NPCI_UPI_PSP_CODE", "NPCI_UPI_SIGNING_KEY_PATH"]},
    "npci_imps": {"label": "NPCI IMPS", "env": ["NPCI_IMPS_SWITCH_URL", "NPCI_IMPS_BANK_CODE"]},
    "npci_nach": {"label": "NPCI NACH / e-NACH", "env": ["NPCI_NACH_URL", "NPCI_NACH_UTILITY_CODE"]},
    "bbps": {"label": "Bharat Connect (BBPS)", "env": ["BBPS_BOU_URL", "BBPS_AGENT_INSTITUTION_ID"]},
    "neft_rtgs": {"label": "RBI NEFT/RTGS (via SFMS)", "env": ["SFMS_GATEWAY_URL", "SFMS_IFSC_PREFIX"]},
    "bureau": {"label": "Credit bureau", "env": ["BUREAU_API_URL", "BUREAU_MEMBER_ID", "BUREAU_API_KEY"]},
    "account_aggregator": {"label": "Account Aggregator (FIU)", "env": ["AA_FIU_ID", "AA_GATEWAY_URL", "AA_SIGNING_KEY_PATH"]},
    "gstn": {"label": "GSTN (GSP)", "env": ["GSP_API_URL", "GSP_CLIENT_ID", "GSP_CLIENT_SECRET"]},
    "itr": {"label": "Income-tax ITR fetch", "env": ["ITR_API_URL", "ITR_API_KEY"]},
    "esign": {"label": "Aadhaar eSign (ESP)", "env": ["ESIGN_ESP_URL", "ESIGN_ASP_ID", "ESIGN_SIGNING_CERT_PATH"]},
    "shcil": {"label": "SHCIL (e-stamping)", "env": ["SHCIL_API_URL", "SHCIL_CLIENT_ID"]},
    "card_management": {"label": "Card management system", "env": ["CMS_API_URL", "CMS_CLIENT_CERT_PATH"]},
    "hsm": {"label": "Hardware security module", "env": ["HSM_ENDPOINT", "HSM_PARTITION", "HSM_CLIENT_CERT_PATH"]},
    "swift": {"label": "SWIFT (gpi)", "env": ["SWIFT_ALLIANCE_URL", "SWIFT_BIC"]},
    "crm": {"label": "CRM (lead management)", "env": ["CRM_API_URL", "CRM_API_KEY"]},
    "sms_gateway": {"label": "SMS / email / push gateway", "env": ["SMS_GATEWAY_URL", "SMS_GATEWAY_API_KEY", "SMS_SENDER_ID", "EMAIL_SMTP_URL"]},
    "mf_rta": {"label": "Mutual fund RTA / exchange", "env": ["MF_RTA_API_URL", "MF_RTA_ARN", "MF_RTA_API_KEY"]},
    "gst_tax_portal": {"label": "GST / income-tax challan portals (CIN)", "env": ["TAX_PORTAL_API_URL", "TAX_PORTAL_BANK_CODE"]},
}

ENVIRONMENTS = ["dev", "qa", "uat", "production"]


class IntegrationError(Exception):
    """An external provider failed. Mapped to HTTP 502/503 by the app."""

    status_code = 502
    code = "integration_error"

    def __init__(self, provider: str, message: str):
        super().__init__(message)
        self.provider = provider
        self.message = message


class IntegrationNotConfigured(IntegrationError):
    status_code = 503
    code = "integration_not_configured"


class IntegrationNotImplemented(IntegrationError):
    status_code = 503
    code = "integration_live_unavailable"


def current_environment() -> str:
    env = os.getenv("APP_ENV", "dev").lower()
    return env if env in ENVIRONMENTS else "dev"


def adapter_mode(conn: sqlite3.Connection, provider: str) -> str:
    row = conn.execute(
        "SELECT mode FROM integration_adapters WHERE provider = ? AND environment = ?",
        (provider, current_environment()),
    ).fetchone()
    return row["mode"] if row else "mock"


def is_mock(conn: sqlite3.Connection, provider: str) -> bool:
    return adapter_mode(conn, provider) != "live"


def mock_meta(provider: str) -> dict:
    """Attach to every API response that came from a mock provider (shown as a MOCK label)."""
    return {"provider": provider, "mode": "MOCK", "label": f"MOCK {PROVIDERS.get(provider, {}).get('label', provider)}"}


def require_env(provider: str) -> dict[str, str]:
    names = PROVIDERS[provider]["env"]
    missing = [n for n in names if not os.getenv(n)]
    if missing:
        raise IntegrationNotConfigured(
            provider,
            f"Live {PROVIDERS[provider]['label']} adapter is enabled but not configured: set {', '.join(missing)}",
        )
    return {n: os.environ[n] for n in names}


class LiveAdapter:
    """Live implementation placeholder bound to the provider's credentials.

    Any call first checks the credentials named in ``PROVIDERS`` (raising
    ``IntegrationNotConfigured`` listing the missing variables). With credentials
    present it still refuses, because the interface specification for the bank's ESB /
    the institution has to be supplied and certified before a live call is made.
    """

    is_mock = False

    def __init__(self, provider: str):
        self.provider = provider

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("__"):
            raise AttributeError(name)

        def _call(*_args: Any, **_kwargs: Any) -> Any:
            require_env(self.provider)
            raise IntegrationNotImplemented(
                self.provider,
                f"Live {PROVIDERS[self.provider]['label']} call '{name}' needs the certified interface "
                "specification from the bank; the adapter stays on mock until then.",
            )

        return _call


def resolve(conn: sqlite3.Connection, provider: str, mock_factory: Callable[[], Any]) -> Any:
    """Return the mock adapter unless the provider is in approved ``live`` mode."""
    if provider not in PROVIDERS:
        raise ValueError(f"Unknown provider {provider}")
    if adapter_mode(conn, provider) == "live":
        return LiveAdapter(provider)
    return mock_factory()

"""Google / Microsoft OAuth 2.0 adapters.

Real credentials (client id/secret) are read from environment variables.
When a provider's credentials are not configured, the login flow serves a
clearly-labelled local "mock sign-in" screen instead of a provider redirect,
so the product still works end-to-end in local/dev/docker-compose
environments that don't have real OAuth apps registered. As soon as the
matching env vars are set, that provider automatically switches to the real
OAuth Authorization Code flow -- no code changes needed -- and its mock
endpoints stop accepting sign-ins. Set OAUTH_DEV_MOCK=false to switch the
mock off everywhere (production).
"""
import os
import secrets
import urllib.parse

import httpx

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "http://localhost:8000/auth/google/callback")

MICROSOFT_CLIENT_ID = os.environ.get("MICROSOFT_CLIENT_ID", "")
MICROSOFT_CLIENT_SECRET = os.environ.get("MICROSOFT_CLIENT_SECRET", "")
MICROSOFT_TENANT_ID = os.environ.get("MICROSOFT_TENANT_ID", "common")
MICROSOFT_REDIRECT_URI = os.environ.get("MICROSOFT_REDIRECT_URI", "http://localhost:8000/auth/microsoft/callback")

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"

MS_AUTH_ENDPOINT = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
MS_TOKEN_ENDPOINT = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
MS_USERINFO_ENDPOINT = "https://graph.microsoft.com/oidc/userinfo"


class ProviderNotConfigured(Exception):
    """Raised only by callers that explicitly require real provider credentials."""


def mock_enabled() -> bool:
    """OAUTH_DEV_MOCK=false disables the dev mock sign-in entirely, so login
    always hands off to the real Google/Microsoft authorize endpoint (use in
    production and in any environment that must verify the real redirect)."""
    return os.environ.get("OAUTH_DEV_MOCK", "true").strip().lower() not in ("0", "false", "no", "off")


def use_mock(provider: str) -> bool:
    return mock_enabled() and not is_configured(provider)


def is_configured(provider: str) -> bool:
    if provider == "google":
        return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
    if provider == "microsoft":
        return bool(MICROSOFT_CLIENT_ID and MICROSOFT_CLIENT_SECRET)
    raise ValueError(f"Unknown provider: {provider}")


def new_state() -> str:
    return secrets.token_urlsafe(24)


def build_authorization_url(provider: str, state: str) -> str:
    if provider == "google":
        params = {
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "offline",
            "prompt": "select_account",
        }
        return f"{GOOGLE_AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"
    if provider == "microsoft":
        params = {
            "client_id": MICROSOFT_CLIENT_ID,
            "redirect_uri": MICROSOFT_REDIRECT_URI,
            "response_type": "code",
            "response_mode": "query",
            "scope": "openid email profile User.Read",
            "state": state,
        }
        url = MS_AUTH_ENDPOINT.format(tenant=MICROSOFT_TENANT_ID)
        return f"{url}?{urllib.parse.urlencode(params)}"
    raise ValueError(f"Unknown provider: {provider}")


async def exchange_code_for_profile(provider: str, code: str) -> dict:
    """Exchanges an authorization code for tokens, then fetches the user's profile.

    Returns a dict with keys: email, name, provider_sub.
    Raises RuntimeError with a clear message if the provider rejects the exchange.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        if provider == "google":
            token_resp = await client.post(
                GOOGLE_TOKEN_ENDPOINT,
                data={
                    "code": code,
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "redirect_uri": GOOGLE_REDIRECT_URI,
                    "grant_type": "authorization_code",
                },
            )
            if token_resp.status_code != 200:
                raise RuntimeError(f"Google token exchange failed: {token_resp.text}")
            access_token = token_resp.json()["access_token"]
            userinfo_resp = await client.get(
                GOOGLE_USERINFO_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"}
            )
            if userinfo_resp.status_code != 200:
                raise RuntimeError(f"Google userinfo fetch failed: {userinfo_resp.text}")
            info = userinfo_resp.json()
            return {
                "email": info["email"],
                "name": info.get("name") or info["email"].split("@")[0],
                "provider_sub": info["sub"],
            }
        if provider == "microsoft":
            token_url = MS_TOKEN_ENDPOINT.format(tenant=MICROSOFT_TENANT_ID)
            token_resp = await client.post(
                token_url,
                data={
                    "code": code,
                    "client_id": MICROSOFT_CLIENT_ID,
                    "client_secret": MICROSOFT_CLIENT_SECRET,
                    "redirect_uri": MICROSOFT_REDIRECT_URI,
                    "grant_type": "authorization_code",
                    "scope": "openid email profile User.Read",
                },
            )
            if token_resp.status_code != 200:
                raise RuntimeError(f"Microsoft token exchange failed: {token_resp.text}")
            access_token = token_resp.json()["access_token"]
            userinfo_resp = await client.get(
                MS_USERINFO_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"}
            )
            if userinfo_resp.status_code != 200:
                raise RuntimeError(f"Microsoft userinfo fetch failed: {userinfo_resp.text}")
            info = userinfo_resp.json()
            email = info.get("email") or info.get("preferred_username")
            if not email:
                raise RuntimeError("Microsoft profile did not include an email address")
            return {
                "email": email,
                "name": info.get("name") or email.split("@")[0],
                "provider_sub": info.get("sub") or email,
            }
    raise ValueError(f"Unknown provider: {provider}")

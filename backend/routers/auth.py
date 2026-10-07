import html
import os
from enum import Enum

from fastapi import APIRouter, Depends, Form, HTTPException, Query, status
from fastapi.responses import HTMLResponse, RedirectResponse

import oauth_providers as oauth
from db import delete_user, get_conn, upsert_user
from deps import get_current_user
from security import create_access_token

router = APIRouter(prefix="/auth", tags=["auth"])

FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:3000")

# In-memory CSRF state store: state -> provider. Fine for a single backend
# instance; a multi-instance deployment would back this with shared storage.
_PENDING_STATES: dict[str, str] = {}


class Provider(str, Enum):
    google = "google"
    microsoft = "microsoft"


@router.get("/{provider}/login")
def login(provider: Provider):
    use_mock = oauth.use_mock(provider.value)
    # Mock off and no (or only half of the) credentials: never send the user
    # to the provider with an empty client_id -- say clearly what's missing.
    if not use_mock and not oauth.is_configured(provider.value):
        prefix = provider.value.upper()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"{provider.value.capitalize()} sign-in is not configured on this server. "
                f"Set {prefix}_CLIENT_ID and {prefix}_CLIENT_SECRET in the backend environment."
            ),
        )
    state = oauth.new_state()
    _PENDING_STATES[state] = provider.value
    if use_mock:
        return RedirectResponse(f"/auth/{provider.value}/mock?state={state}", status_code=status.HTTP_302_FOUND)
    url = oauth.build_authorization_url(provider.value, state)
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)


def _require_mock(provider: Provider):
    # The mock accepts any name/email, so it must never be reachable once a
    # real OAuth app is configured (or the mock is disabled) -- otherwise it
    # would be a way to sign in as anyone without the provider.
    if not oauth.use_mock(provider.value):
        raise HTTPException(status_code=404, detail="Mock sign-in is not available.")


@router.get("/{provider}/mock", response_class=HTMLResponse)
def mock_consent_screen(provider: Provider, state: str = Query(...)):
    _require_mock(provider)
    if _PENDING_STATES.get(state) != provider.value:
        raise HTTPException(status_code=400, detail="Unknown or expired sign-in attempt.")
    provider_label = "Google" if provider == Provider.google else "Microsoft"
    safe_state = html.escape(state, quote=True)
    # Styled with the same Crownwright gold tokens as the frontend, light and
    # dark, so the dev sign-in step doesn't look like a different product.
    return f"""<!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Sign in with {provider_label} (dev mode) | Crownwright</title>
        <style>
          :root {{ color-scheme: light dark; --bg:#ffffff; --surface:#ffffff; --ink:#1c1917; --muted:#57534e;
            --line:#d9c38c; --warn-bg:#fffbeb; --warn:#92400e; --focus:#b45309; }}
          @media (prefers-color-scheme: dark) {{ :root {{ --bg:#0b0b10; --surface:#14141b; --ink:#f6f3ec;
            --muted:#bcb6ab; --line:#453f4f; --warn-bg:rgba(217,119,6,.16); --warn:#fcd34d; --focus:#fcd34d; }} }}
          * {{ box-sizing: border-box; }}
          body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center; padding:16px;
            background:var(--bg); color:var(--ink); font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif; }}
          main {{ width:100%; max-width:420px; background:var(--surface); border:1px solid var(--line); border-radius:16px; padding:24px; }}
          .mark {{ width:48px; height:48px; border-radius:14px; display:flex; align-items:center; justify-content:center;
            background:linear-gradient(135deg,#FCD34D,#EAB308 50%,#D97706); color:#451A03; font-size:24px; }}
          h1 {{ font-size:20px; margin:16px 0 4px; }}
          .brand {{ margin:0; font-size:12px; font-weight:700; letter-spacing:.06em; text-transform:uppercase; color:var(--muted); }}
          .note {{ background:var(--warn-bg); color:var(--warn); border-radius:10px; padding:10px 12px; font-size:14px; }}
          label {{ display:block; font-size:14px; font-weight:600; margin-top:12px; }}
          input {{ width:100%; margin-top:4px; min-height:44px; padding:8px 12px; border-radius:10px; border:1px solid var(--line);
            background:var(--bg); color:var(--ink); font-size:16px; }}
          input:focus, button:focus-visible {{ outline:3px solid var(--focus); outline-offset:2px; }}
          button {{ margin-top:20px; width:100%; min-height:44px; border:0; border-radius:10px; cursor:pointer; font-weight:700;
            font-size:15px; color:#451A03; background:linear-gradient(135deg,#FCD34D,#EAB308 50%,#D97706); }}
        </style>
      </head>
      <body>
        <main>
          <div class="mark" aria-hidden="true">&#9819;</div>
          <h1>Sign in with {provider_label}</h1>
          <p class="brand">Crownwright &middot; Crown AI</p>
          <p class="note">
            Developer mode: no {provider_label} OAuth app is configured
            ({provider.value.upper()}_CLIENT_ID / _SECRET). Enter any name/email
            below to simulate a successful {provider_label} sign-in. Set real
            credentials in the environment to use actual {provider_label} login.
          </p>
          <form method="post" action="/auth/{provider.value}/mock">
            <input type="hidden" name="state" value="{safe_state}" />
            <label for="name">Name</label>
            <input id="name" name="name" required autocomplete="name" />
            <label for="email">Email</label>
            <input id="email" name="email" type="email" required autocomplete="email" />
            <button type="submit">Continue</button>
          </form>
        </main>
      </body>
    </html>
    """


@router.post("/{provider}/mock")
def mock_consent_submit(provider: Provider, state: str = Form(...), name: str = Form(...), email: str = Form(...)):
    _require_mock(provider)
    if _PENDING_STATES.pop(state, None) != provider.value:
        raise HTTPException(status_code=400, detail="Unknown or expired sign-in attempt.")
    with get_conn() as conn:
        user = upsert_user(conn, email=email, name=name, provider=provider.value, provider_sub=email)
    token = create_access_token(user["id"], user["email"])
    return RedirectResponse(
        f"{FRONTEND_URL}/crown-ai/callback?token={token}", status_code=status.HTTP_302_FOUND
    )


@router.get("/{provider}/callback")
async def oauth_callback(provider: Provider, code: str = Query(...), state: str = Query(...)):
    if _PENDING_STATES.pop(state, None) != provider.value:
        raise HTTPException(status_code=400, detail="Unknown or expired sign-in attempt (possible CSRF).")
    try:
        profile = await oauth.exchange_code_for_profile(provider.value, code)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"OAuth provider error: {exc}")
    with get_conn() as conn:
        user = upsert_user(
            conn,
            email=profile["email"],
            name=profile["name"],
            provider=provider.value,
            provider_sub=profile["provider_sub"],
        )
    token = create_access_token(user["id"], user["email"])
    return RedirectResponse(
        f"{FRONTEND_URL}/crown-ai/callback?token={token}", status_code=status.HTTP_302_FOUND
    )


@router.get("/me")
def me(current_user: dict = Depends(get_current_user)):
    return {
        "id": current_user["id"],
        "email": current_user["email"],
        "name": current_user["name"],
        "provider": current_user["provider"],
        "tier": current_user["tier"],
    }


@router.delete("/me", status_code=204)
def delete_me(current_user: dict = Depends(get_current_user)):
    """Data-rights erasure (DPDP / GDPR / CCPA): permanently deletes the
    caller's account and all of their Crown AI projects and artifacts. The
    token stops working immediately because its user no longer exists."""
    with get_conn() as conn:
        delete_user(conn, current_user["id"])
    return None

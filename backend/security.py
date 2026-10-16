"""Authentication, authorisation, OTPs and step-up (transaction) MFA.

Tokens are opaque random strings; only their SHA-256 is stored. A user token is bound
to a ``sessions`` row: the session must be ``active`` and must have seen activity
within ``SESSION_IDLE_SECONDS`` (5 minutes by default), and each authenticated request
extends it. Only one session per user stays active: completing MFA supersedes the rest.

Scopes:
  ``mfa_enrolment``  issued by /auth/register (password level, must enrol a factor)
  ``mfa_pending``    issued by /auth/login (password level, must complete MFA)
  ``full``           issued by /auth/mfa/verify for login
  ``onboarding``     issued by /onboarding/* for one application only

Password-level tokens authenticate the caller for every bearer endpoint, while money
movement and control changes need a single-use ``mfa_token`` from a transaction
challenge. With ``REQUIRE_MFA_SESSION=true`` (recommended in production), customer and
staff endpoints also refuse password-level sessions.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, Request

from core import (MOCK_OTP_CODE, api_error, forbidden, iso, jdump, jload, mask_mobile, new_id, now_iso, parse_iso,
                  utcnow)
from db import get_db

SESSION_IDLE_SECONDS = int(os.getenv("SESSION_IDLE_SECONDS", "300"))
SESSION_WARNING_SECONDS = 60
SESSION_MAX_HOURS = 12
REQUIRE_MFA_SESSION = os.getenv("REQUIRE_MFA_SESSION", "false").lower() == "true"
OTP_TTL_SECONDS = 300
OTP_MAX_SENDS = 5
OTP_SEND_WINDOW_MINUTES = 15
OTP_MAX_ATTEMPTS = 5

OTP_PURPOSES = {
    "registration", "complaint", "complaint_track", "fraud_report", "onboarding", "onboarding_resume",
    "profile_change", "beneficiary", "login",
}

STAFF_ROLES = {
    "content_maker", "content_checker", "legal_approver", "compliance_reviewer", "grievance_officer", "nodal_officer",
    "principal_nodal_officer", "internal_ombudsman", "relationship_manager", "vcip_officer", "ops_admin", "soc_analyst",
    "integration_approver",
}

_hasher = PasswordHasher()  # Argon2id with library defaults


# ------------------------------------------------------------------ passwords
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_problem(password: str) -> str | None:
    if not isinstance(password, str) or len(password) < 8:
        return "Use at least 8 characters"
    if len(password) > 128:
        return "Use at most 128 characters"
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        return "Use letters and at least one number"
    return None


# ------------------------------------------------------------------ tokens
def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def code_hash(code: str, salt: str) -> str:
    return hmac.new(salt.encode("utf-8"), code.encode("utf-8"), hashlib.sha256).hexdigest()


def mint_user_token(conn: sqlite3.Connection, user_id: str, session_id: str, scope: str) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    expires = iso(utcnow() + timedelta(hours=SESSION_MAX_HOURS))
    conn.execute(
        """INSERT INTO access_tokens (token_hash, kind, user_id, session_id, scope, created_at, expires_at)
           VALUES (?, 'user', ?, ?, ?, ?, ?)""",
        (token_hash(token), user_id, session_id, scope, now_iso(), expires),
    )
    return token, expires  # type: ignore[return-value]


def mint_onboarding_token(conn: sqlite3.Connection, application_id: str, minutes: int = 60) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    expires = iso(utcnow() + timedelta(minutes=minutes))
    conn.execute(
        """INSERT INTO access_tokens (token_hash, kind, application_id, scope, created_at, expires_at)
           VALUES (?, 'onboarding', ?, 'onboarding', ?, ?)""",
        (token_hash(token), application_id, now_iso(), expires),
    )
    return token, expires  # type: ignore[return-value]


def create_session(conn: sqlite3.Connection, user_id: str, ip: str | None, device: str | None, risk_score: int,
                   mfa_level: str = "password_only") -> str:
    sid = new_id()
    now = now_iso()
    conn.execute(
        """INSERT INTO sessions (id, user_id, mfa_level, risk_score, device_fingerprint, ip, created_at, last_activity_at, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')""",
        (sid, user_id, mfa_level, risk_score, device, ip, now, now),
    )
    return sid


def end_session(conn: sqlite3.Connection, session_id: str, status: str, reason: str) -> None:
    conn.execute("UPDATE sessions SET status = ?, ended_at = ?, ended_reason = ? WHERE id = ? AND status = 'active'",
                 (status, now_iso(), reason, session_id))
    conn.execute("UPDATE access_tokens SET revoked_at = ? WHERE session_id = ? AND revoked_at IS NULL", (now_iso(), session_id))


def session_expires_at(last_activity_at: str) -> str:
    return iso(parse_iso(last_activity_at) + timedelta(seconds=SESSION_IDLE_SECONDS))  # type: ignore[operator]


# ------------------------------------------------------------------ auth context
@dataclass
class AuthContext:
    kind: str  # 'user' | 'onboarding'
    scope: str
    token_hash: str
    user: dict = field(default_factory=dict)
    session: dict = field(default_factory=dict)
    application_id: str | None = None

    @property
    def user_id(self) -> str | None:
        return self.user.get("id")

    @property
    def user_type(self) -> str | None:
        return self.user.get("user_type")

    @property
    def customer_id(self) -> str | None:
        return self.user.get("customer_id")

    @property
    def corporate_id(self) -> str | None:
        return self.user.get("corporate_id")

    @property
    def session_id(self) -> str | None:
        return self.session.get("id")

    @property
    def roles(self) -> list[str]:
        return jload(self.user.get("staff_roles"), []) or []

    @property
    def mfa_complete(self) -> bool:
        return self.session.get("mfa_level") == "mfa_complete"

    @property
    def actor_type(self) -> str:
        if self.kind == "onboarding":
            return "visitor"
        return "staff" if self.user_type == "staff" else "customer"

    def has_role(self, *roles: str) -> bool:
        return any(r in self.roles for r in roles)


def _unauthenticated(message: str = "Sign in to continue", code: str = "unauthenticated"):
    exc = api_error(401, code, message)
    exc.headers = {"WWW-Authenticate": "Bearer"}
    return exc


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        return None
    token = header[7:].strip()
    return token or None


def _resolve(request: Request, conn: sqlite3.Connection, touch: bool) -> AuthContext:
    token = _bearer(request)
    if not token:
        raise _unauthenticated()
    th = token_hash(token)
    tok = conn.execute("SELECT * FROM access_tokens WHERE token_hash = ?", (th,)).fetchone()
    if not tok or tok["revoked_at"]:
        raise _unauthenticated("Your session has ended. Sign in again.", "token_invalid")
    now = utcnow()
    if parse_iso(tok["expires_at"]) <= now:  # type: ignore[operator]
        raise _unauthenticated("Your session has expired. Sign in again.", "session_expired")
    if tok["kind"] == "onboarding":
        return AuthContext(kind="onboarding", scope="onboarding", token_hash=th, application_id=tok["application_id"])
    sess = conn.execute("SELECT * FROM sessions WHERE id = ?", (tok["session_id"],)).fetchone()
    if not sess or sess["status"] != "active":
        reason = sess["status"] if sess else "missing"
        msg = ("You were signed out because you signed in on another device." if reason == "superseded"
               else "Your session has ended. Sign in again.")
        raise _unauthenticated(msg, "session_superseded" if reason == "superseded" else "session_expired")
    if parse_iso(sess["last_activity_at"]) + timedelta(seconds=SESSION_IDLE_SECONDS) <= now:  # type: ignore[operator]
        end_session(conn, sess["id"], "expired", "idle_timeout")
        from db import commit_now

        commit_now(conn)
        raise _unauthenticated("You were signed out after 5 minutes of inactivity.", "session_expired")
    user = conn.execute("SELECT * FROM users WHERE id = ?", (tok["user_id"],)).fetchone()
    if not user or user["status"] == "disabled":
        raise _unauthenticated("This user is disabled.", "token_invalid")
    session = dict(sess)
    if touch:
        session["last_activity_at"] = iso(now)
        conn.execute("UPDATE sessions SET last_activity_at = ? WHERE id = ?", (session["last_activity_at"], sess["id"]))
    return AuthContext(kind="user", scope=tok["scope"], token_hash=th, user=dict(user), session=session)


def auth_optional(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> AuthContext | None:
    if not _bearer(request):
        return None
    try:
        return _resolve(request, conn, touch=True)
    except Exception:
        return None


def require_auth(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> AuthContext:
    """Any signed-in user (customer, corporate or staff), any user scope."""
    ctx = _resolve(request, conn, touch=True)
    if ctx.kind != "user":
        raise forbidden("This token can only be used for the onboarding application", "wrong_token_kind")
    return ctx


def require_auth_no_touch(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> AuthContext:
    """Like ``require_auth`` but does not extend the idle timer (used by GET /auth/session)."""
    ctx = _resolve(request, conn, touch=False)
    if ctx.kind != "user":
        raise forbidden("This token can only be used for the onboarding application", "wrong_token_kind")
    return ctx


def _check_mfa_session(ctx: AuthContext) -> None:
    if REQUIRE_MFA_SESSION and not ctx.mfa_complete:
        raise forbidden("Complete two-factor verification to continue", "mfa_required")


def require_customer(ctx: AuthContext = Depends(require_auth)) -> AuthContext:
    """A retail customer (or NRI/HNI) user linked to a Customer record."""
    if ctx.user_type not in ("retail",) or not ctx.customer_id:
        raise forbidden("This service is available to retail banking customers only", "customer_only")
    _check_mfa_session(ctx)
    return ctx


def require_corporate(ctx: AuthContext = Depends(require_auth)) -> AuthContext:
    if ctx.user_type != "corporate" or not ctx.corporate_id:
        raise forbidden("This service is available to corporate users only", "corporate_only")
    _check_mfa_session(ctx)
    return ctx


def require_staff(*roles: str):
    """Dependency factory: a staff user holding at least one of ``roles`` (any staff role if none given)."""

    def dep(ctx: AuthContext = Depends(require_auth)) -> AuthContext:
        if ctx.user_type != "staff":
            raise forbidden("Staff access only", "staff_only")
        if roles and not ctx.has_role(*roles):
            raise forbidden(f"Requires one of the roles: {', '.join(roles)}", "role_required")
        _check_mfa_session(ctx)
        return ctx

    return dep


def require_onboarding(application_id: str, request: Request, conn: sqlite3.Connection = Depends(get_db)) -> AuthContext:
    """Onboarding-scoped token for exactly this application (403 for another application's token)."""
    ctx = _resolve(request, conn, touch=False)
    if ctx.kind != "onboarding":
        raise forbidden("Use the onboarding access token for this application", "wrong_token_kind")
    if ctx.application_id != application_id:
        exists = conn.execute("SELECT 1 FROM onboarding_applications WHERE id = ?", (application_id,)).fetchone()
        if not exists:
            raise api_error(404, "not_found", "Application not found")
        raise forbidden("This token belongs to a different application")
    return ctx


# ------------------------------------------------------------------ OTP (public flows)
def send_otp(conn: sqlite3.Connection, mobile: str, purpose: str) -> dict:
    """Create an OTP challenge and send it through the SMS gateway. Rate limited per mobile."""
    from integrations import sms_gateway
    from integrations.base import is_mock

    since = iso(utcnow() - timedelta(minutes=OTP_SEND_WINDOW_MINUTES))
    sent = conn.execute("SELECT COUNT(*) FROM otp_challenges WHERE mobile = ? AND created_at >= ?", (mobile, since)).fetchone()[0]
    if sent >= OTP_MAX_SENDS:
        raise api_error(429, "otp_rate_limited", f"Too many OTP requests. Try again in {OTP_SEND_WINDOW_MINUTES} minutes.")
    mock = is_mock(conn, "sms_gateway")
    code = MOCK_OTP_CODE if mock else f"{secrets.randbelow(1_000_000):06d}"
    cid = new_id()
    expires = iso(utcnow() + timedelta(seconds=OTP_TTL_SECONDS))
    conn.execute(
        """INSERT INTO otp_challenges (id, mobile, purpose, code_hash, created_at, expires_at) VALUES (?,?,?,?,?,?)""",
        (cid, mobile, purpose, code_hash(code, cid), now_iso(), expires),
    )
    sms_gateway.get(conn).send_sms(mobile, f"{code} is your OTP. Valid for 5 minutes. Never share it with anyone, including bank staff.")
    out = {"otp_ref": cid, "expires_at": expires, "resend_after_seconds": 30, "masked_mobile": mask_mobile(mobile), "mock": mock}
    if mock:
        out["mock_otp"] = MOCK_OTP_CODE
        out["mock_label"] = "MOCK SMS gateway: no SMS is sent; use the OTP shown"
    return out


def check_otp(conn: sqlite3.Connection, mobile: str, purpose: str, code: str | None) -> bool:
    """Verify an OTP for (mobile, purpose). Consumes the challenge on success."""
    from integrations.base import is_mock

    code = (code or "").strip()
    if not code:
        return False
    now = now_iso()
    ch = conn.execute(
        """SELECT * FROM otp_challenges WHERE mobile = ? AND purpose = ? AND consumed_at IS NULL AND expires_at > ?
           ORDER BY created_at DESC LIMIT 1""",
        (mobile, purpose, now),
    ).fetchone()
    if ch:
        if ch["attempts"] >= OTP_MAX_ATTEMPTS:
            return False
        if hmac.compare_digest(code_hash(code, ch["id"]), ch["code_hash"]):
            conn.execute("UPDATE otp_challenges SET consumed_at = ? WHERE id = ?", (now, ch["id"]))
            return True
        conn.execute("UPDATE otp_challenges SET attempts = attempts + 1 WHERE id = ?", (ch["id"],))
        return False
    # Sandbox: with the mock SMS gateway the well-known mock OTP is accepted without a prior send.
    return is_mock(conn, "sms_gateway") and hmac.compare_digest(code, MOCK_OTP_CODE)


def require_otp(conn: sqlite3.Connection, mobile: str, purpose: str, code: str | None, field_name: str = "otp") -> None:
    if not check_otp(conn, mobile, purpose, code):
        from db import commit_now

        commit_now(conn)  # keep the attempt counter
        raise api_error(422, "otp_invalid", "The OTP is incorrect or has expired. Request a new OTP.", field=field_name)


# ------------------------------------------------------------------ step-up MFA tokens
def mint_mfa_token(conn: sqlite3.Connection, user_id: str, session_id: str | None, purpose: str = "transaction") -> tuple[str, str]:
    token = "mfa_" + secrets.token_urlsafe(24)
    expires = iso(utcnow() + timedelta(minutes=5))
    conn.execute(
        """INSERT INTO mfa_tokens (token_hash, user_id, session_id, purpose, created_at, expires_at) VALUES (?,?,?,?,?,?)""",
        (token_hash(token), user_id, session_id, purpose, now_iso(), expires),
    )
    return token, expires  # type: ignore[return-value]


def consume_mfa_token(conn: sqlite3.Connection, ctx: AuthContext, mfa_token: str | None, used_for: str) -> None:
    """Validate and burn a single-use step-up token. 403 ``mfa_invalid`` if missing, used, expired or foreign."""
    if not mfa_token:
        raise api_error(422, "mfa_required", "Verify with your second factor to continue", field="mfa_token")
    row = conn.execute("SELECT * FROM mfa_tokens WHERE token_hash = ?", (token_hash(mfa_token),)).fetchone()
    if (not row or row["user_id"] != ctx.user_id or row["used_at"]
            or parse_iso(row["expires_at"]) <= utcnow()):  # type: ignore[operator]
        raise api_error(403, "mfa_invalid", "Second-factor verification is invalid or has expired. Verify again.", field="mfa_token")
    conn.execute("UPDATE mfa_tokens SET used_at = ?, used_for = ? WHERE token_hash = ?", (now_iso(), used_for, row["token_hash"]))


def user_public(user: dict) -> dict:
    return {
        "id": user["id"],
        "user_type": user["user_type"],
        "email": user["email"],
        "display_name": user.get("display_name"),
        "staff_roles": jload(user.get("staff_roles"), []),
        "status": user["status"],
    }


__all__ = [
    "AuthContext", "auth_optional", "require_auth", "require_auth_no_touch", "require_customer", "require_corporate",
    "require_staff", "require_onboarding", "send_otp", "check_otp", "require_otp", "mint_mfa_token", "consume_mfa_token",
    "hash_password", "verify_password", "password_problem", "mint_user_token", "mint_onboarding_token", "create_session",
    "end_session", "session_expires_at", "SESSION_IDLE_SECONDS", "SESSION_WARNING_SECONDS", "STAFF_ROLES", "OTP_PURPOSES",
    "jdump",
]

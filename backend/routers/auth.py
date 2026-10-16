"""Internet banking registration, password login, MFA, sessions and public OTPs."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict

from audit import audit
from banking import upsert_customer_from_finacle
from core import (ACCOUNT_RE, MOCK_OTP_CODE, api_error, client_ip, invalid, iso, jload, mask_email, mask_mobile,
                  new_id, normalize_mobile, now_iso, one, parse_iso, require_mobile, utcnow, valid_email)
from db import commit_now, get_db
from integrations import finacle
from integrations.base import is_mock, mock_meta, resolve
from notify import notify
from security import (OTP_PURPOSES, SESSION_IDLE_SECONDS, SESSION_WARNING_SECONDS, AuthContext, code_hash, create_session,
                      end_session, hash_password, mint_mfa_token, mint_user_token, password_problem, require_auth,
                      require_auth_no_touch, require_otp, send_otp, session_expires_at, verify_password)

router = APIRouter(tags=["auth"])

LOCKOUT_THRESHOLD = 5
LOCKOUT_WINDOW_MINUTES = 15
FACTOR_TYPES = ("passkey", "totp", "push", "sms_otp")
LANGUAGES = ("en", "hi", "ta", "te", "kn", "mr", "bn", "gu", "ml")


class Body(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class OtpSendIn(Body):
    mobile: str
    purpose: str


class RegisterIn(Body):
    email: str
    password: str
    mobile: str
    account_number: str
    otp: str
    preferred_language: Optional[str] = "en"


class LoginIn(Body):
    email: str
    password: str


class ChallengeIn(Body):
    factor_type: str
    purpose: str


class VerifyIn(Body):
    challenge_id: str
    response: str


class FactorIn(Body):
    factor_type: str
    attestation: str
    label: Optional[str] = None


# ------------------------------------------------------------------ helpers
def _factors(conn: sqlite3.Connection, user: dict) -> list[dict]:
    rows = [dict(r) for r in conn.execute(
        "SELECT id, factor_type, label, is_primary, created_at FROM mfa_factors WHERE user_id = ? ORDER BY is_primary DESC, created_at",
        (user["id"],))]
    for r in rows:
        r["is_primary"] = bool(r["is_primary"])
    if not any(r["factor_type"] == "sms_otp" for r in rows):
        rows.append({"id": "registered-mobile", "factor_type": "sms_otp", "label": f"SMS to {mask_mobile(user['mobile'])}",
                     "is_primary": not rows, "created_at": user["created_at"]})
    return rows


def _corporate_role(conn: sqlite3.Connection, user: dict) -> dict | None:
    if not user.get("corporate_id"):
        return None
    row = one(conn, "SELECT role, approval_limit_paise FROM corporate_user_limits WHERE user_id = ? AND corporate_id = ?",
              (user["id"], user["corporate_id"]))
    return row


def _device(request: Request) -> str:
    ua = request.headers.get("user-agent", "")
    return hashlib.sha256(ua.encode("utf-8")).hexdigest()[:16]


def _risk(conn: sqlite3.Connection, user: dict, ip: str | None, device: str) -> tuple[int, str]:
    score = 10
    if user.get("last_login_ip") and ip and user["last_login_ip"] != ip:
        score += 30
    last = one(conn, "SELECT device_fingerprint FROM sessions WHERE user_id = ? ORDER BY created_at DESC LIMIT 1", (user["id"],))
    if last and last["device_fingerprint"] and last["device_fingerprint"] != device:
        score += 20
    since = iso(utcnow() - timedelta(minutes=LOCKOUT_WINDOW_MINUTES))
    fails = conn.execute("SELECT COUNT(*) FROM login_attempts WHERE user_id = ? AND success = 0 AND at >= ?",
                         (user["id"], since)).fetchone()[0]
    score += min(40, fails * 10)
    level = "low" if score < 40 else "medium" if score < 70 else "high"
    return score, level


def _session_payload(conn: sqlite3.Connection, ctx: AuthContext) -> dict:
    user = ctx.user
    sess = ctx.session
    corp = _corporate_role(conn, user)
    return {
        "session_id": sess["id"],
        "user_id": user["id"],
        "user_type": user["user_type"],
        "display_name": user.get("display_name"),
        "email_masked": mask_email(user["email"]),
        "mobile_masked": mask_mobile(user["mobile"]),
        "staff_roles": jload(user.get("staff_roles"), []),
        "customer_id": user.get("customer_id"),
        "corporate_id": user.get("corporate_id"),
        "corporate_role": corp["role"] if corp else None,
        "scope": ctx.scope,
        "mfa_level": sess["mfa_level"],
        "risk_score": sess["risk_score"],
        "created_at": sess["created_at"],
        "last_activity_at": sess["last_activity_at"],
        "expires_at": session_expires_at(sess["last_activity_at"]),
        "idle_timeout_seconds": SESSION_IDLE_SECONDS,
        "warning_seconds": SESSION_WARNING_SECONDS,
        "last_login_at": user.get("last_login_at"),
        "last_login_ip": user.get("last_login_ip"),
        "preferred_language": user.get("preferred_language") or "en",
        "senior_mode": bool(user.get("senior_mode")),
        "has_strong_factor": bool(conn.execute(
            "SELECT 1 FROM mfa_factors WHERE user_id = ? AND factor_type != 'sms_otp'", (user["id"],)).fetchone()),
    }


# ------------------------------------------------------------------ OTP
@router.post("/otp/send")
def otp_send(body: OtpSendIn, conn: sqlite3.Connection = Depends(get_db)):
    mobile = require_mobile(body.mobile)
    if body.purpose not in OTP_PURPOSES:
        raise invalid("purpose", f"Unknown purpose. Use one of: {', '.join(sorted(OTP_PURPOSES))}")
    return send_otp(conn, mobile, body.purpose)


# ------------------------------------------------------------------ registration
@router.post("/auth/register", status_code=201)
def register(body: RegisterIn, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    email = body.email.lower()
    if not valid_email(email):
        raise invalid("email", "Enter a valid email address")
    problem = password_problem(body.password)
    if problem:
        raise invalid("password", problem)
    mobile = require_mobile(body.mobile)
    acct_no = re.sub(r"\s", "", body.account_number)
    if not ACCOUNT_RE.match(acct_no):
        raise invalid("account_number", "Enter your account number (6 to 20 digits)")
    lang = body.preferred_language if body.preferred_language in LANGUAGES else "en"
    if one(conn, "SELECT id FROM users WHERE email = ?", (email,)):
        raise api_error(409, "already_registered", "This email is already registered for internet banking. Sign in instead.")
    require_otp(conn, mobile, "registration", body.otp)

    core = finacle.get(conn)
    found = core.lookup_by_account(acct_no, mobile)
    if not found["mobile_matches"]:
        raise invalid("mobile", "This mobile number is not the one registered for this account. Visit a branch to update it.",
                      code="mobile_mismatch")
    customer = upsert_customer_from_finacle(conn, found["cif"])
    if one(conn, "SELECT id FROM users WHERE customer_id = ?", (customer["id"],)):
        raise api_error(409, "already_registered", "Internet banking is already registered for this account. Sign in instead.")

    user_id = new_id()
    conn.execute(
        """INSERT INTO users (id, user_type, email, mobile, password_hash, display_name, customer_id, preferred_language, created_at)
           VALUES (?, 'retail', ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, email, mobile, hash_password(body.password), customer["full_name"], customer["id"], lang, now_iso()),
    )
    ip = client_ip(request)
    sid = create_session(conn, user_id, ip, _device(request), 10)
    token, expires = mint_user_token(conn, user_id, sid, "mfa_enrolment")
    audit(conn, "login", "internet_banking.registered", actor_type="customer", actor_id=user_id, entity_type="user",
          entity_id=user_id, details={"cif": found["cif"], "mobile": mask_mobile(mobile)}, ip=ip)
    notify(conn, user_id, "security", "Internet banking registered",
           "Your internet banking registration is complete. If this was not you, call the bank or report fraud immediately.")
    return {
        "access_token": token,
        "token_type": "bearer",
        "scope": "mfa_enrolment",
        "user_id": user_id,
        "expires_at": expires,
        "mfa_level": "password_only",
        "next_step": "enrol_mfa",
        "mock": mock_meta("finacle") if found.get("mock") else None,
    }


# ------------------------------------------------------------------ login
@router.post("/auth/login")
def login(body: LoginIn, request: Request, conn: sqlite3.Connection = Depends(get_db)):
    email = body.email.lower()
    ip = client_ip(request)
    user = one(conn, "SELECT * FROM users WHERE email = ?", (email,))
    now = utcnow()
    if user and user["locked_until"] and parse_iso(user["locked_until"]) > now:  # type: ignore[operator]
        raise api_error(423, "account_locked",
                        "Too many failed attempts. Your login is locked for 15 minutes, or reset your password.",
                        locked_until=user["locked_until"])
    if user and user["status"] == "disabled":
        raise api_error(401, "invalid_credentials", "Incorrect user ID or password")
    if not user or not verify_password(user["password_hash"], body.password):
        conn.execute("INSERT INTO login_attempts (id, email, user_id, success, ip, at) VALUES (?,?,?,0,?,?)",
                     (new_id(), email, user["id"] if user else None, ip, now_iso()))
        remaining = None
        if user:
            since = iso(now - timedelta(minutes=LOCKOUT_WINDOW_MINUTES))
            fails = conn.execute("SELECT COUNT(*) FROM login_attempts WHERE user_id = ? AND success = 0 AND at >= ?",
                                 (user["id"], since)).fetchone()[0]
            conn.execute("UPDATE users SET failed_login_count = ? WHERE id = ?", (fails, user["id"]))
            remaining = max(0, LOCKOUT_THRESHOLD - fails)
            if fails >= LOCKOUT_THRESHOLD:
                until = iso(now + timedelta(minutes=LOCKOUT_WINDOW_MINUTES))
                conn.execute("UPDATE users SET locked_until = ? WHERE id = ?", (until, user["id"]))
                audit(conn, "login", "login.locked", actor_type="system", entity_type="user", entity_id=user["id"],
                      details={"failures": fails}, ip=ip)
                notify(conn, user["id"], "security", "Login locked",
                       "We locked internet banking login after 5 failed attempts. If this was not you, report fraud now.")
                commit_now(conn)
                raise api_error(423, "account_locked", "Too many failed attempts. Your login is locked for 15 minutes.",
                                locked_until=until)
        audit(conn, "login", "login.failed", actor_type="visitor", entity_type="user", entity_id=user["id"] if user else None,
              details={"email": mask_email(email)}, ip=ip)
        commit_now(conn)
        raise api_error(401, "invalid_credentials", "Incorrect user ID or password", attempts_remaining=remaining)

    conn.execute("INSERT INTO login_attempts (id, email, user_id, success, ip, at) VALUES (?,?,?,1,?,?)",
                 (new_id(), email, user["id"], ip, now_iso()))
    conn.execute("UPDATE users SET failed_login_count = 0, locked_until = NULL WHERE id = ?", (user["id"],))
    device = _device(request)
    score, level = _risk(conn, user, ip, device)
    sid = create_session(conn, user["id"], ip, device, score)
    token, expires = mint_user_token(conn, user["id"], sid, "mfa_pending")
    audit(conn, "login", "login.password_ok", actor_type="staff" if user["user_type"] == "staff" else "customer",
          actor_id=user["id"], entity_type="session", entity_id=sid, details={"risk_score": score}, ip=ip)
    factors = _factors(conn, user)
    return {
        "access_token": token,
        "token_type": "bearer",
        "scope": "mfa_pending",
        "user_id": user["id"],
        "user_type": user["user_type"],
        "expires_at": expires,
        "available_factors": factors,
        "risk_level": level,
        "risk_score": score,
        "next_step": "mfa",
        "mock_otp_hint": MOCK_OTP_CODE if is_mock(conn, "sms_gateway") else None,
    }


# ------------------------------------------------------------------ MFA
class _MockAuthenticator:
    """Mock HSM / authenticator: TOTP, push and passkey assertions verify against the mock code."""

    is_mock = True

    def issue(self, factor_type: str) -> str:
        return MOCK_OTP_CODE


@router.post("/auth/mfa/challenge")
def mfa_challenge(body: ChallengeIn, ctx: AuthContext = Depends(require_auth), conn: sqlite3.Connection = Depends(get_db)):
    if body.factor_type not in FACTOR_TYPES:
        raise invalid("factor_type", "Choose passkey, totp, push or sms_otp")
    if body.purpose not in ("login", "transaction"):
        raise invalid("purpose", "Purpose must be login or transaction")
    factor = None
    if body.factor_type != "sms_otp":
        factor = one(conn, "SELECT * FROM mfa_factors WHERE user_id = ? AND factor_type = ? ORDER BY is_primary DESC LIMIT 1",
                     (ctx.user_id, body.factor_type))
        if not factor:
            raise invalid("factor_type", "This factor is not enrolled yet. Choose another factor or enrol it in your profile.",
                          code="factor_not_enrolled")
        mock = is_mock(conn, "hsm")
        code = resolve(conn, "hsm", _MockAuthenticator).issue(body.factor_type)
    else:
        mock = is_mock(conn, "sms_gateway")
        code = MOCK_OTP_CODE if mock else f"{secrets.randbelow(1_000_000):06d}"
    cid = new_id()
    expires = iso(utcnow() + timedelta(minutes=5))
    conn.execute(
        """INSERT INTO mfa_challenges (id, user_id, session_id, factor_type, factor_id, purpose, code_hash, created_at, expires_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (cid, ctx.user_id, ctx.session_id, body.factor_type, factor["id"] if factor else None, body.purpose,
         code_hash(code, cid), now_iso(), expires),
    )
    out: dict = {"challenge_id": cid, "factor_type": body.factor_type, "purpose": body.purpose, "expires_at": expires, "mock": mock}
    if body.factor_type == "sms_otp":
        from integrations import sms_gateway

        sms_gateway.get(conn).send_sms(ctx.user["mobile"], f"{code} is your verification code. Never share it.")
        out["delivery"] = f"OTP sent by SMS to {mask_mobile(ctx.user['mobile'])}"
    elif body.factor_type == "passkey":
        out["delivery"] = "Confirm with your passkey"
        out["webauthn_options"] = {"challenge": secrets.token_urlsafe(32), "rpId": "example.bank.in", "timeout": 60000,
                                   "userVerification": "required", "allowCredentials": [{"type": "public-key", "id": factor["credential_ref"]}]}  # type: ignore[index]
    elif body.factor_type == "push":
        out["delivery"] = f"Approve the request on {factor['label'] or 'your registered device'}"  # type: ignore[index]
    else:
        out["delivery"] = "Enter the 6-digit code from your authenticator app"
    if mock:
        out["mock_code"] = MOCK_OTP_CODE
        out["mock_label"] = "MOCK authenticator: use the code shown"
    return out


@router.post("/auth/mfa/verify")
def mfa_verify(body: VerifyIn, request: Request, ctx: AuthContext = Depends(require_auth), conn: sqlite3.Connection = Depends(get_db)):
    ch = one(conn, "SELECT * FROM mfa_challenges WHERE id = ?", (body.challenge_id,))
    if not ch or ch["user_id"] != ctx.user_id:
        raise api_error(404, "not_found", "Challenge not found. Start verification again.")
    if ch["consumed_at"] or parse_iso(ch["expires_at"]) <= utcnow() or ch["attempts"] >= 5:  # type: ignore[operator]
        raise api_error(401, "mfa_expired", "This verification has expired. Request a new code.")
    if not hmac.compare_digest(code_hash(body.response.strip(), ch["id"]), ch["code_hash"]):
        conn.execute("UPDATE mfa_challenges SET attempts = attempts + 1 WHERE id = ?", (ch["id"],))
        audit(conn, "login", "mfa.failed", actor_type=ctx.actor_type, actor_id=ctx.user_id, entity_type="session",
              entity_id=ctx.session_id, details={"factor": ch["factor_type"], "purpose": ch["purpose"]}, ip=client_ip(request))
        commit_now(conn)
        raise api_error(401, "mfa_invalid", "The code is incorrect. Check it and try again.", field="response",
                        attempts_remaining=max(0, 4 - ch["attempts"]))
    conn.execute("UPDATE mfa_challenges SET consumed_at = ? WHERE id = ?", (now_iso(), ch["id"]))
    ip = client_ip(request)

    if ch["purpose"] == "transaction":
        token, expires = mint_mfa_token(conn, ctx.user_id, ctx.session_id)  # type: ignore[arg-type]
        audit(conn, "login", "mfa.step_up", actor_type=ctx.actor_type, actor_id=ctx.user_id, entity_type="session",
              entity_id=ctx.session_id, details={"factor": ch["factor_type"]}, ip=ip)
        return {"mfa_token": token, "purpose": "transaction", "expires_at": expires}

    # Login: upgrade this session, supersede every other active session of the user.
    others = [r["id"] for r in conn.execute(
        "SELECT id FROM sessions WHERE user_id = ? AND status = 'active' AND id != ?", (ctx.user_id, ctx.session_id))]
    for sid in others:
        end_session(conn, sid, "superseded", "new_login")
    conn.execute("UPDATE sessions SET mfa_level = 'mfa_complete', last_activity_at = ? WHERE id = ?", (now_iso(), ctx.session_id))
    conn.execute("UPDATE access_tokens SET revoked_at = ? WHERE token_hash = ?", (now_iso(), ctx.token_hash))
    token, _ = mint_user_token(conn, ctx.user_id, ctx.session_id, "full")  # type: ignore[arg-type]
    previous_at, previous_ip = ctx.user.get("last_login_at"), ctx.user.get("last_login_ip")
    conn.execute("UPDATE users SET last_login_at = ?, last_login_ip = ? WHERE id = ?", (now_iso(), ip, ctx.user_id))
    audit(conn, "login", "login.success", actor_type=ctx.actor_type, actor_id=ctx.user_id, entity_type="session",
          entity_id=ctx.session_id, details={"factor": ch["factor_type"], "superseded_sessions": len(others)}, ip=ip)
    notify(conn, ctx.user_id, "login", "New login",
           f"Login to internet banking from IP {ip or 'unknown'}. Not you? Report fraud immediately or call 1930.")
    return {
        "access_token": token,
        "token_type": "bearer",
        "scope": "full",
        "mfa_level": "mfa_complete",
        "expires_at": session_expires_at(now_iso()),
        "user_type": ctx.user_type,
        "last_login_at": previous_at,
        "last_login_ip": previous_ip,
        "other_sessions_terminated": len(others),
    }


@router.get("/auth/mfa/factors")
def list_factors(ctx: AuthContext = Depends(require_auth), conn: sqlite3.Connection = Depends(get_db)):
    return {"items": _factors(conn, ctx.user)}


@router.post("/auth/mfa/factors", status_code=201)
def enrol_factor(body: FactorIn, request: Request, ctx: AuthContext = Depends(require_auth), conn: sqlite3.Connection = Depends(get_db)):
    if body.factor_type not in FACTOR_TYPES:
        raise invalid("factor_type", "Choose passkey, totp, push or sms_otp")
    if not body.attestation:
        raise invalid("attestation", "Attestation is required")
    mock = is_mock(conn, "hsm")
    if body.factor_type in ("totp", "push", "sms_otp"):
        if mock and body.attestation != MOCK_OTP_CODE:
            raise invalid("attestation", "The verification code is incorrect", code="attestation_invalid")
        if not mock:
            resolve(conn, "hsm", _MockAuthenticator).verify_enrolment(body.factor_type, body.attestation)
    elif len(body.attestation) < 6:
        raise invalid("attestation", "Passkey attestation is invalid", code="attestation_invalid")
    fid = new_id()
    ref = {
        "passkey": "webauthn:" + hashlib.sha256(body.attestation.encode()).hexdigest()[:32],
        "totp": f"hsm://totp/{fid}",
        "push": f"device:{fid[:8]}",
        "sms_otp": f"mobile:{ctx.user['mobile']}",
    }[body.factor_type]
    has_any = conn.execute("SELECT 1 FROM mfa_factors WHERE user_id = ?", (ctx.user_id,)).fetchone()
    default_label = {"passkey": "Passkey", "totp": "Authenticator app", "push": "Mobile app push",
                     "sms_otp": f"SMS to {mask_mobile(ctx.user['mobile'])}"}[body.factor_type]
    factor = {"id": fid, "user_id": ctx.user_id, "factor_type": body.factor_type, "credential_ref": ref,
              "label": (body.label or default_label)[:60], "is_primary": 0 if has_any else 1, "created_at": now_iso()}
    conn.execute(
        """INSERT INTO mfa_factors (id, user_id, factor_type, credential_ref, label, is_primary, created_at)
           VALUES (:id, :user_id, :factor_type, :credential_ref, :label, :is_primary, :created_at)""",
        factor,
    )
    audit(conn, "login", "mfa.factor_enrolled", actor_type=ctx.actor_type, actor_id=ctx.user_id, entity_type="mfa_factor",
          entity_id=fid, details={"factor_type": body.factor_type}, ip=client_ip(request))
    notify(conn, ctx.user_id, "security", "New security factor added",
           f"A {default_label} was added to your internet banking. Not you? Report fraud immediately.")
    return {"factor": {k: factor[k] for k in ("id", "factor_type", "label", "created_at")} | {"is_primary": bool(factor["is_primary"])},
            "mock": mock}


# ------------------------------------------------------------------ session
@router.get("/auth/session")
def get_session(ctx: AuthContext = Depends(require_auth_no_touch), conn: sqlite3.Connection = Depends(get_db)):
    return _session_payload(conn, ctx)


@router.post("/auth/session/keepalive")
def keepalive(ctx: AuthContext = Depends(require_auth)):
    return {"expires_at": session_expires_at(ctx.session["last_activity_at"]), "idle_timeout_seconds": SESSION_IDLE_SECONDS}


@router.post("/auth/logout", status_code=204)
def logout(request: Request, ctx: AuthContext = Depends(require_auth), conn: sqlite3.Connection = Depends(get_db)):
    end_session(conn, ctx.session_id, "terminated", "logout")  # type: ignore[arg-type]
    audit(conn, "login", "logout", actor_type=ctx.actor_type, actor_id=ctx.user_id, entity_type="session",
          entity_id=ctx.session_id, ip=client_ip(request))
    return Response(status_code=204)

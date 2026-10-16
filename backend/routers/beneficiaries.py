"""Beneficiaries (US-20): add with name check and OTP, cooling period with a lower limit, delete.

Account numbers are stored encrypted (``account_number_enc``) and shown masked. The
status ``cooling`` turns into ``active`` once ``cooling_until`` has passed; that change
is applied lazily whenever beneficiaries are read.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict

from audit import audit_ctx
from banking import customer_for, in_cooling_period
from core import (ACCOUNT_RE, IFSC_RE, VPA_RE, api_error, fmt_ist, forbidden, format_inr, get_reg_value, invalid, iso,
                  mask_account, new_id, not_found, now_iso, one, utcnow)
from crypto import decrypt_str, encrypt_str
from db import get_db
from integrations import npci_imps, npci_upi
from notify import notify, outbox
from security import AuthContext, require_customer, require_otp, send_otp

router = APIRouter(tags=["beneficiaries"])

DEFAULT_COOLING_HOURS = 24
DEFAULT_COOLING_LIMIT_PAISE = 5_000_000
COUNTED_TRANSFER_STATUSES = ("initiated", "pending", "success")


class Body(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class BeneficiaryIn(Body):
    payee_type: str
    name: str
    nickname: Optional[str] = None
    account_number: Optional[str] = None
    confirm_account_number: Optional[str] = None
    ifsc: Optional[str] = None
    vpa: Optional[str] = None


class ConfirmIn(Body):
    otp: str


# ------------------------------------------------------------------ helpers (also used by transfers)
def cooling_config(conn: sqlite3.Connection) -> dict:
    hours = get_reg_value(conn, "beneficiary_cooling_hours", DEFAULT_COOLING_HOURS)
    limit = get_reg_value(conn, "beneficiary_cooling_limit_paise", DEFAULT_COOLING_LIMIT_PAISE)
    try:
        hours = int(hours)
    except (TypeError, ValueError):
        hours = DEFAULT_COOLING_HOURS
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_COOLING_LIMIT_PAISE
    return {"cooling_hours": hours, "cooling_limit_paise": limit}


def mask_vpa(vpa: str | None) -> str | None:
    if not vpa or "@" not in vpa:
        return vpa
    local, handle = vpa.split("@", 1)
    shown = local[:2] if len(local) > 3 else local[:1]
    return f"{shown}{'*' * max(3, len(local) - len(shown))}@{handle}"


def refresh_cooling(conn: sqlite3.Connection, customer_id: str) -> None:
    """Lazy state change: cooling -> active once the cooling period is over."""
    conn.execute("UPDATE beneficiaries SET status = 'active' WHERE customer_id = ? AND status = 'cooling' AND cooling_until <= ?",
                 (customer_id, now_iso()))


def cooling_used_paise(conn: sqlite3.Connection, ben: dict) -> int:
    marks = ",".join("?" for _ in COUNTED_TRANSFER_STATUSES)
    row = conn.execute(
        f"""SELECT COALESCE(SUM(amount_paise), 0) FROM transfers WHERE beneficiary_id = ? AND status IN ({marks})
            AND created_at >= ?""",
        (ben["id"], *COUNTED_TRANSFER_STATUSES, ben.get("confirmed_at") or ben["created_at"]),
    ).fetchone()
    return int(row[0] or 0)


def cooling_remaining_paise(conn: sqlite3.Connection, ben: dict) -> int | None:
    """Remaining amount that may be sent during the cooling period (None when not cooling)."""
    if ben["status"] != "cooling":
        return None
    limit = ben.get("cooling_limit_paise")
    if limit is None:
        limit = cooling_config(conn)["cooling_limit_paise"]
    return max(0, int(limit) - cooling_used_paise(conn, ben))


def owned_beneficiary(conn: sqlite3.Connection, customer_id: str, beneficiary_id: str) -> dict:
    """404 if unknown or deleted, 403 if it belongs to another customer."""
    ben = one(conn, "SELECT * FROM beneficiaries WHERE id = ?", (str(beneficiary_id or "")[:64],))
    if not ben or ben["status"] == "deleted":
        raise not_found("Beneficiary")
    if ben["customer_id"] != customer_id:
        raise forbidden("This beneficiary does not belong to you")
    if ben["status"] == "cooling" and ben["cooling_until"] and ben["cooling_until"] <= now_iso():
        conn.execute("UPDATE beneficiaries SET status = 'active' WHERE id = ?", (ben["id"],))
        ben["status"] = "active"
    return ben


def beneficiary_account_number(ben: dict) -> str | None:
    return decrypt_str(ben.get("account_number_enc"))


def public_beneficiary(conn: sqlite3.Connection, ben: dict) -> dict:
    remaining = cooling_remaining_paise(conn, ben)
    status = ben["status"]
    return {
        "id": ben["id"],
        "name": ben["name"],
        "nickname": ben.get("nickname"),
        "payee_type": ben["payee_type"],
        "account_masked": ben.get("account_masked"),
        "ifsc": ben.get("ifsc"),
        "vpa_masked": mask_vpa(ben.get("vpa")),
        "display_id": ben.get("account_masked") if ben["payee_type"] == "bank_account" else mask_vpa(ben.get("vpa")),
        "name_at_bank": ben.get("name_at_bank"),
        "status": status,
        "status_label": ("Pending OTP confirmation" if status == "pending_otp"
                         else f"Cooling until {fmt_ist(ben['cooling_until'])}" if status == "cooling"
                         else "Active" if status == "active" else status),
        "cooling_until": ben.get("cooling_until") if status == "cooling" else None,
        "cooling_limit_paise": ben.get("cooling_limit_paise") if status == "cooling" else None,
        "cooling_remaining_paise": remaining,
        "allowed_rails": ["UPI"] if ben["payee_type"] == "vpa" else ["IMPS", "NEFT", "RTGS"],
        "created_at": ben["created_at"],
        "confirmed_at": ben.get("confirmed_at"),
    }


def _names_match(a: str | None, b: str | None) -> bool:
    def norm(s: str | None) -> set[str]:
        return {w for w in re.sub(r"[^A-Z ]", " ", (s or "").upper()).split() if len(w) > 1}

    wa, wb = norm(a), norm(b)
    return bool(wa and wb and (wa & wb))


# ------------------------------------------------------------------ endpoints
@router.get("/me/beneficiaries")
def list_beneficiaries(ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    refresh_cooling(conn, ctx.customer_id)
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM beneficiaries WHERE customer_id = ? AND status != 'deleted' ORDER BY name COLLATE NOCASE", (ctx.customer_id,))]
    cfg = cooling_config(conn)
    return {
        "items": [public_beneficiary(conn, b) for b in rows],
        "total": len(rows),
        "cooling_hours": cfg["cooling_hours"],
        "cooling_limit_paise": cfg["cooling_limit_paise"],
        "cooling_note": (f"For the first {cfg['cooling_hours']} hours after a beneficiary is added you can send at most "
                         f"{format_inr(cfg['cooling_limit_paise'])} in total to them. This protects you if your account is misused."),
    }


@router.post("/me/beneficiaries", status_code=201)
def add_beneficiary(body: BeneficiaryIn, request: Request, ctx: AuthContext = Depends(require_customer),
                    conn: sqlite3.Connection = Depends(get_db)):
    customer = customer_for(conn, ctx.customer_id)
    if in_cooling_period(customer):
        raise api_error(422, "cooling_period",
                        f"Adding beneficiaries is paused until {fmt_ist(customer['high_risk_cooling_until'])} because your "
                        "contact details were changed recently. This protects your account.")
    payee_type = (body.payee_type or "").strip().lower()
    if payee_type in ("account", "bank"):
        payee_type = "bank_account"
    if payee_type in ("upi",):
        payee_type = "vpa"
    if payee_type not in ("bank_account", "vpa"):
        raise invalid("payee_type", "Choose bank account or UPI ID")
    name = re.sub(r"\s+", " ", body.name or "").strip()
    if not (2 <= len(name) <= 100) or not re.fullmatch(r"[A-Za-z][A-Za-z .'\-&]*", name):
        raise invalid("name", "Enter the beneficiary's name (letters, spaces, . ' - &; 2 to 100 characters)")
    nickname = (body.nickname or "").strip()[:40] or None

    existing = [dict(r) for r in conn.execute(
        "SELECT * FROM beneficiaries WHERE customer_id = ? AND payee_type = ? AND status != 'deleted'", (ctx.customer_id, payee_type))]

    account_number = ifsc = vpa = None
    if payee_type == "bank_account":
        account_number = re.sub(r"\s", "", body.account_number or "")
        if not ACCOUNT_RE.match(account_number):
            raise invalid("account_number", "Enter the account number (6 to 20 digits)")
        if body.confirm_account_number is not None and re.sub(r"\s", "", body.confirm_account_number) != account_number:
            raise invalid("confirm_account_number", "Account numbers do not match")
        ifsc = (body.ifsc or "").strip().upper()
        if not IFSC_RE.match(ifsc):
            raise invalid("ifsc", "Enter a valid 11-character IFSC, for example EXMB0000001")
        dupes = [b for b in existing if b.get("ifsc") == ifsc and beneficiary_account_number(b) == account_number]
        check = npci_imps.get(conn).name_check(account_number, ifsc)
        if not check.get("valid"):
            field = "ifsc" if check.get("reason") == "invalid_ifsc" else "account_number"
            raise invalid(field, check.get("message") or "The beneficiary bank could not verify this account")
    else:
        vpa = (body.vpa or "").strip().lower()
        if not VPA_RE.match(vpa):
            raise invalid("vpa", "Enter a valid UPI ID, for example name@okaxis")
        dupes = [b for b in existing if (b.get("vpa") or "").lower() == vpa]
        check = npci_upi.get(conn).vpa_lookup(vpa)
        if not check.get("valid"):
            raise invalid("vpa", check.get("message") or "This UPI ID could not be verified")

    if any(b["status"] in ("cooling", "active") for b in dupes):
        raise api_error(409, "duplicate_beneficiary", "This beneficiary is already in your list.")
    # An earlier, never-confirmed attempt for the same payee is replaced.
    for b in dupes:
        conn.execute("UPDATE beneficiaries SET status = 'deleted', deleted_at = ? WHERE id = ?", (now_iso(), b["id"]))

    name_at_bank = check.get("name_at_bank")
    ben = {
        "id": new_id(),
        "customer_id": ctx.customer_id,
        "name": name,
        "nickname": nickname,
        "payee_type": payee_type,
        "account_number_enc": encrypt_str(account_number) if account_number else None,
        "account_masked": mask_account(account_number) if account_number else None,
        "ifsc": ifsc,
        "vpa": vpa,
        "name_at_bank": name_at_bank,
        "status": "pending_otp",
        "created_at": now_iso(),
    }
    conn.execute(
        """INSERT INTO beneficiaries (id, customer_id, name, nickname, payee_type, account_number_enc, account_masked, ifsc, vpa,
           name_at_bank, status, created_at) VALUES (:id, :customer_id, :name, :nickname, :payee_type, :account_number_enc,
           :account_masked, :ifsc, :vpa, :name_at_bank, :status, :created_at)""",
        ben,
    )
    otp = send_otp(conn, ctx.user["mobile"], "beneficiary")
    audit_ctx(conn, ctx, "financial", "beneficiary.add_requested", entity_type="beneficiary", entity_id=ben["id"],
              details={"payee_type": payee_type, "masked": ben["account_masked"] or mask_vpa(vpa), "ifsc": ifsc,
                       "name_check_match": _names_match(name, name_at_bank)}, request=request)
    cfg = cooling_config(conn)
    return {
        "beneficiary_id": ben["id"],
        "status": "pending_otp",
        "name_at_bank": name_at_bank,
        "name_match": _names_match(name, name_at_bank),
        "name_check": {"name_at_bank": name_at_bank, "mock": check.get("mock")},
        "beneficiary": public_beneficiary(conn, ben),
        "cooling_hours": cfg["cooling_hours"],
        "cooling_limit_paise": cfg["cooling_limit_paise"],
        "otp": otp,
    }


@router.post("/me/beneficiaries/{beneficiary_id}/otp")
def resend_otp(beneficiary_id: str, ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    ben = owned_beneficiary(conn, ctx.customer_id, beneficiary_id)
    if ben["status"] != "pending_otp":
        raise api_error(409, "already_confirmed", "This beneficiary is already confirmed.")
    return {"beneficiary_id": ben["id"], "otp": send_otp(conn, ctx.user["mobile"], "beneficiary")}


@router.post("/me/beneficiaries/{beneficiary_id}/confirm")
def confirm_beneficiary(beneficiary_id: str, body: ConfirmIn, request: Request, ctx: AuthContext = Depends(require_customer),
                        conn: sqlite3.Connection = Depends(get_db)):
    ben = owned_beneficiary(conn, ctx.customer_id, beneficiary_id)
    if ben["status"] != "pending_otp":
        raise api_error(409, "already_confirmed", "This beneficiary is already confirmed.")
    require_otp(conn, ctx.user["mobile"], "beneficiary", body.otp)
    cfg = cooling_config(conn)
    now = utcnow()
    until = iso(now + timedelta(hours=cfg["cooling_hours"]))
    conn.execute(
        """UPDATE beneficiaries SET status = 'cooling', cooling_until = ?, cooling_limit_paise = ?, confirmed_at = ? WHERE id = ?""",
        (until, cfg["cooling_limit_paise"], iso(now), ben["id"]),
    )
    ben.update({"status": "cooling", "cooling_until": until, "cooling_limit_paise": cfg["cooling_limit_paise"], "confirmed_at": iso(now)})
    shown = ben["account_masked"] or mask_vpa(ben["vpa"])
    notify(conn, ctx.user_id, "beneficiary_added", "Beneficiary added",
           f"{ben['name']} ({shown}) was added to your internet banking. Up to {format_inr(cfg['cooling_limit_paise'])} can be sent "
           f"until {fmt_ist(until)}. Not you? Call the bank immediately or report fraud.")
    outbox(conn, "beneficiary.added", {"beneficiary_id": ben["id"], "customer_id": ctx.customer_id})
    audit_ctx(conn, ctx, "financial", "beneficiary.confirmed", entity_type="beneficiary", entity_id=ben["id"],
              details={"masked": shown, "cooling_until": until, "cooling_limit_paise": cfg["cooling_limit_paise"]}, request=request)
    return public_beneficiary(conn, ben)


@router.delete("/me/beneficiaries/{beneficiary_id}", status_code=204)
def delete_beneficiary(beneficiary_id: str, request: Request, ctx: AuthContext = Depends(require_customer),
                       conn: sqlite3.Connection = Depends(get_db)):
    ben = owned_beneficiary(conn, ctx.customer_id, beneficiary_id)
    conn.execute("UPDATE beneficiaries SET status = 'deleted', deleted_at = ? WHERE id = ?", (now_iso(), ben["id"]))
    audit_ctx(conn, ctx, "financial", "beneficiary.deleted", entity_type="beneficiary", entity_id=ben["id"],
              details={"masked": ben["account_masked"] or mask_vpa(ben["vpa"])}, request=request)
    return Response(status_code=204)

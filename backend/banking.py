"""Shared customer/account helpers used by several routers.

Account numbers are kept encrypted (``accounts.finacle_account_ref``) with a SHA-256
lookup hash. Balances are never stored here; ask ``integrations.finacle`` for them.
"""
from __future__ import annotations

import sqlite3

from core import forbidden, mask_account, mask_pan, new_id, not_found, now_iso, one, sha256_hex
from crypto import decrypt_str, encrypt_str
from integrations import finacle


def account_hash(account_number: str) -> str:
    return sha256_hex("acct:" + account_number)


def link_account(conn: sqlite3.Connection, owner_id: str, account_number: str, account_type: str, *, currency: str = "INR",
                 ifsc: str | None = None, owner_type: str = "customer", nickname: str | None = None) -> dict:
    """Register a Finacle account on the platform (idempotent on the account number)."""
    h = account_hash(account_number)
    existing = one(conn, "SELECT * FROM accounts WHERE account_ref_hash = ?", (h,))
    if existing:
        return existing
    row = {
        "id": new_id(),
        "customer_id": owner_id,
        "owner_type": owner_type,
        "finacle_account_ref": encrypt_str(account_number),
        "account_ref_hash": h,
        "masked_number": mask_account(account_number),
        "account_type": account_type,
        "currency": currency,
        "ifsc": ifsc or finacle.HOME_IFSC,
        "nickname": nickname,
        "created_at": now_iso(),
    }
    conn.execute(
        """INSERT INTO accounts (id, customer_id, owner_type, finacle_account_ref, account_ref_hash, masked_number, account_type,
           currency, ifsc, nickname, created_at) VALUES (:id, :customer_id, :owner_type, :finacle_account_ref, :account_ref_hash,
           :masked_number, :account_type, :currency, :ifsc, :nickname, :created_at)""",
        row,
    )
    return row


def upsert_customer_from_finacle(conn: sqlite3.Connection, cif: str) -> dict:
    """Create/refresh the platform Customer for a Finacle CIF and link all its accounts."""
    core = finacle.get(conn)
    fc = core.get_customer(cif)
    if not fc:
        raise not_found("Customer")
    existing = one(conn, "SELECT * FROM customers WHERE finacle_cif = ?", (cif,))
    if existing:
        cust = existing
    else:
        cust = {
            "id": new_id(),
            "finacle_cif": cif,
            "full_name": fc["full_name"],
            "pan_masked": mask_pan(fc.get("pan")),
            "date_of_birth": fc.get("date_of_birth"),
            "segment": fc.get("segment") or "retail",
            "residency_status": fc.get("residency_status") or "resident",
            "kyc_status": "full",
            "address": fc.get("address"),
            "email": fc.get("email"),
            "mobile": fc.get("mobile"),
            "created_at": now_iso(),
        }
        conn.execute(
            """INSERT INTO customers (id, finacle_cif, full_name, pan_masked, date_of_birth, segment, residency_status, kyc_status,
               address, email, mobile, created_at) VALUES (:id, :finacle_cif, :full_name, :pan_masked, :date_of_birth, :segment,
               :residency_status, :kyc_status, :address, :email, :mobile, :created_at)""",
            cust,
        )
    for acct in core.accounts_for_cif(cif):
        link_account(conn, cust["id"], acct["account_number"], acct["account_type"], currency=acct["currency"], ifsc=acct["ifsc"])
    return cust


def account_number(account_row: dict) -> str:
    num = decrypt_str(account_row["finacle_account_ref"])
    if not num:
        raise RuntimeError("Account reference could not be decrypted")
    return num


def owned_account(conn: sqlite3.Connection, owner_id: str | None, account_id: str, *, owner_type: str = "customer") -> dict:
    """The account if it belongs to ``owner_id``; 404 if unknown, 403 if someone else's."""
    acct = one(conn, "SELECT * FROM accounts WHERE id = ?", (str(account_id or ""),))
    if not acct:
        raise not_found("Account")
    if not owner_id or acct["customer_id"] != owner_id or acct["owner_type"] != owner_type:
        raise forbidden("This account does not belong to you")
    return acct


def public_account(acct: dict) -> dict:
    return {
        "id": acct["id"],
        "masked_number": acct["masked_number"],
        "account_type": acct["account_type"],
        "currency": acct["currency"],
        "ifsc": acct["ifsc"],
        "nickname": acct["nickname"],
    }


def customer_for(conn: sqlite3.Connection, customer_id: str) -> dict:
    cust = one(conn, "SELECT * FROM customers WHERE id = ?", (customer_id,))
    if not cust:
        raise not_found("Customer")
    return cust


def in_cooling_period(customer: dict) -> bool:
    from core import parse_iso, utcnow

    until = parse_iso(customer.get("high_risk_cooling_until"))
    return bool(until and until > utcnow())

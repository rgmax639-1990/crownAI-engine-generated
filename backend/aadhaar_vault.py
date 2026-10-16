"""Aadhaar Data Vault.

The full Aadhaar number lives only here: encrypted with the dedicated vault key
(``crypto.vault_cipher()``, HSM-backed ``AADHAAR_VAULT_KEY`` in production) and
indexed by a keyed HMAC so the same number always maps to the same reference key.
Every other table stores only the reference key and the last four digits.

Only this module ever decrypts a vault entry. Numbers are never logged, audited or
returned by the API.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from functools import lru_cache

from core import now_iso, valid_aadhaar
from crypto import _load_key, vault_cipher
from db import register_ddl

register_ddl(
    """
CREATE TABLE IF NOT EXISTS aadhaar_vault (
  ref_key TEXT PRIMARY KEY,
  number_enc TEXT NOT NULL,
  number_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);
"""
)


class VaultError(ValueError):
    """Raised for malformed input. The message never contains the number."""


@lru_cache(maxsize=1)
def _lookup_key() -> bytes:
    # A lookup key derived from (not equal to) the vault encryption key.
    return hmac.new(_load_key("AADHAAR_VAULT_KEY", ".dev_vault_key"), b"aadhaar-vault-lookup-v1", hashlib.sha256).digest()


def _digits(number: str) -> str:
    return re.sub(r"[\s-]", "", str(number or ""))


def number_hash(number: str) -> str:
    return hmac.new(_lookup_key(), _digits(number).encode("ascii"), hashlib.sha256).hexdigest()


def store(conn: sqlite3.Connection, number: str) -> tuple[str, str]:
    """Put an Aadhaar number in the vault. Returns (ref_key, last4).

    Storing the same number twice returns the existing reference key.
    """
    digits = _digits(number)
    if not valid_aadhaar(digits):
        raise VaultError("Not a valid Aadhaar number")
    h = number_hash(digits)
    row = conn.execute("SELECT ref_key FROM aadhaar_vault WHERE number_hash = ?", (h,)).fetchone()
    if row:
        return row["ref_key"], digits[-4:]
    ref = "ADV-" + secrets.token_hex(16).upper()
    conn.execute(
        "INSERT INTO aadhaar_vault (ref_key, number_enc, number_hash, created_at) VALUES (?, ?, ?, ?)",
        (ref, vault_cipher().encrypt(digits.encode("ascii")).decode("ascii"), h, now_iso()),
    )
    return ref, digits[-4:]


def exists(conn: sqlite3.Connection, ref_key: str | None) -> bool:
    if not ref_key:
        return False
    return conn.execute("SELECT 1 FROM aadhaar_vault WHERE ref_key = ?", (ref_key,)).fetchone() is not None


def last4(conn: sqlite3.Connection, ref_key: str) -> str | None:
    number = _reveal(conn, ref_key)
    return number[-4:] if number else None


def _reveal(conn: sqlite3.Connection, ref_key: str) -> str | None:
    """Decrypt a vault entry. Private: only the vault (e.g. a live UIDAI call routed through it) may use it."""
    row = conn.execute("SELECT number_enc FROM aadhaar_vault WHERE ref_key = ?", (ref_key,)).fetchone()
    if not row:
        return None
    try:
        return vault_cipher().decrypt(row["number_enc"].encode("ascii")).decode("ascii")
    except Exception:  # noqa: BLE001 - never surface ciphertext problems with data attached
        return None


__all__ = ["store", "exists", "last4", "number_hash", "VaultError"]

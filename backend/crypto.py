"""Field and object encryption (AES via Fernet).

Production keys come from the HSM-backed key service through ``DATA_ENCRYPTION_KEY``
(and ``AADHAAR_VAULT_KEY`` for the Aadhaar Data Vault). Outside production, a
development key is generated once into the data directory so a laptop run works; the
app refuses to start in production without the environment keys.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from db import DATA_DIR


def _load_key(env_name: str, file_name: str) -> bytes:
    value = os.getenv(env_name)
    if value:
        return value.encode("ascii")
    if os.getenv("APP_ENV", "dev") == "production":
        raise RuntimeError(f"{env_name} must be set in production (HSM-backed key)")
    path = Path(DATA_DIR) / file_name
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(Fernet.generate_key())
    return path.read_bytes().strip()


@lru_cache(maxsize=None)
def _fernet(env_name: str, file_name: str) -> Fernet:
    return Fernet(_load_key(env_name, file_name))


def data_cipher() -> Fernet:
    return _fernet("DATA_ENCRYPTION_KEY", ".dev_data_key")


def vault_cipher() -> Fernet:
    return _fernet("AADHAAR_VAULT_KEY", ".dev_vault_key")


def encrypt_str(value: str) -> str:
    return data_cipher().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_str(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return data_cipher().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        return None


def encrypt_bytes(data: bytes) -> bytes:
    return data_cipher().encrypt(data)


def decrypt_bytes(data: bytes) -> bytes:
    return data_cipher().decrypt(data)

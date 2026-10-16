"""Object storage for documents (KYC, KFS/MITC, statements, receipts, evidence).

Locally this is an encrypted directory under the data dir; in deployment it maps to an
object store in an Indian region. Objects are always encrypted at rest.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from core import new_id, now_iso, sha256_hex
from crypto import decrypt_bytes, encrypt_bytes
from db import DATA_DIR

ROOT = Path(DATA_DIR) / "objects"


def put_bytes(data: bytes, prefix: str) -> tuple[str, str]:
    """Store bytes encrypted; returns (storage_key, sha256 of the plaintext)."""
    safe_prefix = re.sub(r"[^a-z0-9_\-/]", "", prefix.lower()) or "misc"
    key = f"{safe_prefix}/{new_id()}"
    path = ROOT / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encrypt_bytes(data))
    return key, sha256_hex(data)


def get_bytes(storage_key: str) -> bytes:
    path = (ROOT / storage_key).resolve()
    if ROOT.resolve() not in path.parents or not path.exists():
        raise FileNotFoundError(storage_key)
    return decrypt_bytes(path.read_bytes())


def create_document(
    conn: sqlite3.Connection,
    *,
    owner_type: str,
    owner_id: str,
    doc_type: str,
    data: bytes,
    file_name: str,
    content_type: str = "application/pdf",
    locale: str = "en",
) -> dict:
    """Store a generated or uploaded file and register it in ``documents``."""
    key, digest = put_bytes(data, f"documents/{owner_type}")
    doc = {
        "id": new_id(),
        "owner_type": owner_type,
        "owner_id": owner_id,
        "doc_type": doc_type,
        "locale": locale,
        "storage_key": key,
        "sha256": digest,
        "file_name": file_name,
        "content_type": content_type,
        "size_bytes": len(data),
        "created_at": now_iso(),
    }
    conn.execute(
        """INSERT INTO documents (id, owner_type, owner_id, doc_type, locale, storage_key, sha256, file_name,
           content_type, size_bytes, created_at) VALUES (:id, :owner_type, :owner_id, :doc_type, :locale,
           :storage_key, :sha256, :file_name, :content_type, :size_bytes, :created_at)""",
        doc,
    )
    return doc


def consume_upload(conn: sqlite3.Connection, upload_ref: str, user_id: str | None, field: str = "file_upload_ref") -> dict:
    """Resolve an ``upload_ref`` returned by ``POST /uploads``. Raises 422 if unknown/foreign."""
    from core import invalid, one

    row = one(conn, "SELECT * FROM uploads WHERE id = ?", (str(upload_ref or ""),))
    if not row or (user_id and row["owner_user_id"] and row["owner_user_id"] != user_id):
        raise invalid(field, "Upload not found. Upload the file again.")
    conn.execute("UPDATE uploads SET consumed_at = COALESCE(consumed_at, ?) WHERE id = ?", (now_iso(), row["id"]))
    return row

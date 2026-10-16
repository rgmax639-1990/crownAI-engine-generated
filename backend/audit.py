"""Append-only, hash-chained audit log.

Each event stores ``prev_hash`` (the previous event's hash) and its own ``hash`` over
its canonical content, so any edit or deletion breaks the chain. The table also has
triggers that reject UPDATE and DELETE. Requests run inside ``BEGIN IMMEDIATE``, so
reading the previous hash and inserting the event happen under the write lock.
Details are PII-redacted before they are stored (never a full Aadhaar, card or PAN).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from core import client_ip, iso, new_id, redact_obj, sha256_hex, utcnow

GENESIS = "0" * 64
RETENTION_DAYS = 180


def _canonical(ev: dict) -> str:
    keys = ["id", "category", "actor_type", "actor_id", "action", "entity_type", "entity_id", "details", "ip", "occurred_at", "prev_hash"]
    return json.dumps({k: ev.get(k) for k in keys}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def audit(
    conn: sqlite3.Connection,
    category: str,
    action: str,
    *,
    actor_type: str = "system",
    actor_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    details: dict | None = None,
    ip: str | None = None,
) -> dict:
    now = utcnow()
    prev = conn.execute("SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1").fetchone()
    ev = {
        "id": new_id(),
        "category": category,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "details": json.dumps(redact_obj(details or {}), sort_keys=True, ensure_ascii=False, default=str),
        "ip": ip,
        "occurred_at": iso(now),
        "prev_hash": prev["hash"] if prev else GENESIS,
    }
    ev["hash"] = sha256_hex(_canonical(ev))
    ev["retain_until"] = (now + timedelta(days=RETENTION_DAYS)).date().isoformat()
    conn.execute(
        """INSERT INTO audit_events (id, category, actor_type, actor_id, action, entity_type, entity_id, details, ip,
           occurred_at, prev_hash, hash, retain_until) VALUES (:id, :category, :actor_type, :actor_id, :action,
           :entity_type, :entity_id, :details, :ip, :occurred_at, :prev_hash, :hash, :retain_until)""",
        ev,
    )
    return ev


def audit_ctx(conn: sqlite3.Connection, ctx: Any, category: str, action: str, *, entity_type: str | None = None,
              entity_id: str | None = None, details: dict | None = None, request: Any = None) -> dict:
    """Audit an action by the authenticated caller (``security.AuthContext``)."""
    return audit(
        conn, category, action,
        actor_type=ctx.actor_type if ctx else "visitor",
        actor_id=ctx.user_id if ctx else None,
        entity_type=entity_type, entity_id=entity_id, details=details, ip=client_ip(request),
    )


def verify_chain(conn: sqlite3.Connection) -> dict:
    """Recompute every hash. Returns {verified, checked, broken_at_seq}."""
    prev_hash = GENESIS
    checked = 0
    for row in conn.execute("SELECT * FROM audit_events ORDER BY seq ASC"):
        ev = dict(row)
        if ev["prev_hash"] != prev_hash or sha256_hex(_canonical(ev)) != ev["hash"]:
            return {"verified": False, "checked": checked, "broken_at_seq": ev["seq"]}
        prev_hash = ev["hash"]
        checked += 1
    return {"verified": True, "checked": checked, "broken_at_seq": None}

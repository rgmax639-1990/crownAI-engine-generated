"""DPDP consent state shared by every module.

A purpose is active for a subject when the most recent ``consent_records`` row for
(subject_id, purpose) has ``granted = 1`` and no ``withdrawn_at``. Withdrawal takes
effect immediately because every check reads the latest row.
"""
from __future__ import annotations

import sqlite3

from core import new_id, now_iso

NON_ESSENTIAL_PURPOSES = ("analytics", "marketing", "personalisation")


def latest_consent(conn: sqlite3.Connection, subject_id: str, purpose: str) -> dict | None:
    row = conn.execute(
        """SELECT * FROM consent_records WHERE subject_id = ? AND purpose = ?
           ORDER BY recorded_at DESC, rowid DESC LIMIT 1""",
        (str(subject_id), purpose),
    ).fetchone()
    return dict(row) if row else None


def consent_active(conn: sqlite3.Connection, subject_id: str | None, purpose: str) -> bool:
    if not subject_id:
        return False
    rec = latest_consent(conn, subject_id, purpose)
    return bool(rec and rec["granted"] and not rec["withdrawn_at"])


def record_consent(conn: sqlite3.Connection, *, subject_type: str, subject_id: str, purpose: str, granted: bool,
                   notice_version_id: str | None = None, language: str = "en") -> dict:
    """Append a consent record (history is never overwritten)."""
    rec = {
        "id": new_id(),
        "subject_type": subject_type,
        "subject_id": str(subject_id),
        "purpose": purpose,
        "granted": int(bool(granted)),
        "notice_version_id": notice_version_id,
        "language": language if language in ("en", "hi") else "en",
        "recorded_at": now_iso(),
    }
    conn.execute(
        """INSERT INTO consent_records (id, subject_type, subject_id, purpose, granted, notice_version_id, language, recorded_at)
           VALUES (:id, :subject_type, :subject_id, :purpose, :granted, :notice_version_id, :language, :recorded_at)""",
        rec,
    )
    return rec


def withdraw_consent(conn: sqlite3.Connection, subject_id: str, purpose: str) -> int:
    """Mark every active grant for (subject, purpose) withdrawn now. Returns rows changed."""
    cur = conn.execute(
        "UPDATE consent_records SET withdrawn_at = ? WHERE subject_id = ? AND purpose = ? AND granted = 1 AND withdrawn_at IS NULL",
        (now_iso(), str(subject_id), purpose),
    )
    return cur.rowcount


def current_consent_notice_version(conn: sqlite3.Connection, locale: str = "en") -> str | None:
    """Published version id of the DPDP consent notice in ``locale`` (falls back to English)."""
    for loc in (locale, "en"):
        row = conn.execute(
            """SELECT published_version_id FROM content_items WHERE content_type = 'consent_notice' AND locale = ?
               AND published_version_id IS NOT NULL ORDER BY created_at LIMIT 1""",
            (loc,),
        ).fetchone()
        if row:
            return row["published_version_id"]
    return None

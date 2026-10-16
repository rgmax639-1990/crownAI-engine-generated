"""Helper for seeding published CMS content with a complete maker -> checker -> legal trail.

Seed content is created exactly as if it had gone through the workflow: a version by the
seed content maker, approved by a different checker (and legal approver where required),
then published, with audit events for each step.
"""
from __future__ import annotations

import sqlite3

from audit import audit
from core import jdump, new_id, now_iso, one

SEED_MAKER_EMAIL = "maker@example.bank.in"
SEED_CHECKER_EMAIL = "checker@example.bank.in"
SEED_LEGAL_EMAIL = "legal@example.bank.in"
LEGAL_REQUIRED_TYPES = {"product_page", "policy", "footer_disclosure", "consent_notice", "regulatory_value"}


def _uid(conn: sqlite3.Connection, email: str) -> str:
    row = one(conn, "SELECT id FROM users WHERE email = ?", (email,))
    if not row:
        raise RuntimeError(f"Seed staff user {email} missing; seed_core must run first")
    return row["id"]


def seed_published(
    conn: sqlite3.Connection,
    *,
    content_type: str,
    template: str,
    slug: str,
    locale: str,
    body: dict,
    translation_group_id: str | None = None,
    needs_compliance_verification: bool = False,
    change_note: str = "Initial content (synthetic seed)",
) -> dict:
    """Create a content item + published version. Returns {item_id, version_id, translation_group_id}."""
    maker, checker, legal = _uid(conn, SEED_MAKER_EMAIL), _uid(conn, SEED_CHECKER_EMAIL), _uid(conn, SEED_LEGAL_EMAIL)
    item_id, version_id = new_id(), new_id()
    group = translation_group_id or new_id()
    now = now_iso()
    conn.execute(
        """INSERT INTO content_items (id, content_type, template, slug, locale, translation_group_id, published_version_id,
           needs_compliance_verification, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (item_id, content_type, template, slug, locale, group, version_id, int(needs_compliance_verification), maker, now),
    )
    needs_legal = content_type in LEGAL_REQUIRED_TYPES
    conn.execute(
        """INSERT INTO content_versions (id, content_item_id, version_no, body, change_note, status, maker_id, checker_id,
           legal_approver_id, created_at, submitted_at, checker_approved_at, legal_approved_at, published_at, published_by)
           VALUES (?,?,1,?,?, 'published', ?,?,?,?,?,?,?,?,?)""",
        (version_id, item_id, jdump(body), change_note, maker, checker, legal if needs_legal else None, now, now, now,
         now if needs_legal else None, now, checker),
    )
    for action, actor in (("content.version_created", maker), ("content.submitted", maker), ("content.checker_approved", checker),
                          *((("content.legal_approved", legal),) if needs_legal else ()), ("content.published", checker)):
        audit(conn, "content", action, actor_type="staff", actor_id=actor, entity_type="content_version", entity_id=version_id,
              details={"slug": slug, "locale": locale, "seed": True})
    return {"item_id": item_id, "version_id": version_id, "translation_group_id": group}

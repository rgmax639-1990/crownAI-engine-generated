"""Customer alerts (SMS, email, push). Every login and transaction is alerted.

Delivery goes through the SMS/email gateway adapter (mock by default); the delivery
time is recorded so the 30-second alert target can be measured.
"""
from __future__ import annotations

import sqlite3

from core import jdump, jload, new_id, now_iso
from integrations import sms_gateway


def notify(conn: sqlite3.Connection, user_id: str | None, event_type: str, title: str, body: str,
           channels: list[str] | None = None) -> dict | None:
    if not user_id:
        return None
    user = conn.execute("SELECT mobile, email, notification_channels FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        return None
    preferred = jload(user["notification_channels"], ["sms", "email", "push"]) or ["sms"]
    # Security-relevant alerts always go by SMS regardless of preference.
    chosen = channels or sorted(set(preferred) | {"sms"})
    gateway = sms_gateway.get(conn)
    delivered_at = None
    if "sms" in chosen:
        res = gateway.send_sms(user["mobile"], f"{title}: {body}")
        delivered_at = res.get("delivered_at")
    if "email" in chosen and user["email"]:
        res = gateway.send_email(user["email"], title, body)
        delivered_at = delivered_at or res.get("delivered_at")
    n = {
        "id": new_id(),
        "user_id": user_id,
        "event_type": event_type,
        "channels": jdump(chosen),
        "title": title,
        "body": body,
        "triggered_at": now_iso(),
        "delivered_at": delivered_at or now_iso(),
    }
    conn.execute(
        """INSERT INTO notifications (id, user_id, event_type, channels, title, body, triggered_at, delivered_at)
           VALUES (:id, :user_id, :event_type, :channels, :title, :body, :triggered_at, :delivered_at)""",
        n,
    )
    return n


def notify_contact(conn: sqlite3.Connection, mobile: str | None, email: str | None, title: str, body: str) -> None:
    """Alert a contact that is not (or not yet) a user, e.g. old and new contacts on a profile change."""
    gateway = sms_gateway.get(conn)
    if mobile:
        gateway.send_sms(mobile, f"{title}: {body}")
    if email:
        gateway.send_email(email, title, body)


def outbox(conn: sqlite3.Connection, topic: str, payload: dict) -> None:
    """Transactional outbox: written in the same transaction as the business change."""
    conn.execute(
        "INSERT INTO outbox (id, topic, payload, created_at) VALUES (?, ?, ?, ?)",
        (new_id(), topic, jdump(payload), now_iso()),
    )

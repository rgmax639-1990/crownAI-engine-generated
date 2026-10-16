"""Card controls (US-21): channel switches, limits, temporary / permanent block, unblock.

Changes are applied through the card management system (CMS) adapter first; the
platform keeps the resulting settings for display. Control changes and unblocking need
a step-up ``mfa_token``; blocking does not (it must always be quick).
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from audit import audit_ctx
from core import api_error, fmt_ist, forbidden, invalid, not_found, now_iso, one
from db import get_db
from integrations import card_management
from integrations.base import mock_meta
from notify import notify, outbox
from security import AuthContext, consume_mfa_token, require_customer

router = APIRouter(tags=["cards"])

SWITCHES = [("domestic_enabled", "Domestic use"), ("international_enabled", "International use"),
            ("online_enabled", "Online (e-commerce)"), ("contactless_enabled", "Contactless (tap to pay)")]
LIMITS = [("atm_limit_paise", "max_atm_limit_paise", "ATM limit"), ("pos_limit_paise", "max_pos_limit_paise", "POS limit"),
          ("online_limit_paise", "max_online_limit_paise", "Online limit")]
BLOCK_REASONS = {"lost": "Lost", "stolen": "Stolen", "suspected_fraud": "Suspected fraud", "damaged": "Damaged",
                 "not_in_use": "Not in use", "other": "Other"}
KIND_LABEL = {"debit": "Debit card", "credit": "Credit card", "virtual_debit": "Virtual debit card"}


class Body(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class ControlsIn(Body):
    domestic_enabled: Optional[bool] = None
    international_enabled: Optional[bool] = None
    online_enabled: Optional[bool] = None
    contactless_enabled: Optional[bool] = None
    atm_limit_paise: Any = None
    pos_limit_paise: Any = None
    online_limit_paise: Any = None
    mfa_token: Optional[str] = None


class BlockIn(Body):
    permanent: bool = False
    reason: Optional[str] = None
    note: Optional[str] = None


class UnblockIn(Body):
    mfa_token: Optional[str] = None


def _rupees(paise: int) -> str:
    rupees, p = divmod(int(paise), 100)
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    return f"₹{s}" + (f".{p:02d}" if p else "")


def _owned_card(conn: sqlite3.Connection, ctx: AuthContext, card_id: str) -> dict:
    card = one(conn, "SELECT * FROM cards WHERE id = ?", (str(card_id or "")[:64],))
    if not card:
        raise not_found("Card")
    if card["customer_id"] != ctx.customer_id:
        raise forbidden("This card does not belong to you")
    return card


def public_card(conn: sqlite3.Connection, c: dict) -> dict:
    acct = one(conn, "SELECT masked_number FROM accounts WHERE id = ?", (c.get("account_id"),)) if c.get("account_id") else None
    return {
        "id": c["id"],
        "card_kind": c["card_kind"],
        "kind_label": KIND_LABEL.get(c["card_kind"], c["card_kind"]),
        "network": c["network"],
        "variant": c.get("variant"),
        "last4": c["last4"],
        "masked_number": f"XXXX XXXX XXXX {c['last4']}",
        "expiry": c.get("expiry"),
        "status": c["status"],
        "linked_account": acct["masked_number"] if acct else None,
        "controls": {k: bool(c[k]) for k, _ in SWITCHES},
        "limits": {k: c[k] for k, _, _ in LIMITS},
        "max_limits": {k: c[m] for k, m, _ in LIMITS},
        **{k: bool(c[k]) for k, _ in SWITCHES},
        **{k: c[k] for k, _, _ in LIMITS},
        **{m: c[m] for _, m, _ in LIMITS},
        "last_change_summary": c.get("last_change_summary"),
        "updated_at": c.get("updated_at"),
        "blocked_at": c.get("blocked_at"),
        "can_unblock": c["status"] == "temporarily_blocked",
        "can_change_controls": c["status"] != "permanently_blocked",
    }


def _user_id(ctx: AuthContext) -> str | None:
    return ctx.user_id


@router.get("/me/cards")
def list_cards(ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM cards WHERE customer_id = ? ORDER BY CASE card_kind WHEN 'debit' THEN 0 WHEN 'credit' THEN 1 ELSE 2 END, created_at",
        (ctx.customer_id,))]
    cms = card_management.get(conn)
    return {"items": [public_card(conn, c) for c in rows], "total": len(rows),
            "block_reasons": [{"value": k, "label": v} for k, v in BLOCK_REASONS.items()],
            "mock": mock_meta("card_management") if getattr(cms, "is_mock", False) else None}


@router.get("/me/cards/{card_id}")
def get_card(card_id: str, ctx: AuthContext = Depends(require_customer), conn: sqlite3.Connection = Depends(get_db)):
    return public_card(conn, _owned_card(conn, ctx, card_id))


@router.put("/me/cards/{card_id}/controls")
def update_controls(card_id: str, body: ControlsIn, request: Request, ctx: AuthContext = Depends(require_customer),
                    conn: sqlite3.Connection = Depends(get_db)):
    card = _owned_card(conn, ctx, card_id)
    if card["status"] == "permanently_blocked":
        raise api_error(409, "card_permanently_blocked", "This card is permanently blocked. Request a replacement card.")
    changes: dict = {}
    summary: list[str] = []
    for key, label in SWITCHES:
        val = getattr(body, key)
        if val is not None and bool(val) != bool(card[key]):
            changes[key] = int(bool(val))
            summary.append(f"{label.split(' ')[0]} {'enabled' if val else 'disabled'}")
    for key, max_key, label in LIMITS:
        val = getattr(body, key)
        if val is None:
            continue
        if not isinstance(val, int) or isinstance(val, bool) or val < 0 or val > card[max_key]:
            raise invalid(key, f"{label} must be between ₹0 and {_rupees(card[max_key])}")
        if val != card[key]:
            changes[key] = val
            summary.append(f"{label} {_rupees(card[key])} -> {_rupees(val)}")
    if not changes:
        raise api_error(422, "no_changes", "Nothing was changed.")
    consume_mfa_token(conn, ctx, body.mfa_token, f"card_controls:{card['id']}")
    ack = card_management.get(conn).set_controls(card["cms_card_ref"], changes)
    now = now_iso()
    text = "; ".join(summary) + f" on {fmt_ist(now)}"
    sets = ", ".join(f"{k} = ?" for k in changes)
    conn.execute(f"UPDATE cards SET {sets}, updated_at = ?, last_change_summary = ? WHERE id = ?",
                 (*changes.values(), now, text, card["id"]))
    notify(conn, _user_id(ctx), "card_control", "Card settings changed",
           f"Card XX{card['last4']}: {text}. Not you? Block the card in internet banking or call the bank.")
    outbox(conn, "card.controls_changed", {"card_id": card["id"], "changes": changes})
    audit_ctx(conn, ctx, "financial", "card.controls_changed", entity_type="card", entity_id=card["id"],
              details={"last4": card["last4"], "changes": changes, "cms_ack_ref": ack.get("cms_ack_ref")}, request=request)
    return {**public_card(conn, one(conn, "SELECT * FROM cards WHERE id = ?", (card["id"],))),  # type: ignore[arg-type]
            "cms_ack_ref": ack.get("cms_ack_ref"), "mock": ack.get("mock")}


@router.post("/me/cards/{card_id}/block")
def block_card(card_id: str, body: BlockIn, request: Request, ctx: AuthContext = Depends(require_customer),
               conn: sqlite3.Connection = Depends(get_db)):
    card = _owned_card(conn, ctx, card_id)
    if card["status"] == "permanently_blocked":
        raise api_error(409, "card_permanently_blocked", "This card is already permanently blocked.")
    if card["status"] == "temporarily_blocked" and not body.permanent:
        raise api_error(409, "already_blocked", "This card is already temporarily blocked.")
    reason = (body.reason or "").strip().lower() or None
    if reason and reason not in BLOCK_REASONS:
        raise invalid("reason", "Choose a reason from the list")
    if body.permanent and not reason:
        raise invalid("reason", "Choose why you are blocking the card permanently")
    note = (body.note or "").strip()[:200] or None
    ack = card_management.get(conn).block(card["cms_card_ref"], bool(body.permanent), reason)
    status = "permanently_blocked" if body.permanent else "temporarily_blocked"
    now = now_iso()
    label = "Permanently blocked" if body.permanent else "Temporarily blocked"
    text = f"{label}" + (f" ({BLOCK_REASONS[reason]})" if reason else "") + f" on {fmt_ist(now)}"
    conn.execute("UPDATE cards SET status = ?, blocked_at = ?, updated_at = ?, last_change_summary = ? WHERE id = ?",
                 (status, now, now, text, card["id"]))
    notify(conn, _user_id(ctx), "card_control", "Card blocked",
           f"Card XX{card['last4']} {label.lower()} on {fmt_ist(now)}."
           + (" A replacement card can be requested from Service requests." if body.permanent else " You can unblock it any time."))
    outbox(conn, "card.blocked", {"card_id": card["id"], "permanent": bool(body.permanent), "reason": reason})
    audit_ctx(conn, ctx, "financial", "card.blocked", entity_type="card", entity_id=card["id"],
              details={"last4": card["last4"], "permanent": bool(body.permanent), "reason": reason, "note": note,
                       "cms_ack_ref": ack.get("cms_ack_ref")}, request=request)
    return {**public_card(conn, one(conn, "SELECT * FROM cards WHERE id = ?", (card["id"],))),  # type: ignore[arg-type]
            "cms_ack_ref": ack.get("cms_ack_ref"), "mock": ack.get("mock")}


@router.post("/me/cards/{card_id}/unblock")
def unblock_card(card_id: str, body: UnblockIn, request: Request, ctx: AuthContext = Depends(require_customer),
                 conn: sqlite3.Connection = Depends(get_db)):
    card = _owned_card(conn, ctx, card_id)
    if card["status"] != "temporarily_blocked":
        msg = ("A permanently blocked card cannot be unblocked. Request a replacement card."
               if card["status"] == "permanently_blocked" else "This card is not blocked.")
        raise api_error(409, "cannot_unblock", msg)
    consume_mfa_token(conn, ctx, body.mfa_token, f"card_unblock:{card['id']}")
    ack = card_management.get(conn).unblock(card["cms_card_ref"])
    now = now_iso()
    text = f"Unblocked on {fmt_ist(now)}"
    conn.execute("UPDATE cards SET status = 'active', blocked_at = NULL, updated_at = ?, last_change_summary = ? WHERE id = ?",
                 (now, text, card["id"]))
    notify(conn, _user_id(ctx), "card_control", "Card unblocked",
           f"Card XX{card['last4']} was unblocked on {fmt_ist(now)}. Not you? Block it again immediately.")
    outbox(conn, "card.unblocked", {"card_id": card["id"]})
    audit_ctx(conn, ctx, "financial", "card.unblocked", entity_type="card", entity_id=card["id"],
              details={"last4": card["last4"], "cms_ack_ref": ack.get("cms_ack_ref")}, request=request)
    return {**public_card(conn, one(conn, "SELECT * FROM cards WHERE id = ?", (card["id"],))),  # type: ignore[arg-type]
            "cms_ack_ref": ack.get("cms_ack_ref"), "mock": ack.get("mock")}


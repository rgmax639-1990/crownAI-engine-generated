"""Staff administration: integrations (mock/live with recorded approvals), audit search with hash-chain
verification, incidents with the CERT-In reporting countdown, the evidence register (traceability), maintenance
notices and staff users.

Rules enforced here:
  * A provider only goes live through: request (ops_admin) -> decision by a *different* integration_approver ->
    enable with that approved, unused approval AND every required credential variable present.
  * Credentials are never returned: only the variable NAMES and whether each is set.
  * Evidence records must reference an executed run with an uploaded artifact; nothing is seeded, so the
    traceability matrix shows "Not evidenced" until a real run is recorded.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
import sqlite3
import string
from datetime import datetime, time, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from audit import RETENTION_DAYS, audit_ctx, verify_chain
from core import (IST, api_error, fmt_ist, forbidden, get_or_404, get_reg_value, invalid, iso, jdump, jload, many,
                  mask_mobile, new_id, now_iso, one, parse_iso, redact_obj, require_mobile, today_ist, utcnow,
                  valid_email)
from db import get_db, register_seed
from integrations.base import ENVIRONMENTS, PROVIDERS, current_environment
from objectstore import consume_upload, get_bytes
from security import STAFF_ROLES, AuthContext, end_session, hash_password, password_problem, require_staff

router = APIRouter(tags=["admin"])


class Body(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


# ------------------------------------------------------------------ shared helpers
def _names(conn: sqlite3.Connection, ids: list[Any]) -> dict[str, str]:
    ids = sorted({str(i) for i in ids if i})
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    return {r["id"]: (r["display_name"] or r["email"]) for r in conn.execute(
        f"SELECT id, display_name, email FROM users WHERE id IN ({marks})", ids)}


def _text(value: Optional[str], field: str, label: str, min_len: int, max_len: int) -> str:
    v = (value or "").strip()
    if len(v) < min_len:
        raise invalid(field, f"{label} must be at least {min_len} characters" if min_len > 1 else f"{label} is required")
    if len(v) > max_len:
        raise invalid(field, f"{label} must be at most {max_len} characters")
    return v


def _choice(value: Optional[str], field: str, allowed: Any, label: str) -> str:
    v = (value or "").strip()
    if v not in allowed:
        raise invalid(field, f"Choose a valid {label}: {', '.join(allowed)}")
    return v


def _when(value: Optional[str], field: str, label: str) -> datetime:
    if not value or not str(value).strip():
        raise invalid(field, f"{label} is required")
    dt = parse_iso(value)
    if dt is None:
        raise invalid(field, f"{label} must be a date and time, for example 2026-10-20T14:30 (IST)")
    return dt


# ================================================================== integrations
ADMIN_VIEW_ROLES = ("ops_admin", "integration_approver", "soc_analyst", "compliance_reviewer")


def _approval_out(a: dict | None, names: dict[str, str], adapter: dict | None = None) -> dict | None:
    if not a:
        return None
    used = bool(adapter and adapter.get("active_approval_id") == a["id"] and adapter.get("mode") == "live")
    return {
        "id": a["id"],
        "adapter_id": a["adapter_id"],
        "status": a["decision"],
        "evidence_refs": a["evidence_refs"],
        "justification": a["justification"],
        "requested_by": a["requested_by"],
        "requested_by_name": names.get(a["requested_by"]),
        "requested_at": a["requested_at"],
        "decided_by": a["approved_by"],
        "decided_by_name": names.get(a["approved_by"]) if a["approved_by"] else None,
        "decided_at": a["decided_at"],
        "decision_comment": a["decision_comment"],
        "used_to_enable_live": used,
    }


def _adapter_out(row: dict, approval: dict | None, names: dict[str, str]) -> dict:
    meta = PROVIDERS.get(row["provider"], {"label": row["provider"], "env": []})
    env_vars = [{"name": n, "set": bool(os.getenv(n))} for n in meta["env"]]
    set_count = sum(1 for e in env_vars if e["set"])
    return {
        "id": row["id"],
        "provider": row["provider"],
        "label": meta["label"],
        "environment": row["environment"],
        "mode": row["mode"],
        "badge": "LIVE" if row["mode"] == "live" else "MOCK",
        "health_status": row["health_status"],
        "last_health_check": row["last_health_check"],
        "env_vars": env_vars,
        "env_vars_set": set_count,
        "env_vars_total": len(env_vars),
        "credentials_complete": set_count == len(env_vars),
        "active_approval_id": row["active_approval_id"],
        "latest_approval": _approval_out(approval, names, row),
        "can_enable_live": row["mode"] == "live_approved" and bool(row["active_approval_id"]),
    }


def _latest_approval(conn: sqlite3.Connection, adapter_id: str) -> dict | None:
    return one(conn, "SELECT * FROM integration_approvals WHERE adapter_id = ? ORDER BY requested_at DESC, rowid DESC LIMIT 1",
               (adapter_id,))


def _adapter_payload(conn: sqlite3.Connection, adapter_id: str) -> dict:
    row = get_or_404(conn, "integration_adapters", adapter_id, "Integration adapter")
    appr = _latest_approval(conn, adapter_id)
    names = _names(conn, [appr["requested_by"], appr["approved_by"]] if appr else [])
    return _adapter_out(row, appr, names)


@router.get("/admin/integrations")
def list_integrations(environment: Optional[str] = None, ctx: AuthContext = Depends(require_staff(*ADMIN_VIEW_ROLES)),
                      conn: sqlite3.Connection = Depends(get_db)):
    env = (environment or current_environment()).strip().lower()
    if env not in ENVIRONMENTS:
        raise invalid("environment", f"Choose one of: {', '.join(ENVIRONMENTS)}")
    rows = {r["provider"]: r for r in many(conn, "SELECT * FROM integration_adapters WHERE environment = ?", (env,))}
    latest: dict[str, dict] = {}
    for a in many(conn, """SELECT ap.* FROM integration_approvals ap JOIN integration_adapters ad ON ad.id = ap.adapter_id
                           WHERE ad.environment = ? ORDER BY ap.requested_at DESC, ap.rowid DESC""", (env,)):
        latest.setdefault(a["adapter_id"], a)
    names = _names(conn, [x for a in latest.values() for x in (a["requested_by"], a["approved_by"])])
    adapters = [_adapter_out(rows[p], latest.get(rows[p]["id"]), names) for p in PROVIDERS if p in rows]
    return {
        "environment": env,
        "current_environment": current_environment(),
        "environments": ENVIRONMENTS,
        "adapters": adapters,
        "live_count": sum(1 for a in adapters if a["badge"] == "LIVE"),
        "pending_count": sum(1 for a in adapters if a["latest_approval"] and a["latest_approval"]["status"] == "pending"),
        "credentials_note": "Only variable names are shown. 'Set' reflects this server process's environment; values are never displayed.",
    }


class LiveRequestIn(Body):
    evidence_refs: Optional[str] = None
    justification: Optional[str] = None


@router.post("/admin/integrations/{adapter_id}/live-requests", status_code=201)
def request_live(adapter_id: str, body: LiveRequestIn, request: Request, ctx: AuthContext = Depends(require_staff("ops_admin")),
                 conn: sqlite3.Connection = Depends(get_db)):
    adapter = get_or_404(conn, "integration_adapters", adapter_id, "Integration adapter")
    evidence = _text(body.evidence_refs, "evidence_refs", "Evidence references", 5, 2000)
    justification = _text(body.justification, "justification", "Justification", 20, 4000)
    pending = one(conn, "SELECT id FROM integration_approvals WHERE adapter_id = ? AND decision = 'pending'", (adapter_id,))
    if pending:
        raise api_error(409, "request_pending", "A live request for this adapter is already waiting for a decision",
                        approval_id=pending["id"])
    if adapter["mode"] == "live":
        raise api_error(409, "already_live", "This adapter is already live")
    if adapter["mode"] == "live_approved":
        raise api_error(409, "already_approved", "A live request is already approved; enable live or revert to mock first")
    aid = new_id()
    now = now_iso()
    conn.execute(
        """INSERT INTO integration_approvals (id, adapter_id, requested_by, evidence_refs, justification, decision, requested_at)
           VALUES (?,?,?,?,?,'pending',?)""",
        (aid, adapter_id, ctx.user_id, evidence, justification, now),
    )
    conn.execute("UPDATE integration_adapters SET mode = 'live_requested' WHERE id = ?", (adapter_id,))
    audit_ctx(conn, ctx, "integration", "integration.live_requested", entity_type="integration_adapter", entity_id=adapter_id,
              details={"provider": adapter["provider"], "environment": adapter["environment"], "approval_id": aid,
                       "evidence_refs": evidence}, request=request)
    out = _adapter_payload(conn, adapter_id)
    return {"approval": out["latest_approval"], "adapter": out}


class DecideIn(Body):
    decision: Optional[str] = None
    comment: Optional[str] = None


@router.post("/admin/integrations/approvals/{approval_id}/decide")
def decide_approval(approval_id: str, body: DecideIn, request: Request,
                    ctx: AuthContext = Depends(require_staff("integration_approver")), conn: sqlite3.Connection = Depends(get_db)):
    appr = get_or_404(conn, "integration_approvals", approval_id, "Approval")
    decision = _choice(body.decision, "decision", ("approved", "rejected"), "decision")
    if appr["requested_by"] == ctx.user_id:
        raise forbidden("You requested this change, so a different integration approver must decide it", "self_approval_forbidden")
    if appr["decision"] != "pending":
        raise api_error(409, "already_decided", f"This request was already {appr['decision']}")
    comment = (body.comment or "").strip()
    if decision == "rejected" and len(comment) < 5:
        raise invalid("comment", "Give a reason for the rejection (at least 5 characters)")
    if len(comment) > 2000:
        raise invalid("comment", "Comment must be at most 2000 characters")
    now = now_iso()
    conn.execute("UPDATE integration_approvals SET decision = ?, approved_by = ?, decision_comment = ?, decided_at = ? WHERE id = ?",
                 (decision, ctx.user_id, comment or None, now, approval_id))
    adapter = get_or_404(conn, "integration_adapters", appr["adapter_id"], "Integration adapter")
    if decision == "approved":
        conn.execute("UPDATE integration_adapters SET mode = 'live_approved', active_approval_id = ? WHERE id = ?",
                     (approval_id, adapter["id"]))
    else:
        conn.execute("UPDATE integration_adapters SET mode = 'mock', active_approval_id = NULL WHERE id = ?", (adapter["id"],))
    audit_ctx(conn, ctx, "integration", f"integration.live_request_{decision}", entity_type="integration_approval",
              entity_id=approval_id, details={"adapter_id": adapter["id"], "provider": adapter["provider"],
                                              "environment": adapter["environment"], "comment": comment or None}, request=request)
    out = _adapter_payload(conn, adapter["id"])
    return {"approval": out["latest_approval"], "adapter": out}


class EnableIn(Body):
    approval_id: Optional[str] = None


@router.post("/admin/integrations/{adapter_id}/enable-live")
def enable_live(adapter_id: str, body: EnableIn, request: Request,
                ctx: AuthContext = Depends(require_staff("ops_admin", "integration_approver")),
                conn: sqlite3.Connection = Depends(get_db)):
    adapter = get_or_404(conn, "integration_adapters", adapter_id, "Integration adapter")
    appr = one(conn, "SELECT * FROM integration_approvals WHERE id = ? AND adapter_id = ?", (str(body.approval_id or ""), adapter_id))
    valid = (appr is not None and appr["decision"] == "approved" and adapter["mode"] == "live_approved"
             and adapter["active_approval_id"] == appr["id"])
    if not valid:
        raise forbidden("Live mode needs an approved, unused live request for this adapter", "no_valid_approval")
    missing = [n for n in PROVIDERS.get(adapter["provider"], {}).get("env", []) if not os.getenv(n)]
    if missing:
        raise api_error(422, "credentials_missing",
                        f"Cannot enable live: set these environment variables first: {', '.join(missing)}",
                        missing_env_vars=missing)
    conn.execute("UPDATE integration_adapters SET mode = 'live', last_health_check = ? WHERE id = ?", (now_iso(), adapter_id))
    audit_ctx(conn, ctx, "integration", "integration.live_enabled", entity_type="integration_adapter", entity_id=adapter_id,
              details={"provider": adapter["provider"], "environment": adapter["environment"], "approval_id": appr["id"]},
              request=request)
    return {"adapter": _adapter_payload(conn, adapter_id)}


class RevertIn(Body):
    reason: Optional[str] = None


@router.post("/admin/integrations/{adapter_id}/revert-to-mock")
def revert_to_mock(adapter_id: str, body: RevertIn, request: Request,
                   ctx: AuthContext = Depends(require_staff("ops_admin", "integration_approver")),
                   conn: sqlite3.Connection = Depends(get_db)):
    adapter = get_or_404(conn, "integration_adapters", adapter_id, "Integration adapter")
    reason = _text(body.reason, "reason", "Reason", 5, 1000)
    pending = one(conn, "SELECT id FROM integration_approvals WHERE adapter_id = ? AND decision = 'pending'", (adapter_id,))
    if adapter["mode"] == "mock" and not pending:
        raise api_error(409, "already_mock", "This adapter is already on mock")
    now = now_iso()
    if pending:
        conn.execute("""UPDATE integration_approvals SET decision = 'rejected', approved_by = ?, decision_comment = ?, decided_at = ?
                        WHERE id = ?""", (ctx.user_id, f"Withdrawn on revert to mock: {reason}", now, pending["id"]))
    conn.execute("UPDATE integration_adapters SET mode = 'mock', active_approval_id = NULL WHERE id = ?", (adapter_id,))
    audit_ctx(conn, ctx, "integration", "integration.reverted_to_mock", entity_type="integration_adapter", entity_id=adapter_id,
              details={"provider": adapter["provider"], "environment": adapter["environment"], "previous_mode": adapter["mode"],
                       "reason": reason, "withdrawn_approval_id": pending["id"] if pending else None}, request=request)
    return {"adapter": _adapter_payload(conn, adapter_id)}


# ================================================================== audit log
AUDIT_ROLES = ("soc_analyst", "ops_admin", "compliance_reviewer")
AUDIT_CATEGORIES = ("login", "financial", "admin", "content", "consent", "integration")
ACTOR_TYPES = ("customer", "staff", "system", "visitor")


def _audit_where(category, actor_id, actor_type, entity_type, entity_id, action, date_from, date_to) -> tuple[str, list]:
    where: list[str] = []
    params: list[Any] = []
    if category:
        where.append("category = ?")
        params.append(_choice(category, "category", AUDIT_CATEGORIES, "category"))
    if actor_type:
        where.append("actor_type = ?")
        params.append(_choice(actor_type, "actor_type", ACTOR_TYPES, "actor type"))
    for col, val in (("actor_id", actor_id), ("entity_type", entity_type), ("entity_id", entity_id)):
        if val and val.strip():
            where.append(f"{col} = ?")
            params.append(val.strip()[:100])
    if action and action.strip():
        prefix = action.strip()[:100].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("action LIKE ? ESCAPE '\\'")
        params.append(prefix + "%")
    if date_from:
        dt = parse_iso(date_from)
        if dt is None:
            raise invalid("from", "Enter a valid start date/time (IST)")
        where.append("occurred_at >= ?")
        params.append(iso(dt))
    if date_to:
        text = date_to.strip()
        dt = parse_iso(text)
        if dt is None:
            raise invalid("to", "Enter a valid end date/time (IST)")
        if len(text) == 10:  # a date: include the whole IST day
            dt = dt + timedelta(days=1)
        elif len(text) == 16:  # YYYY-MM-DDTHH:MM: include the whole minute
            dt = dt + timedelta(minutes=1)
        else:
            dt = dt + timedelta(seconds=1)
        where.append("occurred_at < ?")
        params.append(iso(dt))
    return (" WHERE " + " AND ".join(where)) if where else "", params


def _audit_item(r: dict, names: dict[str, str]) -> dict:
    return {
        "seq": r["seq"],
        "id": r["id"],
        "category": r["category"],
        "actor_type": r["actor_type"],
        "actor_id": r["actor_id"],
        "actor_name": names.get(r["actor_id"]) if r["actor_type"] == "staff" else None,
        "action": r["action"],
        "entity_type": r["entity_type"],
        "entity_id": r["entity_id"],
        "details": redact_obj(jload(r["details"], {})),
        "ip": r["ip"],
        "occurred_at": r["occurred_at"],
        "hash": r["hash"],
        "prev_hash": r["prev_hash"],
        "retain_until": r["retain_until"],
    }


@router.get("/admin/audit-events")
def audit_events(category: Optional[str] = None, actor_id: Optional[str] = None, actor_type: Optional[str] = None,
                 entity_type: Optional[str] = None, entity_id: Optional[str] = None, action: Optional[str] = None,
                 date_from: Optional[str] = Query(None, alias="from"), date_to: Optional[str] = Query(None, alias="to"),
                 page: int = Query(1, ge=1, le=100000), page_size: int = Query(50, ge=1, le=200),
                 ctx: AuthContext = Depends(require_staff(*AUDIT_ROLES)), conn: sqlite3.Connection = Depends(get_db)):
    where, params = _audit_where(category, actor_id, actor_type, entity_type, entity_id, action, date_from, date_to)
    total = conn.execute(f"SELECT COUNT(*) FROM audit_events{where}", params).fetchone()[0]
    rows = many(conn, f"SELECT * FROM audit_events{where} ORDER BY seq DESC LIMIT ? OFFSET ?",
                params + [page_size, (page - 1) * page_size])
    staff_ids = [r["actor_id"] for r in rows if r["actor_type"] == "staff"]
    names = _names(conn, staff_ids)
    oldest = conn.execute("SELECT MIN(retain_until) FROM audit_events").fetchone()[0]
    return {
        "items": [_audit_item(r, names) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "chain": verify_chain(conn),
        "retention": {
            "retention_days": RETENTION_DAYS,
            "earliest_retain_until": oldest,
            "policy": f"Append-only and hash-chained. Each event is retained for at least {RETENTION_DAYS} days "
                      "(CERT-In log retention) in an Indian region; details are PII-masked when written.",
        },
    }


def _csv_cell(v: Any) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s  # neutralise spreadsheet formulas


@router.get("/admin/audit-events/export")
def export_audit_events(request: Request, format: str = "csv", category: Optional[str] = None, actor_id: Optional[str] = None,
                        actor_type: Optional[str] = None, entity_type: Optional[str] = None, entity_id: Optional[str] = None,
                        action: Optional[str] = None, date_from: Optional[str] = Query(None, alias="from"),
                        date_to: Optional[str] = Query(None, alias="to"),
                        ctx: AuthContext = Depends(require_staff(*AUDIT_ROLES)), conn: sqlite3.Connection = Depends(get_db)):
    if format != "csv":
        raise invalid("format", "Only CSV export is supported (format=csv)")
    where, params = _audit_where(category, actor_id, actor_type, entity_type, entity_id, action, date_from, date_to)
    rows = many(conn, f"SELECT * FROM audit_events{where} ORDER BY seq ASC LIMIT 50000", params)
    names = _names(conn, [r["actor_id"] for r in rows if r["actor_type"] == "staff"])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["seq", "occurred_at_utc", "occurred_at_ist", "category", "actor_type", "actor_id", "actor_name", "action",
                "entity_type", "entity_id", "ip", "details", "prev_hash", "hash", "retain_until"])
    for r in rows:
        item = _audit_item(r, names)
        w.writerow([_csv_cell(x) for x in (
            item["seq"], item["occurred_at"], fmt_ist(item["occurred_at"]), item["category"], item["actor_type"], item["actor_id"],
            item["actor_name"], item["action"], item["entity_type"], item["entity_id"], item["ip"],
            json.dumps(item["details"], ensure_ascii=False, sort_keys=True), item["prev_hash"], item["hash"], item["retain_until"])])
    chain = verify_chain(conn)
    audit_ctx(conn, ctx, "admin", "audit.exported", entity_type="audit_events", entity_id=None,
              details={"rows": len(rows), "filters": {"category": category, "actor_id": actor_id, "actor_type": actor_type,
                                                      "entity_type": entity_type, "entity_id": entity_id, "action": action,
                                                      "from": date_from, "to": date_to},
                       "chain_verified": chain["verified"]}, request=request)
    name = f"audit-events-{today_ist().strftime('%Y%m%d')}.csv"
    return Response(content="﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ================================================================== incidents (CERT-In)
INCIDENT_ROLES = ("soc_analyst", "ops_admin")
SEVERITIES = ("low", "medium", "high", "critical")
CERT_IN_CATEGORIES = [
    ("targeted_scanning", "Targeted scanning / probing of critical networks or systems"),
    ("critical_system_compromise", "Compromise of critical systems / information"),
    ("unauthorised_access", "Unauthorised access to IT systems / data"),
    ("website_defacement", "Website defacement or intrusion (malicious code, unauthorised changes)"),
    ("malicious_code", "Malicious code attack (virus, worm, trojan, bots, spyware, ransomware, cryptominers)"),
    ("server_network_attack", "Attack on servers (database, mail, DNS) or network devices (routers)"),
    ("identity_theft_phishing", "Identity theft, spoofing and phishing attacks"),
    ("dos_ddos", "Denial of service (DoS) / distributed denial of service (DDoS)"),
    ("critical_infrastructure", "Attack on critical infrastructure, SCADA / OT systems or wireless networks"),
    ("application_attack", "Attack on applications (e-governance, e-commerce, etc.)"),
    ("data_breach", "Data breach"),
    ("data_leak", "Data leak"),
    ("iot_attack", "Attack on IoT devices and associated systems"),
    ("digital_payments", "Attack or incident affecting digital payment systems"),
    ("malicious_mobile_app", "Attack through malicious mobile apps"),
    ("fake_mobile_app", "Fake mobile apps"),
    ("social_media_access", "Unauthorised access to social media accounts"),
    ("cloud", "Attack / suspicious activity affecting cloud systems"),
    ("emerging_tech", "Attack affecting Big Data, blockchain, virtual assets, robotics, drones, 3D/4D printing"),
    ("ai_ml", "Attack affecting AI / ML systems"),
]
CERT_IN_CATEGORY_KEYS = [k for k, _ in CERT_IN_CATEGORIES]
CERT_IN_NOTE = "Categories follow Annexure I of the CERT-In Directions (28 April 2022). Verify with the current CERT-In directions."
INCIDENT_TRANSITIONS = {
    "detected": ("triaged", "reported_to_cert_in", "contained"),
    "triaged": ("reported_to_cert_in", "contained", "resolved"),
    "reported_to_cert_in": ("contained", "resolved"),
    "contained": ("reported_to_cert_in", "resolved"),
    "resolved": ("closed",),
    "closed": (),
}
OPEN_INCIDENT_STATUSES = ("detected", "triaged", "reported_to_cert_in", "contained")


def _incident_out(r: dict, names: dict[str, str] | None = None) -> dict:
    d = dict(r)
    d["notes"] = jload(r["notes"], [])
    due = parse_iso(r["cert_in_due_at"])
    now = utcnow()
    if r["reported_at"] or due is None:
        d["minutes_remaining"] = None
        d["overdue"] = False
    else:
        d["minutes_remaining"] = int((due - now).total_seconds() // 60)
        d["overdue"] = due <= now
    d["reported_late"] = bool(r["reported_at"] and due and parse_iso(r["reported_at"]) > due)
    d["is_open"] = r["status"] in OPEN_INCIDENT_STATUSES
    d["allowed_transitions"] = list(INCIDENT_TRANSITIONS.get(r["status"], ()))
    d["cert_in_category_label"] = dict(CERT_IN_CATEGORIES).get(r["cert_in_category"] or "", r["cert_in_category"])
    d["created_by_name"] = (names or {}).get(r["created_by"])
    return d


@router.get("/admin/incidents")
def list_incidents(status: Optional[str] = None, open_only: bool = False,
                   ctx: AuthContext = Depends(require_staff(*INCIDENT_ROLES)), conn: sqlite3.Connection = Depends(get_db)):
    where, params = [], []
    if status:
        where.append("status = ?")
        params.append(_choice(status, "status", tuple(INCIDENT_TRANSITIONS), "status"))
    if open_only:
        where.append(f"status IN ({','.join('?' * len(OPEN_INCIDENT_STATUSES))})")
        params.extend(OPEN_INCIDENT_STATUSES)
    sql = "SELECT * FROM incidents" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY detected_at DESC LIMIT 500"
    rows = many(conn, sql, params)
    names = _names(conn, [r["created_by"] for r in rows])
    items = [_incident_out(r, names) for r in rows]
    return {
        "items": items,
        "open_count": sum(1 for i in items if i["is_open"]),
        "overdue_count": sum(1 for i in items if i["overdue"]),
        "reporting_hours": get_reg_value(conn, "cert_in_reporting_hours", 6),
        "cert_in_categories": [{"value": k, "label": v} for k, v in CERT_IN_CATEGORIES],
        "cert_in_categories_note": CERT_IN_NOTE,
        "severities": list(SEVERITIES),
    }


class IncidentIn(Body):
    title: Optional[str] = None
    severity: Optional[str] = None
    cert_in_category: Optional[str] = None
    detected_at: Optional[str] = None
    note: Optional[str] = None


@router.post("/admin/incidents", status_code=201)
def create_incident(body: IncidentIn, request: Request, ctx: AuthContext = Depends(require_staff(*INCIDENT_ROLES)),
                    conn: sqlite3.Connection = Depends(get_db)):
    title = _text(body.title, "title", "Title", 5, 200)
    severity = _choice(body.severity, "severity", SEVERITIES, "severity")
    category = _choice(body.cert_in_category, "cert_in_category", CERT_IN_CATEGORY_KEYS, "CERT-In category")
    detected = _when(body.detected_at, "detected_at", "Detection time")
    if detected > utcnow() + timedelta(minutes=1):
        raise invalid("detected_at", "Detection time cannot be in the future")
    note = (body.note or "").strip()[:2000]
    hours = float(get_reg_value(conn, "cert_in_reporting_hours", 6) or 6)
    now = now_iso()
    iid = new_id()
    notes = [{"at": now, "actor_id": ctx.user_id, "actor_name": ctx.user.get("display_name"), "from_status": None,
              "to_status": "detected", "note": note or "Incident recorded"}]
    conn.execute(
        """INSERT INTO incidents (id, title, severity, cert_in_category, detected_at, cert_in_due_at, status, notes, created_by,
           created_at, updated_at) VALUES (?,?,?,?,?,?, 'detected', ?,?,?,?)""",
        (iid, title, severity, category, iso(detected), iso(detected + timedelta(hours=hours)), jdump(notes), ctx.user_id, now, now),
    )
    audit_ctx(conn, ctx, "admin", "incident.recorded", entity_type="incident", entity_id=iid,
              details={"severity": severity, "cert_in_category": category, "detected_at": iso(detected)}, request=request)
    row = one(conn, "SELECT * FROM incidents WHERE id = ?", (iid,))
    return _incident_out(row, _names(conn, [ctx.user_id]))  # type: ignore[arg-type]


class TransitionIn(Body):
    to_status: Optional[str] = None
    note: Optional[str] = None
    cert_in_reference: Optional[str] = None


@router.post("/admin/incidents/{incident_id}/transition")
def transition_incident(incident_id: str, body: TransitionIn, request: Request,
                        ctx: AuthContext = Depends(require_staff(*INCIDENT_ROLES)), conn: sqlite3.Connection = Depends(get_db)):
    inc = get_or_404(conn, "incidents", incident_id, "Incident")
    to = _choice(body.to_status, "to_status", ("triaged", "reported_to_cert_in", "contained", "resolved", "closed"), "status")
    allowed = INCIDENT_TRANSITIONS.get(inc["status"], ())
    if to not in allowed:
        hint = "close only after the incident is resolved" if to == "closed" else (
            f"allowed next steps: {', '.join(allowed)}" if allowed else "the incident is closed")
        raise api_error(422, "invalid_transition", f"Cannot move from {inc['status']} to {to}: {hint}", field="to_status")
    now = now_iso()
    note = (body.note or "").strip()[:2000]
    ref = (body.cert_in_reference or "").strip()
    updates = {"status": to, "updated_at": now}
    if to == "reported_to_cert_in":
        if len(ref) < 4 or len(ref) > 100:
            raise invalid("cert_in_reference", "Enter the CERT-In acknowledgement / reference number (4-100 characters)")
        updates["cert_in_reference"] = ref
        updates["reported_at"] = now
    notes = jload(inc["notes"], [])
    notes.append({"at": now, "actor_id": ctx.user_id, "actor_name": ctx.user.get("display_name"), "from_status": inc["status"],
                  "to_status": to, "note": note or None, "cert_in_reference": ref or None})
    updates["notes"] = jdump(notes)
    sets = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(f"UPDATE incidents SET {sets} WHERE id = ?", list(updates.values()) + [incident_id])
    audit_ctx(conn, ctx, "admin", f"incident.{to}", entity_type="incident", entity_id=incident_id,
              details={"from_status": inc["status"], "to_status": to, "cert_in_reference": ref or None, "note": note or None},
              request=request)
    row = one(conn, "SELECT * FROM incidents WHERE id = ?", (incident_id,))
    return _incident_out(row, _names(conn, [row["created_by"]]))  # type: ignore[index, arg-type]


# ================================================================== evidence register
EVIDENCE_ROLES = ("compliance_reviewer", "ops_admin", "soc_analyst")
EVIDENCE_TYPES = ("load_test", "dr_drill", "vapt", "test_run", "accessibility_audit", "architecture_review")
EVIDENCE_ENVS = ("dev", "qa", "uat", "production", "dr")
EVIDENCE_RESULTS = ("passed", "failed", "partial")
SRS_ID_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}(-[A-Z0-9]{1,10}){1,3}$")
TARGETS_NOTE = ("Targets are engineering targets until the bank approves them. Status is shown only from recorded, executed "
                "runs with an attached report; nothing is assumed.")


def _num(m: dict, key: str) -> float | None:
    v = m.get(key)
    if isinstance(v, bool):
        return None
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _all_known(*vals: float | None) -> bool:
    return all(v is not None for v in vals)


def _check_load(m: dict) -> bool | None:
    rps, p95 = _num(m, "rps"), _num(m, "p95_ms")
    return (rps >= 15000 and p95 < 500) if _all_known(rps, p95) else None  # type: ignore[operator]


def _check_dr(m: dict) -> bool | None:
    rto, rpo = _num(m, "rto_minutes"), _num(m, "rpo_minutes")
    return (rto <= 120 and rpo <= 15) if _all_known(rto, rpo) else None  # type: ignore[operator]


def _check_vapt(m: dict) -> bool | None:
    c, h = _num(m, "critical_findings"), _num(m, "high_findings")
    return (c == 0 and h == 0) if _all_known(c, h) else None


def _check_coverage(m: dict) -> bool | None:
    cov = _num(m, "coverage_percent")
    return cov >= 80 if cov is not None else None


EXPECTED_EVIDENCE = [
    {"key": "load_test", "srs_id": "US-28", "evidence_type": "load_test", "title": "Load test",
     "requirement": "15,000 requests per second sustained with p95 latency under 500 ms", "check": _check_load,
     "metrics_hint": ["rps", "p95_ms"]},
    {"key": "dr_drill", "srs_id": "US-29", "evidence_type": "dr_drill", "title": "Disaster recovery drill",
     "requirement": "Failover to DR with RTO of 2 hours or less and RPO of 15 minutes or less", "check": _check_dr,
     "metrics_hint": ["rto_minutes", "rpo_minutes"]},
    {"key": "vapt", "srs_id": None, "evidence_type": "vapt", "title": "VAPT",
     "requirement": "Vulnerability assessment and penetration test with zero open critical or high findings", "check": _check_vapt,
     "metrics_hint": ["critical_findings", "high_findings"]},
    {"key": "accessibility_audit", "srs_id": None, "evidence_type": "accessibility_audit", "title": "Accessibility audit",
     "requirement": "WCAG 2.2 AA conformance audit of public site, portal and staff console", "check": None, "metrics_hint": []},
    {"key": "test_run", "srs_id": None, "evidence_type": "test_run", "title": "Automated test run",
     "requirement": "Automated test suite run with code coverage of at least 80%", "check": _check_coverage,
     "metrics_hint": ["coverage_percent"]},
    {"key": "architecture_review", "srs_id": None, "evidence_type": "architecture_review", "title": "Architecture review",
     "requirement": "Data residency: all customer and payment data stored and processed in India", "check": None,
     "metrics_hint": []},
]


def _evidence_out(r: dict, names: dict[str, str]) -> dict:
    return {
        "id": r["id"],
        "evidence_type": r["evidence_type"],
        "srs_ids": jload(r["srs_ids"], []),
        "environment": r["environment"],
        "executed_at": r["executed_at"],
        "result": r["result"],
        "metrics": jload(r["metrics"], {}),
        "artifact_file_name": r["artifact_file_name"],
        "artifact_sha256": r["artifact_sha256"],
        "has_artifact": bool(r["artifact_storage_key"]),
        "recorded_by": r["recorded_by"],
        "recorded_by_name": names.get(r["recorded_by"]),
        "created_at": r["created_at"],
    }


def _srs_like(srs: str) -> str:
    return '%"' + srs.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + '"%'


@router.get("/admin/evidence")
def list_evidence(srs_id: Optional[str] = None, evidence_type: Optional[str] = None,
                  ctx: AuthContext = Depends(require_staff(*EVIDENCE_ROLES)), conn: sqlite3.Connection = Depends(get_db)):
    where, params = [], []
    srs = (srs_id or "").strip().upper()
    if srs:
        if not SRS_ID_RE.match(srs):
            raise invalid("srs_id", "Enter an SRS ID such as US-28")
        where.append("srs_ids LIKE ? ESCAPE '\\'")
        params.append(_srs_like(srs))
    if evidence_type:
        where.append("evidence_type = ?")
        params.append(_choice(evidence_type, "evidence_type", EVIDENCE_TYPES, "evidence type"))
    sql = "SELECT * FROM evidence_records" + (" WHERE " + " AND ".join(where) if where else "") + \
          " ORDER BY executed_at DESC, created_at DESC LIMIT 500"
    rows = many(conn, sql, params)
    all_rows = many(conn, "SELECT * FROM evidence_records ORDER BY executed_at DESC, created_at DESC")
    names = _names(conn, [r["recorded_by"] for r in rows + all_rows])
    matrix = []
    for item in EXPECTED_EVIDENCE:
        if srs and item["srs_id"] != srs:
            continue
        match = next((r for r in all_rows if r["evidence_type"] == item["evidence_type"]
                      and (item["srs_id"] is None or item["srs_id"] in jload(r["srs_ids"], []))), None)
        metrics = jload(match["metrics"], {}) if match else {}
        check = item["check"]
        matrix.append({
            "key": item["key"],
            "srs_id": item["srs_id"],
            "evidence_type": item["evidence_type"],
            "title": item["title"],
            "requirement": item["requirement"],
            "metrics_hint": item["metrics_hint"],
            "evidenced": bool(match),
            "status": f"Evidenced ({match['result']})" if match else "Not evidenced",
            "result": match["result"] if match else None,
            "record_id": match["id"] if match else None,
            "executed_at": match["executed_at"] if match else None,
            "environment": match["environment"] if match else None,
            "metrics": metrics,
            "target_met": (check(metrics) if (match and check) else None),
        })
    return {
        "records": [_evidence_out(r, names) for r in rows],
        "matrix": matrix,
        "evidenced_count": sum(1 for m in matrix if m["evidenced"]),
        "expected_count": len(matrix),
        "note": TARGETS_NOTE,
        "evidence_types": list(EVIDENCE_TYPES),
        "environments": list(EVIDENCE_ENVS),
        "results": list(EVIDENCE_RESULTS),
    }


class EvidenceIn(Body):
    evidence_type: Optional[str] = None
    srs_ids: Optional[str] = None
    environment: Optional[str] = None
    executed_at: Optional[str] = None
    result: Optional[str] = None
    metrics_json: Optional[str] = None
    artifact_upload_ref: Optional[str] = None


@router.post("/admin/evidence", status_code=201)
def record_evidence(body: EvidenceIn, request: Request, ctx: AuthContext = Depends(require_staff(*EVIDENCE_ROLES)),
                    conn: sqlite3.Connection = Depends(get_db)):
    etype = _choice(body.evidence_type, "evidence_type", EVIDENCE_TYPES, "evidence type")
    raw_ids = [s.strip().upper() for s in (body.srs_ids or "").split(",") if s.strip()]
    if not raw_ids:
        raise invalid("srs_ids", "Enter at least one SRS ID, for example US-28,US-29")
    bad = [s for s in raw_ids if not SRS_ID_RE.match(s)]
    if bad:
        raise invalid("srs_ids", f"Not a valid SRS ID: {', '.join(bad)} (use the form US-28)")
    srs_ids = list(dict.fromkeys(raw_ids))[:20]
    env = _choice(body.environment, "environment", EVIDENCE_ENVS, "environment")
    executed = _when(body.executed_at, "executed_at", "Execution time")
    if executed > utcnow():
        raise invalid("executed_at", "Evidence must come from a run that has already been executed (time is in the future)")
    result = _choice(body.result, "result", EVIDENCE_RESULTS, "result")
    metrics: dict = {}
    if body.metrics_json and body.metrics_json.strip():
        if len(body.metrics_json) > 20000:
            raise invalid("metrics_json", "Metrics JSON is too large (max 20,000 characters)")
        try:
            metrics = json.loads(body.metrics_json)
        except ValueError:
            raise invalid("metrics_json", "Metrics must be valid JSON, for example {\"p95_ms\": 420, \"rps\": 15200}")
        if not isinstance(metrics, dict):
            raise invalid("metrics_json", "Metrics must be a JSON object, for example {\"p95_ms\": 420}")
    if not body.artifact_upload_ref:
        raise invalid("artifact_upload_ref", "Attach the raw report from the executed run")
    upload = consume_upload(conn, body.artifact_upload_ref, ctx.user_id, field="artifact_upload_ref")
    if upload.get("consumed_at"):
        raise invalid("artifact_upload_ref", "This file was already used for another record. Upload the report again.")
    eid = new_id()
    now = now_iso()
    conn.execute(
        """INSERT INTO evidence_records (id, evidence_type, srs_ids, environment, executed_at, result, metrics, artifact_storage_key,
           artifact_file_name, artifact_sha256, recorded_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (eid, etype, jdump(srs_ids), env, iso(executed), result, jdump(metrics), upload["storage_key"], upload["file_name"],
         upload["sha256"], ctx.user_id, now),
    )
    conn.execute(
        """INSERT INTO documents (id, owner_type, owner_id, doc_type, locale, storage_key, sha256, file_name, content_type,
           size_bytes, created_at) VALUES (?, 'evidence', ?, 'evidence_artifact', 'en', ?,?,?,?,?,?)""",
        (new_id(), eid, upload["storage_key"], upload["sha256"], upload["file_name"], upload["content_type"],
         upload["size_bytes"], now),
    )
    audit_ctx(conn, ctx, "admin", "evidence.recorded", entity_type="evidence_record", entity_id=eid,
              details={"evidence_type": etype, "srs_ids": srs_ids, "environment": env, "result": result,
                       "executed_at": iso(executed), "artifact_sha256": upload["sha256"]}, request=request)
    row = one(conn, "SELECT * FROM evidence_records WHERE id = ?", (eid,))
    return _evidence_out(row, _names(conn, [ctx.user_id]))  # type: ignore[arg-type]


@router.get("/admin/evidence/{evidence_id}/artifact")
def evidence_artifact(evidence_id: str, request: Request, ctx: AuthContext = Depends(require_staff(*EVIDENCE_ROLES)),
                      conn: sqlite3.Connection = Depends(get_db)):
    rec = get_or_404(conn, "evidence_records", evidence_id, "Evidence record")
    try:
        data = get_bytes(rec["artifact_storage_key"])
    except FileNotFoundError:
        raise api_error(404, "artifact_missing", "The artifact file could not be found in storage")
    doc = one(conn, "SELECT content_type FROM documents WHERE storage_key = ? LIMIT 1", (rec["artifact_storage_key"],))
    ctype = (doc or {}).get("content_type") or "application/octet-stream"
    name = re.sub(r'[^A-Za-z0-9._\- ]', "_", rec["artifact_file_name"] or f"evidence-{evidence_id}")[:120] or "evidence"
    audit_ctx(conn, ctx, "admin", "evidence.artifact_downloaded", entity_type="evidence_record", entity_id=evidence_id,
              request=request)
    return Response(content=data, media_type=ctype,
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "X-Content-SHA256": rec["artifact_sha256"] or ""})


# ================================================================== maintenance notices
MAINT_SERVICES = ("website", "internet_banking", "upi", "cards")
MAX_WINDOW_HOURS = 24


def maintenance_state(r: dict, now: datetime | None = None) -> str:
    now = now or utcnow()
    s, e = parse_iso(r["starts_at"]), parse_iso(r["ends_at"])
    if s and e and s <= now < e:
        return "ongoing"
    if s and s > now:
        return "upcoming"
    return "completed"


def notice_lead_hours(r: dict) -> float:
    s, c = parse_iso(r["starts_at"]), parse_iso(r["created_at"])
    return round((s - c).total_seconds() / 3600, 1) if s and c else 0.0


class NoticeIn(Body):
    service: Optional[str] = None
    starts_at: Optional[str] = None
    ends_at: Optional[str] = None
    message_en: Optional[str] = None
    message_hi: Optional[str] = None


@router.post("/admin/maintenance-notices", status_code=201)
def create_notice(body: NoticeIn, request: Request, ctx: AuthContext = Depends(require_staff("ops_admin")),
                  conn: sqlite3.Connection = Depends(get_db)):
    service = _choice(body.service, "service", MAINT_SERVICES, "service")
    starts = _when(body.starts_at, "starts_at", "Start time")
    ends = _when(body.ends_at, "ends_at", "End time")
    hours = float(get_reg_value(conn, "maintenance_notice_hours", 48) or 48)
    earliest = utcnow() + timedelta(hours=hours)
    if starts < earliest:
        raise invalid("starts_at", f"Planned downtime must be announced at least {hours:g} hours ahead. "
                                   f"The earliest allowed start is {fmt_ist(earliest)}.")
    if ends <= starts:
        raise invalid("ends_at", "End time must be after the start time")
    if ends - starts > timedelta(hours=MAX_WINDOW_HOURS):
        raise invalid("ends_at", f"A maintenance window can be at most {MAX_WINDOW_HOURS} hours long")
    msg_en = _text(body.message_en, "message_en", "English message", 10, 500)
    msg_hi = _text(body.message_hi, "message_hi", "Hindi message", 10, 500)
    overlap = one(conn, "SELECT id FROM maintenance_notices WHERE service = ? AND starts_at < ? AND ends_at > ?",
                  (service, iso(ends), iso(starts)))
    if overlap:
        raise api_error(409, "overlapping_notice", "Another maintenance window for this service overlaps these times",
                        field="starts_at", notice_id=overlap["id"])
    nid = new_id()
    now = now_iso()
    conn.execute(
        """INSERT INTO maintenance_notices (id, service, starts_at, ends_at, message_en, message_hi, created_by, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (nid, service, iso(starts), iso(ends), msg_en, msg_hi, ctx.user_id, now),
    )
    audit_ctx(conn, ctx, "admin", "maintenance.published", entity_type="maintenance_notice", entity_id=nid,
              details={"service": service, "starts_at": iso(starts), "ends_at": iso(ends)}, request=request)
    row = one(conn, "SELECT * FROM maintenance_notices WHERE id = ?", (nid,))
    return _notice_out(row, _names(conn, [ctx.user_id]), hours)  # type: ignore[arg-type]


def _notice_out(r: dict, names: dict[str, str], min_hours: float) -> dict:
    lead = notice_lead_hours(r)
    return {**r, "state": maintenance_state(r), "created_by_name": names.get(r["created_by"]), "notice_hours": lead,
            "meets_notice_rule": lead >= min_hours}


@router.get("/admin/maintenance-notices")
def staff_notices(ctx: AuthContext = Depends(require_staff("ops_admin")), conn: sqlite3.Connection = Depends(get_db)):
    rows = many(conn, "SELECT * FROM maintenance_notices ORDER BY starts_at DESC LIMIT 500")
    names = _names(conn, [r["created_by"] for r in rows])
    hours = float(get_reg_value(conn, "maintenance_notice_hours", 48) or 48)
    return {"items": [_notice_out(r, names, hours) for r in rows], "minimum_notice_hours": hours,
            "max_window_hours": MAX_WINDOW_HOURS, "services": list(MAINT_SERVICES)}


# ================================================================== staff users
def _staff_out(r: dict) -> dict:
    return {
        "id": r["id"],
        "email": r["email"],
        "display_name": r["display_name"],
        "mobile_masked": mask_mobile(r["mobile"]),
        "roles": jload(r["staff_roles"], []),
        "status": r["status"],
        "locked_until": r["locked_until"],
        "last_login_at": r["last_login_at"],
        "last_login_ip": r["last_login_ip"],
        "created_at": r["created_at"],
    }


def _parse_roles(value: Any) -> list[str]:
    items = value if isinstance(value, list) else str(value or "").split(",")
    roles = list(dict.fromkeys(str(x).strip() for x in items if str(x).strip()))
    if not roles:
        raise invalid("roles", "Choose at least one role")
    unknown = [r for r in roles if r not in STAFF_ROLES]
    if unknown:
        raise invalid("roles", f"Unknown role(s): {', '.join(unknown)}")
    return roles


def _temp_password() -> str:
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = ("".join(secrets.choice(alphabet) for _ in range(12)) + "-" + secrets.choice(string.digits)
              + secrets.choice(string.ascii_uppercase) + secrets.choice(string.ascii_lowercase))
        if password_problem(pw) is None:
            return pw


@router.get("/admin/staff-users")
def list_staff(ctx: AuthContext = Depends(require_staff("ops_admin")), conn: sqlite3.Connection = Depends(get_db)):
    rows = many(conn, "SELECT * FROM users WHERE user_type = 'staff' ORDER BY display_name COLLATE NOCASE, email")
    return {"items": [_staff_out(r) for r in rows], "roles": sorted(STAFF_ROLES)}


class StaffIn(Body):
    email: Optional[str] = None
    mobile: Optional[str] = None
    display_name: Optional[str] = None
    roles: Optional[Any] = None


@router.post("/admin/staff-users", status_code=201)
def create_staff(body: StaffIn, request: Request, ctx: AuthContext = Depends(require_staff("ops_admin")),
                 conn: sqlite3.Connection = Depends(get_db)):
    email = (body.email or "").strip().lower()
    if not valid_email(email):
        raise invalid("email", "Enter a valid email address")
    mobile = require_mobile(body.mobile)
    roles = _parse_roles(body.roles)
    name = (body.display_name or "").strip()[:120] or None
    if one(conn, "SELECT id FROM users WHERE email = ?", (email,)):
        raise api_error(409, "email_taken", "A user with this email already exists", field="email")
    password = _temp_password()
    uid = new_id()
    conn.execute(
        """INSERT INTO users (id, user_type, email, mobile, password_hash, display_name, staff_roles, status, created_at)
           VALUES (?, 'staff', ?, ?, ?, ?, ?, 'active', ?)""",
        (uid, email, mobile, hash_password(password), name, jdump(roles), now_iso()),
    )
    audit_ctx(conn, ctx, "admin", "staff_user.created", entity_type="user", entity_id=uid,
              details={"email": email, "roles": roles}, request=request)
    row = one(conn, "SELECT * FROM users WHERE id = ?", (uid,))
    return {
        "user": _staff_out(row),  # type: ignore[arg-type]
        "temporary_password": password,
        "delivery_note": "MOCK delivery: in production the one-time password is delivered securely out of band. "
                         "It is shown only once and is stored only as an Argon2id hash. The user must enrol MFA at first sign-in.",
    }


class StaffUpdateIn(Body):
    roles: Optional[Any] = None
    status: Optional[str] = None


@router.put("/admin/staff-users/{user_id}")
def update_staff(user_id: str, body: StaffUpdateIn, request: Request, ctx: AuthContext = Depends(require_staff("ops_admin")),
                 conn: sqlite3.Connection = Depends(get_db)):
    user = get_or_404(conn, "users", user_id, "Staff user")
    if user["user_type"] != "staff":
        raise api_error(404, "not_found", "Staff user not found")
    changes: dict[str, Any] = {}
    old_roles = jload(user["staff_roles"], [])
    if body.roles is not None:
        roles = _parse_roles(body.roles)
        if user_id == ctx.user_id and "ops_admin" in old_roles and "ops_admin" not in roles:
            raise forbidden("You cannot remove your own ops_admin role", "self_role_removal")
        changes["staff_roles"] = jdump(roles)
    if body.status is not None:
        status = _choice(body.status, "status", ("active", "disabled"), "status")
        if user_id == ctx.user_id and status != "active":
            raise forbidden("You cannot disable your own account", "self_disable")
        changes["status"] = status
        if status == "active":
            changes["failed_login_count"] = 0
            changes["locked_until"] = None
    if not changes:
        raise invalid("roles", "Nothing to change")
    sets = ", ".join(f"{k} = ?" for k in changes)
    conn.execute(f"UPDATE users SET {sets} WHERE id = ?", list(changes.values()) + [user_id])
    if changes.get("status") == "disabled":
        for s in many(conn, "SELECT id FROM sessions WHERE user_id = ? AND status = 'active'", (user_id,)):
            end_session(conn, s["id"], "terminated", "disabled_by_admin")
    audit_ctx(conn, ctx, "admin", "staff_user.updated", entity_type="user", entity_id=user_id,
              details={"old_roles": old_roles, "new_roles": jload(changes.get("staff_roles"), None),
                       "status": changes.get("status")}, request=request)
    return {"user": _staff_out(one(conn, "SELECT * FROM users WHERE id = ?", (user_id,)))}  # type: ignore[arg-type]


# ================================================================== seed (synthetic)
def _seed_admin(conn: sqlite3.Connection) -> None:
    def uid(email: str) -> str | None:
        r = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        return r["id"] if r else None

    ops, soc = uid("ops@example.bank.in"), uid("soc@example.bank.in")
    now = utcnow()
    now_s = iso(now)
    hours = float(get_reg_value(conn, "cert_in_reporting_hours", 6) or 6)

    # Planned maintenance for internet banking ~5 days ahead, 02:00-04:00 IST.
    day = today_ist() + timedelta(days=5)
    start = datetime.combine(day, time(2, 0), tzinfo=IST)
    conn.execute(
        """INSERT INTO maintenance_notices (id, service, starts_at, ends_at, message_en, message_hi, created_by, created_at)
           VALUES (?, 'internet_banking', ?, ?, ?, ?, ?, ?)""",
        (new_id(), iso(start), iso(start + timedelta(hours=2)),
         "Internet banking will be unavailable for scheduled system maintenance. UPI, cards and ATMs keep working.",
         "निर्धारित सिस्टम रखरखाव के कारण इंटरनेट बैंकिंग उपलब्ध नहीं रहेगी। UPI, कार्ड और एटीएम सामान्य रूप से काम करते रहेंगे।",
         ops, now_s),
    )

    # A resolved past incident (reported within the CERT-In window) and an open triaged one (countdown running).
    det1 = now - timedelta(days=21, hours=3)
    rep1 = det1 + timedelta(hours=3, minutes=40)
    notes1 = [
        {"at": iso(det1 + timedelta(minutes=5)), "actor_id": soc, "actor_name": "Deepa Menon", "from_status": None, "to_status": "detected",
         "note": "WAF alerted on a surge of requests to the login endpoint"},
        {"at": iso(det1 + timedelta(minutes=30)), "actor_id": soc, "actor_name": "Deepa Menon", "from_status": "detected", "to_status": "triaged",
         "note": "Confirmed volumetric DDoS against the public website; customer data not affected"},
        {"at": iso(rep1), "actor_id": soc, "actor_name": "Deepa Menon", "from_status": "triaged", "to_status": "reported_to_cert_in",
         "note": "Reported to CERT-In (synthetic reference)", "cert_in_reference": "CERTIN-SYN-2026-0912"},
        {"at": iso(rep1 + timedelta(hours=1)), "actor_id": soc, "actor_name": "Deepa Menon", "from_status": "reported_to_cert_in",
         "to_status": "contained", "note": "Scrubbing enabled at the CDN"},
        {"at": iso(rep1 + timedelta(hours=5)), "actor_id": soc, "actor_name": "Deepa Menon", "from_status": "contained",
         "to_status": "resolved", "note": "Traffic back to normal for 4 hours"},
    ]
    conn.execute(
        """INSERT INTO incidents (id, title, severity, cert_in_category, detected_at, cert_in_due_at, reported_at, cert_in_reference,
           status, notes, created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?, 'resolved', ?,?,?,?)""",
        (new_id(), "DDoS against public website login page (synthetic)", "high", "dos_ddos", iso(det1),
         iso(det1 + timedelta(hours=hours)), iso(rep1), "CERTIN-SYN-2026-0912", jdump(notes1), soc, iso(det1),
         iso(rep1 + timedelta(hours=5))),
    )
    det2 = now - timedelta(hours=1, minutes=50)
    notes2 = [
        {"at": iso(det2 + timedelta(minutes=3)), "actor_id": soc, "actor_name": "Deepa Menon", "from_status": None, "to_status": "detected",
         "note": "Customer reports of an SMS phishing campaign using the bank's name"},
        {"at": iso(det2 + timedelta(minutes=25)), "actor_id": soc, "actor_name": "Deepa Menon", "from_status": "detected",
         "to_status": "triaged", "note": "Look-alike domain identified; takedown requested (synthetic)"},
    ]
    conn.execute(
        """INSERT INTO incidents (id, title, severity, cert_in_category, detected_at, cert_in_due_at, status, notes, created_by,
           created_at, updated_at) VALUES (?,?,?,?,?,?, 'triaged', ?,?,?,?)""",
        (new_id(), "Phishing SMS campaign impersonating the bank (synthetic)", "medium", "identity_theft_phishing", iso(det2),
         iso(det2 + timedelta(hours=hours)), jdump(notes2), soc, iso(det2), iso(det2 + timedelta(minutes=25))),
    )

    # One pending live request for the credit bureau adapter in UAT, so the approver flow can be demonstrated.
    bureau = conn.execute("SELECT id FROM integration_adapters WHERE provider = 'bureau' AND environment = 'uat'").fetchone()
    if bureau and ops:
        conn.execute(
            """INSERT INTO integration_approvals (id, adapter_id, requested_by, evidence_refs, justification, decision, requested_at)
               VALUES (?,?,?,?,?, 'pending', ?)""",
            (new_id(), bureau["id"], ops, "UAT certification report BUR-UAT-CERT-001 (synthetic); InfoSec sign-off ISR-2026-044 (synthetic)",
             "Bureau UAT certification complete; enable live bureau pulls in UAT for end-to-end loan journey testing.", now_s),
        )
        conn.execute("UPDATE integration_adapters SET mode = 'live_requested' WHERE id = ?", (bureau["id"],))
    # Deliberately no evidence records: the register must show "Not evidenced" until a real run is recorded.


register_seed(90, "admin", _seed_admin)

"""Shared helpers: ids, time (stored UTC, displayed IST), errors, masking, validation."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import string
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from fastapi import HTTPException

IST = timezone(timedelta(hours=5, minutes=30))
APP_ENV = os.getenv("APP_ENV", "dev")
MOCK_OTP_CODE = os.getenv("MOCK_OTP_CODE", "123456")
BRAND_NAME = os.getenv("BRAND_NAME", "Example Bank")


# ------------------------------------------------------------------ ids & time
def new_id() -> str:
    return str(uuid.uuid4())


def is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def now_iso() -> str:
    return iso(utcnow())  # type: ignore[return-value]


def parse_iso(value: str | None) -> datetime | None:
    """Parse a stored or client timestamp. Naive values are treated as IST (user input)."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(timezone.utc)


def parse_date(value: str | None) -> date | None:
    """Accept YYYY-MM-DD or DD/MM/YYYY."""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def today_ist() -> date:
    return utcnow().astimezone(IST).date()


def fmt_ist(value: datetime | str | None) -> str:
    """DD/MM/YYYY HH:MM IST for human-facing text (PDFs, alerts)."""
    dt = parse_iso(value) if isinstance(value, str) else value
    if dt is None:
        return "-"
    return dt.astimezone(IST).strftime("%d/%m/%Y %H:%M IST")


def fmt_date(value: date | str | None) -> str:
    d = parse_date(value) if isinstance(value, str) else value
    return d.strftime("%d/%m/%Y") if d else "-"


def financial_year(d: date | None = None) -> str:
    d = d or today_ist()
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


# ------------------------------------------------------------------ errors
def api_error(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    detail = {"code": code, "message": message}
    detail.update(extra)
    return HTTPException(status_code=status, detail=detail)


def not_found(entity: str = "Resource") -> HTTPException:
    return api_error(404, "not_found", f"{entity} not found")


def forbidden(message: str = "You are not allowed to access this resource", code: str = "forbidden") -> HTTPException:
    return api_error(403, code, message)


def invalid(field: str, message: str, code: str = "validation_error") -> HTTPException:
    """422 tied to a single field so the UI can show the message inline."""
    return api_error(422, code, message, field=field)


# ------------------------------------------------------------------ rows & json
def jdump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def jload(value: Any, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def row_dict(row: sqlite3.Row | None, json_fields: Iterable[str] = (), bool_fields: Iterable[str] = ()) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for f in json_fields:
        if f in d:
            d[f] = jload(d[f], [] if f.endswith("s") else {})
    for f in bool_fields:
        if f in d and d[f] is not None:
            d[f] = bool(d[f])
    return d


def one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> dict | None:
    row = conn.execute(sql, tuple(params)).fetchone()
    return dict(row) if row else None


def many(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def get_or_404(conn: sqlite3.Connection, table: str, entity_id: str, entity: str | None = None) -> dict:
    """Fetch a row by id; malformed or unknown ids are 404. ``table`` is a trusted literal."""
    if not entity_id or len(str(entity_id)) > 64:
        raise not_found(entity or table)
    row = one(conn, f"SELECT * FROM {table} WHERE id = ?", (str(entity_id),))
    if not row:
        raise not_found(entity or table.rstrip("s").replace("_", " ").capitalize())
    return row


# ------------------------------------------------------------------ references
def reference_no(prefix: str) -> str:
    """Human-friendly unique reference, e.g. CMP-20261012-7KQ2ZD."""
    alphabet = string.ascii_uppercase.replace("O", "").replace("I", "") + "23456789"
    tail = "".join(secrets.choice(alphabet) for _ in range(6))
    return f"{prefix}-{today_ist().strftime('%Y%m%d')}-{tail}"


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def request_fingerprint(payload: dict) -> str:
    """Stable hash of a request body, used to detect idempotency-key reuse with a different payload."""
    return sha256_hex(json.dumps(payload, sort_keys=True, default=str))


# ------------------------------------------------------------------ validation
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
PIN_RE = re.compile(r"^[1-9][0-9]{5}$")
VPA_RE = re.compile(r"^[a-zA-Z0-9.\-_]{2,256}@[a-zA-Z][a-zA-Z0-9]{1,64}$")
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
SWIFT_RE = re.compile(r"^[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?$")
ACCOUNT_RE = re.compile(r"^[0-9]{6,20}$")


def valid_email(v: str | None) -> bool:
    return bool(v) and bool(EMAIL_RE.match(v.strip())) and len(v) <= 254


def normalize_mobile(v: str | None) -> str | None:
    """Return E.164 (+91XXXXXXXXXX for Indian numbers) or None when invalid."""
    if not v:
        return None
    digits = re.sub(r"[\s\-()]", "", str(v))
    if re.fullmatch(r"[6-9][0-9]{9}", digits):
        return "+91" + digits
    if re.fullmatch(r"0[6-9][0-9]{9}", digits):
        return "+91" + digits[1:]
    if re.fullmatch(r"91[6-9][0-9]{9}", digits):
        return "+" + digits
    if re.fullmatch(r"\+[1-9][0-9]{7,14}", digits):
        return digits
    return None


def require_mobile(v: str | None, field: str = "mobile") -> str:
    m = normalize_mobile(v)
    if not m:
        raise invalid(field, "Enter a valid mobile number (10 digits, or international format with +country code)")
    return m


def valid_pan(v: str | None) -> bool:
    return bool(v) and bool(PAN_RE.match(v.strip().upper()))


def valid_ifsc(v: str | None) -> bool:
    return bool(v) and bool(IFSC_RE.match(v.strip().upper()))


def valid_pin(v: str | None) -> bool:
    return bool(v) and bool(PIN_RE.match(v.strip()))


def valid_vpa(v: str | None) -> bool:
    return bool(v) and bool(VPA_RE.match(v.strip()))


def valid_gstin(v: str | None) -> bool:
    return bool(v) and bool(GSTIN_RE.match(v.strip().upper()))


def valid_swift(v: str | None) -> bool:
    return bool(v) and bool(SWIFT_RE.match(v.strip().upper()))


# Verhoeff checksum (used by Aadhaar numbers)
_VD = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_VP = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]
_VINV = [0, 4, 3, 2, 1, 5, 6, 7, 8, 9]


def verhoeff_valid(number: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(number)):
        c = _VD[c][_VP[i % 8][int(ch)]]
    return c == 0


def verhoeff_check_digit(number: str) -> str:
    c = 0
    for i, ch in enumerate(reversed(number)):
        c = _VD[c][_VP[(i + 1) % 8][int(ch)]]
    return str(_VINV[c])


def valid_aadhaar(v: str | None) -> bool:
    if not v:
        return False
    digits = re.sub(r"\s", "", v)
    return bool(re.fullmatch(r"[2-9][0-9]{11}", digits)) and verhoeff_valid(digits)


def sample_aadhaar() -> str:
    """A synthetic, checksum-valid 12-digit number for mock/UAT use only."""
    base = "9" + "".join(secrets.choice("0123456789") for _ in range(10))
    return base + verhoeff_check_digit(base)


def parse_percent(value: Any, field: str, lo: float = 0.0, hi: float = 100.0) -> float:
    try:
        f = round(float(str(value).strip().rstrip("%")), 2)
    except (TypeError, ValueError):
        raise invalid(field, "Enter a number, for example 7.25")
    if not (lo <= f <= hi):
        raise invalid(field, f"Must be between {lo} and {hi}")
    return f


def positive_paise(value: Any, field: str, maximum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise invalid(field, "Enter an amount greater than zero")
    if maximum is not None and value > maximum:
        raise invalid(field, f"Amount exceeds the maximum of {format_inr(maximum)}")
    return value


# ------------------------------------------------------------------ masking
def mask_mobile(m: str | None) -> str:
    if not m:
        return ""
    return m[:-10] + "XXXXXX" + m[-4:] if len(m) >= 10 else "XXXX"


def mask_email(e: str | None) -> str:
    if not e or "@" not in e:
        return ""
    local, domain = e.split("@", 1)
    return (local[:2] + "***" if len(local) > 2 else local[:1] + "***") + "@" + domain


def mask_pan(p: str | None) -> str:
    if not p:
        return ""
    p = p.upper()
    return p[:3] + "XXXX" + p[-3:] if len(p) == 10 else "XXXXXXXXXX"


def mask_account(n: str | None) -> str:
    return "XXXX" + n[-4:] if n and len(n) >= 4 else "XXXX"


def mask_aadhaar_last4(last4: str | None) -> str:
    return f"XXXX XXXX {last4}" if last4 else ""


_AADHAAR_IN_TEXT = re.compile(r"(?<!\d)([2-9]\d{3})[\s-]?(\d{4})[\s-]?(\d{4})(?!\d)")
_CARD_IN_TEXT = re.compile(r"(?<!\d)(\d{4})[\s-]?(\d{4})[\s-]?(\d{4})[\s-]?(\d{4})(?!\d)")
_PAN_IN_TEXT = re.compile(r"\b([A-Z]{5})([0-9]{4})([A-Z])\b")


def redact_pii(text: str) -> str:
    """Mask Aadhaar, card and PAN numbers anywhere in free text (logs, audit details, chat)."""
    if not text:
        return text
    text = _CARD_IN_TEXT.sub(lambda m: "XXXX XXXX XXXX " + m.group(4), text)
    text = _AADHAAR_IN_TEXT.sub(lambda m: "XXXX XXXX " + m.group(3), text)
    text = _PAN_IN_TEXT.sub(lambda m: m.group(1)[:3] + "XXXX" + m.group(2)[-1:] + m.group(3), text)
    return text


def redact_obj(value: Any) -> Any:
    if isinstance(value, str):
        return redact_pii(value)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            lk = str(k).lower()
            if lk in {"password", "otp", "pin", "cvv", "aadhaar_number", "token", "access_token", "mfa_token", "attestation", "response"}:
                out[k] = "[REDACTED]"
            else:
                out[k] = redact_obj(v)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_obj(v) for v in value]
    return value


# ------------------------------------------------------------------ money
def format_inr(paise: int | None) -> str:
    """Indian digit grouping for text output (PDFs, alerts): Rs. 12,34,567.89."""
    if paise is None:
        return "-"
    neg = paise < 0
    rupees, p = divmod(abs(int(paise)), 100)
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
    return ("-" if neg else "") + f"Rs. {s}.{p:02d}"


# ------------------------------------------------------------------ regulatory values (CMS-held)
def get_reg_value(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    """Read a published ``regulatory_value`` content item (slug ``reg-<key>``, locale en).

    Values such as the DICGC limit or the TCS rate are configurable in the CMS and
    flagged for verification by the bank's compliance team.
    """
    row = conn.execute(
        """SELECT v.body FROM content_items i JOIN content_versions v ON v.id = i.published_version_id
           WHERE i.content_type = 'regulatory_value' AND i.slug = ? AND i.locale = 'en'""",
        (f"reg-{key}",),
    ).fetchone()
    if not row:
        return default
    body = jload(row["body"], {})
    return body.get("value", default)


def get_reg_value_meta(conn: sqlite3.Connection, key: str) -> dict | None:
    row = conn.execute(
        """SELECT v.body, v.published_at, i.needs_compliance_verification FROM content_items i
           JOIN content_versions v ON v.id = i.published_version_id
           WHERE i.content_type = 'regulatory_value' AND i.slug = ? AND i.locale = 'en'""",
        (f"reg-{key}",),
    ).fetchone()
    if not row:
        return None
    body = jload(row["body"], {})
    return {
        "key": key,
        "value": body.get("value"),
        "unit": body.get("unit"),
        "label": body.get("label"),
        "source": body.get("source"),
        "published_at": row["published_at"],
        "needs_compliance_verification": bool(row["needs_compliance_verification"]),
    }


def client_ip(request: Any) -> str | None:
    if request is None:
        return None
    fwd = request.headers.get("x-forwarded-for") if hasattr(request, "headers") else None
    if fwd:
        return fwd.split(",")[0].strip()
    client = getattr(request, "client", None)
    return client.host if client else None

"""Branch & ATM locator, IFSC lookup and bank holidays (public, no login).

All branch, ATM and holiday data is synthetic sample data. Holidays are to be
maintained in the CMS by the operations team; ``open_now`` is computed in IST from the
weekly timings, the second/fourth Saturday rule for branches and state holidays.
"""
from __future__ import annotations

import math
import re
import sqlite3
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query

from core import BRAND_NAME, IFSC_RE, IST, PIN_RE, api_error, invalid, jdump, jload, many, new_id, not_found, one, today_ist, utcnow
from db import get_db, register_seed

router = APIRouter(tags=["branches"])

HOLIDAY_DATA_NOTE = "Synthetic sample — to be maintained in the CMS"
BRANCH_DATA_NOTE = "Synthetic sample branch and ATM data for demonstration only"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SCHEMA_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
NEARBY_ATM_KM = 5.0
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
STATE_RE = re.compile(r"^[A-Za-z][A-Za-z .&-]{1,59}$")
SPAN_RE = re.compile(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})$")
KINDS = ("branch", "atm", "all")

# Approximate centroids for PIN prefixes (first three digits = sorting district). Used to
# geocode a PIN code that has no branch of its own.
PIN_PREFIX_CENTROIDS: dict[str, tuple[float, float, str]] = {
    "400": (19.0760, 72.8777, "Mumbai"),
    "401": (19.2813, 72.8557, "Mira-Bhayandar / Vasai"),
    "421": (19.2183, 72.9781, "Thane"),
    "410": (19.0330, 73.0297, "Navi Mumbai"),
    "411": (18.5204, 73.8567, "Pune"),
    "412": (18.6298, 73.7997, "Pimpri-Chinchwad"),
    "110": (28.6139, 77.2090, "Delhi"),
    "122": (28.4595, 77.0266, "Gurugram"),
    "201": (28.5355, 77.3910, "Noida / Ghaziabad"),
    "560": (12.9716, 77.5946, "Bengaluru"),
    "600": (13.0827, 80.2707, "Chennai"),
    "700": (22.5726, 88.3639, "Kolkata"),
    "500": (17.3850, 78.4867, "Hyderabad"),
    "380": (23.0225, 72.5714, "Ahmedabad"),
    "302": (26.9124, 75.7873, "Jaipur"),
    "226": (26.8467, 80.9462, "Lucknow"),
}

# ------------------------------------------------------------------ seed data (synthetic)
BRANCH_HOURS = {"mon": "09:30-16:30", "tue": "09:30-16:30", "wed": "09:30-16:30", "thu": "09:30-16:30",
                "fri": "09:30-16:30", "sat": "09:30-14:00", "sun": None}
EXTENDED_HOURS = {"mon": "09:00-18:00", "tue": "09:00-18:00", "wed": "09:00-18:00", "thu": "09:00-18:00",
                  "fri": "09:00-18:00", "sat": "09:30-16:30", "sun": None}
ATM_HOURS = {d: "00:00-24:00" for d in DAYS}

BASE_SERVICES = ["account_opening", "cash_deposit", "cash_withdrawal", "cheque_deposit", "demand_draft",
                 "fixed_deposits", "loans", "passbook_printing", "card_services"]

# (kind, slug, name, name_hi, ifsc, micr, address, city, state, pin, lat, lng, phone, extended_hours,
#  locker, wheelchair, forex, cash_deposit, extra_services)
SEED_BRANCHES = [
    ("branch", "mumbai-fort-branch", "Fort Branch, Mumbai", "फोर्ट शाखा, मुंबई", "EXMB0000101", "400987101",
     "Ground Floor, Synthetic House, 14 Example Marg, Fort", "Mumbai", "Maharashtra", "400001", 18.9338, 72.8356,
     "+91 22 4000 0101", True, True, True, True, True, ["lockers", "forex", "nri_desk", "wealth_desk"]),
    ("branch", "mumbai-andheri-east-branch", "Andheri East Branch, Mumbai", "अंधेरी पूर्व शाखा, मुंबई", "EXMB0000102", "400987102",
     "Unit 3, Sample Business Park, Example Road, Andheri East", "Mumbai", "Maharashtra", "400069", 19.1136, 72.8697,
     "+91 22 4000 0102", False, True, True, False, True, ["lockers", "msme_desk"]),
    ("branch", "mumbai-powai-branch", "Powai Branch, Mumbai", "पवई शाखा, मुंबई", "EXMB0000103", "400987103",
     "Shop 7, Demo Galleria, Example Gardens, Powai", "Mumbai", "Maharashtra", "400076", 19.1176, 72.9060,
     "+91 22 4000 0103", True, False, True, True, True, ["forex", "wealth_desk"]),
    ("branch", "pune-shivajinagar-branch", "Shivajinagar Branch, Pune", "शिवाजीनगर शाखा, पुणे", "EXMB0000201", "411987201",
     "Plot 22, Sample Chambers, Example Road, Shivajinagar", "Pune", "Maharashtra", "411005", 18.5308, 73.8475,
     "+91 20 4000 0201", False, True, False, False, True, ["lockers"]),
    ("branch", "pune-camp-branch", "Camp Branch, Pune", "कैंप शाखा, पुणे", "EXMB0000202", "411987202",
     "8 Demo Street, Camp", "Pune", "Maharashtra", "411001", 18.5158, 73.8796,
     "+91 20 4000 0202", False, False, True, True, True, ["forex", "nri_desk"]),
    ("branch", "delhi-connaught-place-branch", "Connaught Place Branch, New Delhi", "कनॉट प्लेस शाखा, नई दिल्ली", "EXMB0000301",
     "110987301", "Block E, Synthetic Arcade, Inner Circle, Connaught Place", "New Delhi", "Delhi", "110001", 28.6315, 77.2167,
     "+91 11 4000 0301", True, True, True, True, True, ["lockers", "forex", "nri_desk", "wealth_desk"]),
    ("branch", "delhi-karol-bagh-branch", "Karol Bagh Branch, New Delhi", "करोल बाग शाखा, नई दिल्ली", "EXMB0000302", "110987302",
     "16/4 Example Market Road, Karol Bagh", "New Delhi", "Delhi", "110005", 28.6519, 77.1909,
     "+91 11 4000 0302", False, True, False, False, True, ["lockers", "msme_desk"]),
    ("branch", "delhi-lajpat-nagar-branch", "Lajpat Nagar Branch, New Delhi", "लाजपत नगर शाखा, नई दिल्ली", "EXMB0000303", "110987303",
     "C-21 Sample Central Market, Lajpat Nagar II", "New Delhi", "Delhi", "110024", 28.5677, 77.2433,
     "+91 11 4000 0303", False, False, True, False, True, []),
    ("branch", "bengaluru-mg-road-branch", "MG Road Branch, Bengaluru", "एमजी रोड शाखा, बेंगलुरु", "EXMB0000401", "560987401",
     "No. 45, Demo Towers, Example MG Road", "Bengaluru", "Karnataka", "560001", 12.9756, 77.6050,
     "+91 80 4000 0401", True, True, True, True, True, ["lockers", "forex", "nri_desk", "wealth_desk"]),
    ("branch", "bengaluru-koramangala-branch", "Koramangala Branch, Bengaluru", "कोरमंगला शाखा, बेंगलुरु", "EXMB0000402", "560987402",
     "80 Feet Example Road, 4th Block, Koramangala", "Bengaluru", "Karnataka", "560034", 12.9352, 77.6245,
     "+91 80 4000 0402", False, False, True, False, True, ["msme_desk"]),
    ("branch", "chennai-t-nagar-branch", "T. Nagar Branch, Chennai", "टी. नगर शाखा, चेन्नई", "EXMB0000501", "600987501",
     "No. 12, Sample Plaza, Example Street, T. Nagar", "Chennai", "Tamil Nadu", "600017", 13.0418, 80.2341,
     "+91 44 4000 0501", False, True, True, False, True, ["lockers"]),
    ("branch", "kolkata-park-street-branch", "Park Street Branch, Kolkata", "पार्क स्ट्रीट शाखा, कोलकाता", "EXMB0000601", "700987601",
     "Synthetic Court, 9 Example Park Street", "Kolkata", "West Bengal", "700016", 22.5535, 88.3520,
     "+91 33 4000 0601", False, True, False, True, True, ["lockers", "forex"]),
    ("branch", "hyderabad-banjara-hills-branch", "Banjara Hills Branch, Hyderabad", "बंजारा हिल्स शाखा, हैदराबाद", "EXMB0000701",
     "500987701", "Road No. 1, Demo Heights, Banjara Hills", "Hyderabad", "Telangana", "500034", 17.4156, 78.4347,
     "+91 40 4000 0701", True, True, True, True, True, ["lockers", "forex", "nri_desk"]),
    ("branch", "ahmedabad-navrangpura-branch", "Navrangpura Branch, Ahmedabad", "नवरंगपुरा शाखा, अहमदाबाद", "EXMB0000801", "380987801",
     "Sample Complex, Example CG Road, Navrangpura", "Ahmedabad", "Gujarat", "380009", 23.0365, 72.5611,
     "+91 79 4000 0801", False, True, True, False, True, ["lockers", "msme_desk"]),
    ("branch", "jaipur-c-scheme-branch", "C-Scheme Branch, Jaipur", "सी-स्कीम शाखा, जयपुर", "EXMB0000901", "302987901",
     "Synthetic Tower, Example Ashok Marg, C-Scheme", "Jaipur", "Rajasthan", "302001", 26.9124, 75.7873,
     "+91 141 400 0901", False, True, False, False, True, ["lockers"]),
    ("branch", "lucknow-hazratganj-branch", "Hazratganj Branch, Lucknow", "हज़रतगंज शाखा, लखनऊ", "EXMB0001001", "226987001",
     "12 Demo Arcade, Example Hazratganj Road", "Lucknow", "Uttar Pradesh", "226001", 26.8500, 80.9462,
     "+91 522 400 1001", False, False, True, False, True, []),
    # ATMs and cash recyclers (24x7, no IFSC of their own)
    ("atm", "mumbai-cst-atm", "CSMT Station ATM, Mumbai", "सीएसएमटी स्टेशन एटीएम, मुंबई", None, None,
     "Kiosk 2, Example Concourse, near CSMT", "Mumbai", "Maharashtra", "400001", 18.9401, 72.8353,
     None, False, False, True, False, False, ["cash_withdrawal", "balance_enquiry", "pin_change"]),
    ("cash_recycler", "mumbai-andheri-cash-recycler", "Andheri Station Cash Recycler, Mumbai", "अंधेरी स्टेशन कैश रीसाइक्लर, मुंबई",
     None, None, "Shop 1, Sample Arcade, Station Road, Andheri", "Mumbai", "Maharashtra", "400069", 19.1197, 72.8464,
     None, False, False, True, False, True, ["cash_withdrawal", "cash_deposit", "balance_enquiry"]),
    ("atm", "mumbai-powai-atm", "Powai Lake Road ATM, Mumbai", "पवई लेक रोड एटीएम, मुंबई", None, None,
     "Outside Demo Mall, Example Lake Road, Powai", "Mumbai", "Maharashtra", "400076", 19.1238, 72.9045,
     None, False, False, True, False, False, ["cash_withdrawal", "balance_enquiry", "pin_change"]),
    ("atm", "pune-fc-road-atm", "FC Road ATM, Pune", "एफसी रोड एटीएम, पुणे", None, None,
     "Shop 4, Sample Corner, Example FC Road", "Pune", "Maharashtra", "411004", 18.5236, 73.8414,
     None, False, False, False, False, False, ["cash_withdrawal", "balance_enquiry"]),
    ("atm", "delhi-rajiv-chowk-atm", "Rajiv Chowk ATM, New Delhi", "राजीव चौक एटीएम, नई दिल्ली", None, None,
     "Gate 5, Example Metro Concourse, Rajiv Chowk", "New Delhi", "Delhi", "110001", 28.6328, 77.2197,
     None, False, False, True, False, False, ["cash_withdrawal", "balance_enquiry", "pin_change"]),
    ("cash_recycler", "bengaluru-indiranagar-cash-recycler", "Indiranagar Cash Recycler, Bengaluru", "इंदिरानगर कैश रीसाइक्लर, बेंगलुरु",
     None, None, "100 Feet Example Road, Indiranagar", "Bengaluru", "Karnataka", "560038", 12.9719, 77.6412,
     None, False, False, True, False, True, ["cash_withdrawal", "cash_deposit", "balance_enquiry"]),
    ("atm", "chennai-anna-salai-atm", "Anna Salai ATM, Chennai", "अन्ना सलाई एटीएम, चेन्नई", None, None,
     "Sample Building, Example Anna Salai", "Chennai", "Tamil Nadu", "600002", 13.0604, 80.2496,
     None, False, False, True, False, False, ["cash_withdrawal", "balance_enquiry"]),
    ("cash_recycler", "hyderabad-hitech-city-cash-recycler", "HITEC City Cash Recycler, Hyderabad", "हाईटेक सिटी कैश रीसाइक्लर, हैदराबाद",
     None, None, "Tower B Lobby, Demo IT Park, HITEC City", "Hyderabad", "Telangana", "500081", 17.4435, 78.3772,
     None, False, False, True, False, True, ["cash_withdrawal", "cash_deposit", "balance_enquiry"]),
]

# Holidays (synthetic sample — to be maintained in the CMS). "ALL" = national.
NATIONAL_FIXED = [
    ("01-26", "Republic Day", "गणतंत्र दिवस"),
    ("04-14", "Dr. B. R. Ambedkar Jayanti", "डॉ. बी. आर. आंबेडकर जयंती"),
    ("08-15", "Independence Day", "स्वतंत्रता दिवस"),
    ("10-02", "Gandhi Jayanti", "गांधी जयंती"),
    ("12-25", "Christmas", "क्रिसमस"),
]
NATIONAL_MOVABLE = {
    2026: [("03-04", "Holi", "होली"), ("03-21", "Id-ul-Fitr", "ईद-उल-फ़ित्र"), ("04-03", "Good Friday", "गुड फ्राइडे"),
           ("11-09", "Diwali (Balipratipada)", "दीपावली (बलिप्रतिपदा)")],
    2027: [("03-10", "Id-ul-Fitr", "ईद-उल-फ़ित्र"), ("03-22", "Holi", "होली"), ("03-26", "Good Friday", "गुड फ्राइडे"),
           ("10-29", "Diwali (Lakshmi Puja)", "दीपावली (लक्ष्मी पूजा)")],
}
STATE_FIXED = [
    ("Maharashtra", "02-19", "Chhatrapati Shivaji Maharaj Jayanti", "छत्रपति शिवाजी महाराज जयंती"),
    ("Maharashtra", "05-01", "Maharashtra Day", "महाराष्ट्र दिवस"),
    ("Gujarat", "05-01", "Gujarat Day", "गुजरात दिवस"),
    ("Karnataka", "11-01", "Kannada Rajyotsava", "कन्नड़ राज्योत्सव"),
    ("Tamil Nadu", "01-15", "Pongal", "पोंगल"),
    ("West Bengal", "04-15", "Bengali New Year (Poila Boishakh)", "बंगाली नव वर्ष (पोइला बैसाख)"),
    ("Telangana", "06-02", "Telangana Formation Day", "तेलंगाना स्थापना दिवस"),
    ("Rajasthan", "03-30", "Rajasthan Day", "राजस्थान दिवस"),
    ("Uttar Pradesh", "01-24", "Uttar Pradesh Day", "उत्तर प्रदेश दिवस"),
    ("Delhi", "11-24", "Guru Tegh Bahadur Martyrdom Day", "गुरु तेग बहादुर शहीदी दिवस"),
]


def seed_branches(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) FROM branches").fetchone()[0] == 0:
        for (kind, slug, name, name_hi, ifsc, micr, address, city, state, pin, lat, lng, phone, extended,
             locker, wheelchair, forex, cash_dep, extra) in SEED_BRANCHES:
            if kind == "branch":
                timings = EXTENDED_HOURS if extended else BRANCH_HOURS
                services = BASE_SERVICES + [s for s in extra if s not in BASE_SERVICES]
                is_24x7 = False
            else:
                timings, services, is_24x7 = ATM_HOURS, list(extra), True
            conn.execute(
                """INSERT INTO branches (id, kind, name, name_hi, slug, ifsc, micr, address, city, state, pin_code, lat, lng,
                   timings, services, phone, is_24x7, cash_deposit, locker, wheelchair_access, forex)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (new_id(), kind, name, name_hi, slug, ifsc, micr, f"{address}, {city}, {state} {pin}", city, state, pin, lat, lng,
                 jdump(timings), jdump(services), phone, int(is_24x7), int(cash_dep), int(locker), int(wheelchair), int(forex)),
            )
    if conn.execute("SELECT COUNT(*) FROM holidays").fetchone()[0] == 0:
        y0 = today_ist().year
        for year in (y0, y0 + 1):
            rows = [("ALL", md, n, nh) for md, n, nh in NATIONAL_FIXED]
            rows += [("ALL", md, n, nh) for md, n, nh in NATIONAL_MOVABLE.get(year, [])]
            rows += [(st, md, n, nh) for st, md, n, nh in STATE_FIXED]
            for state, md, name, name_hi in rows:
                conn.execute("INSERT INTO holidays (id, state, date, name, name_hi) VALUES (?,?,?,?,?)",
                             (new_id(), state, f"{year}-{md}", name, name_hi))


register_seed(30, "branches", seed_branches)


# ------------------------------------------------------------------ helpers
def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _span_minutes(span: str | None) -> tuple[int, int] | None:
    m = SPAN_RE.match(span or "")
    if not m:
        return None
    start = int(m.group(1)) * 60 + int(m.group(2))
    end = int(m.group(3)) * 60 + int(m.group(4))
    return start, end


def _now_ist() -> datetime:
    return utcnow().astimezone(IST)


def _holidays_on(conn: sqlite3.Connection, d: date) -> dict[str, dict]:
    """state -> holiday for a date (state 'ALL' for national)."""
    out: dict[str, dict] = {}
    for h in many(conn, "SELECT state, name, name_hi FROM holidays WHERE date = ?", (d.isoformat(),)):
        out.setdefault(h["state"].lower(), {"name": h["name"], "name_hi": h["name_hi"]})
    return out


def open_status(b: dict, now: datetime, holidays_today: dict[str, dict]) -> dict:
    """open_now / today_hours / closed_reason computed in IST."""
    d = now.date()
    key = DAYS[d.weekday()]
    span = (b.get("timings") or {}).get(key)
    if b["kind"] == "branch":
        hol = holidays_today.get(str(b["state"]).lower()) or holidays_today.get("all")
        if hol:
            return {"open_now": False, "today_hours": None, "closed_reason": "holiday", "holiday": hol}
        if key == "sat" and ((d.day - 1) // 7 + 1) in (2, 4):
            return {"open_now": False, "today_hours": None, "closed_reason": "second_fourth_saturday", "holiday": None}
    if not span:
        return {"open_now": False, "today_hours": None, "closed_reason": "weekly_off", "holiday": None}
    mins = _span_minutes(span)
    if not mins:
        return {"open_now": False, "today_hours": span, "closed_reason": "unknown_hours", "holiday": None}
    cur = now.hour * 60 + now.minute
    is_open = mins[0] <= cur < mins[1]
    return {"open_now": is_open, "today_hours": span, "closed_reason": None if is_open else "outside_hours", "holiday": None}


def branch_out(row: dict) -> dict:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "name": row["name"],
        "name_hi": row["name_hi"],
        "slug": row["slug"],
        "ifsc": row["ifsc"],
        "micr": row["micr"],
        "address": row["address"],
        "city": row["city"],
        "state": row["state"],
        "pin_code": row["pin_code"],
        "lat": row["lat"],
        "lng": row["lng"],
        "phone": row["phone"],
        "timings": jload(row["timings"], {}),
        "services": jload(row["services"], []),
        "is_24x7": bool(row["is_24x7"]),
        "cash_deposit": bool(row["cash_deposit"]),
        "locker": bool(row["locker"]),
        "wheelchair": bool(row["wheelchair_access"]),
        "forex": bool(row["forex"]),
    }


def _geocode_pin(conn: sqlite3.Connection, pin: str) -> dict | None:
    rows = many(conn, "SELECT lat, lng FROM branches WHERE pin_code = ?", (pin,))
    if rows:
        return {"lat": sum(r["lat"] for r in rows) / len(rows), "lng": sum(r["lng"] for r in rows) / len(rows),
                "source": "pin_branches", "pin_code": pin}
    cen = PIN_PREFIX_CENTROIDS.get(pin[:3])
    if cen:
        return {"lat": cen[0], "lng": cen[1], "source": "pin_centroid", "pin_code": pin, "area": cen[2]}
    for n in (5, 4, 3, 2):
        rows = many(conn, "SELECT lat, lng FROM branches WHERE substr(pin_code, 1, ?) = ?", (n, pin[:n]))
        if rows:
            return {"lat": sum(r["lat"] for r in rows) / len(rows), "lng": sum(r["lng"] for r in rows) / len(rows),
                    "source": "pin_prefix", "pin_code": pin, "prefix": pin[:n]}
    return None


# ------------------------------------------------------------------ endpoints
@router.get("/public/branches")
def search_branches(
    lat: Optional[float] = Query(None),
    lng: Optional[float] = Query(None),
    pin_code: Optional[str] = Query(None, max_length=10),
    kind: str = Query("all"),
    is_24x7: Optional[bool] = Query(None),
    cash_deposit: Optional[bool] = Query(None),
    locker: Optional[bool] = Query(None),
    wheelchair: Optional[bool] = Query(None),
    forex: Optional[bool] = Query(None),
    radius_km: Optional[float] = Query(None),
    limit: int = Query(20),
    conn: sqlite3.Connection = Depends(get_db),
):
    kind = (kind or "all").strip().lower()
    if kind not in KINDS:
        raise invalid("kind", "Choose branch, atm or all")
    if limit < 1 or limit > 100:
        raise invalid("limit", "Must be between 1 and 100")
    if radius_km is not None and (not math.isfinite(radius_km) or radius_km <= 0 or radius_km > 5000):
        raise invalid("radius_km", "Must be greater than 0 and at most 5000 km")

    pin = (pin_code or "").strip()
    if lat is not None or lng is not None:
        if lat is None:
            raise invalid("lat", "Latitude is required with longitude")
        if lng is None:
            raise invalid("lng", "Longitude is required with latitude")
        if not math.isfinite(lat) or not -90 <= lat <= 90:
            raise invalid("lat", "Latitude must be between -90 and 90")
        if not math.isfinite(lng) or not -180 <= lng <= 180:
            raise invalid("lng", "Longitude must be between -180 and 180")
        origin: dict | None = {"lat": lat, "lng": lng, "source": "coordinates"}
    elif pin:
        if not PIN_RE.match(pin):
            raise invalid("pin_code", "Enter a valid 6-digit PIN code")
        origin = _geocode_pin(conn, pin)
    else:
        raise invalid("location", "Share your location or enter a PIN code")

    filters = {"kind": kind, "is_24x7": bool(is_24x7), "cash_deposit": bool(cash_deposit), "locker": bool(locker),
               "wheelchair": bool(wheelchair), "forex": bool(forex), "radius_km": radius_km, "limit": limit}
    base = {"origin": origin, "filters": filters, "data_note": BRANCH_DATA_NOTE, "as_of": _now_ist().isoformat(timespec="minutes")}
    if origin is None:
        return {**base, "count": 0, "results": [],
                "message": "We could not place this PIN code. Try a nearby PIN or use your location."}

    sql, params = "SELECT * FROM branches", []
    if kind == "branch":
        sql += " WHERE kind = 'branch'"
    elif kind == "atm":
        sql += " WHERE kind IN ('atm','cash_recycler')"
    now = _now_ist()
    hols = _holidays_on(conn, now.date())
    results = []
    for row in many(conn, sql, params):
        b = branch_out(row)
        if is_24x7 and not b["is_24x7"]:
            continue
        if cash_deposit and not b["cash_deposit"]:
            continue
        if locker and not b["locker"]:
            continue
        if wheelchair and not b["wheelchair"]:
            continue
        if forex and not b["forex"]:
            continue
        dist = haversine_km(origin["lat"], origin["lng"], b["lat"], b["lng"])
        if radius_km is not None and dist > radius_km:
            continue
        b["distance_km"] = round(dist, 2)
        b.update(open_status(b, now, hols))
        results.append(b)
    results.sort(key=lambda x: (x["distance_km"], x["name"]))
    results = results[:limit]
    return {**base, "count": len(results), "results": results}


def _structured_data(b: dict) -> dict:
    hours = []
    for i, d in enumerate(DAYS):
        mins = _span_minutes((b["timings"] or {}).get(d))
        if not mins:
            continue
        opens = f"{mins[0] // 60:02d}:{mins[0] % 60:02d}"
        closes = "23:59" if mins[1] >= 24 * 60 else f"{mins[1] // 60:02d}:{mins[1] % 60:02d}"
        hours.append({"@type": "OpeningHoursSpecification", "dayOfWeek": f"https://schema.org/{SCHEMA_DAYS[i]}",
                      "opens": opens, "closes": closes})
    data = {
        "@context": "https://schema.org",
        "@type": "BankOrCreditUnion" if b["kind"] == "branch" else "AutomatedTeller",
        "name": f"{BRAND_NAME} — {b['name']}",
        "address": {"@type": "PostalAddress", "streetAddress": b["address"], "addressLocality": b["city"],
                    "addressRegion": b["state"], "postalCode": b["pin_code"], "addressCountry": "IN"},
        "geo": {"@type": "GeoCoordinates", "latitude": b["lat"], "longitude": b["lng"]},
        "openingHoursSpecification": hours,
        "url_path": f"/en/branches/{b['slug']}",
    }
    if b.get("phone"):
        data["telephone"] = b["phone"]
    if b.get("ifsc"):
        data["identifier"] = [{"@type": "PropertyValue", "propertyID": "IFSC", "value": b["ifsc"]}]
        if b.get("micr"):
            data["identifier"].append({"@type": "PropertyValue", "propertyID": "MICR", "value": b["micr"]})
    return data


def _holiday_out(h: dict) -> dict:
    d = date.fromisoformat(h["date"])
    return {"date": h["date"], "name": h["name"], "name_hi": h["name_hi"], "state": h["state"],
            "scope": "national" if h["state"] == "ALL" else "state", "weekday": DAYS[d.weekday()]}


@router.get("/public/branches/{slug}")
def branch_detail(slug: str, conn: sqlite3.Connection = Depends(get_db)):
    s = (slug or "").strip().lower()
    if not SLUG_RE.match(s):
        raise not_found("Branch")
    row = one(conn, "SELECT * FROM branches WHERE slug = ?", (s,))
    if not row:
        raise not_found("Branch")
    b = branch_out(row)
    now = _now_ist()
    b.update(open_status(b, now, _holidays_on(conn, now.date())))
    year = now.year
    hols = many(conn, """SELECT * FROM holidays WHERE (state = 'ALL' OR state = ? COLLATE NOCASE) AND substr(date, 1, 4) = ?
                         ORDER BY date, name""", (b["state"], str(year)))
    hols_today = _holidays_on(conn, now.date())
    nearby = []
    for r in many(conn, "SELECT * FROM branches WHERE kind IN ('atm','cash_recycler') AND id != ?", (b["id"],)):
        a = branch_out(r)
        dist = haversine_km(b["lat"], b["lng"], a["lat"], a["lng"])
        if dist <= NEARBY_ATM_KM:
            a["distance_km"] = round(dist, 2)
            a.update(open_status(a, now, hols_today))
            nearby.append(a)
    nearby.sort(key=lambda x: x["distance_km"])
    weekly = [{"day": d, "hours": (b["timings"] or {}).get(d)} for d in DAYS]
    return {
        **b,
        "bank": BRAND_NAME,
        "weekly_timings": weekly,
        "timing_notes": ["Branches are closed on the second and fourth Saturday of every month and on bank holidays."]
        if b["kind"] == "branch" else ["Available 24x7. Cash availability may vary."],
        "holidays_year": year,
        "holidays": [_holiday_out(h) for h in hols],
        "holiday_data_note": HOLIDAY_DATA_NOTE,
        "nearby_atms": nearby,
        "nearby_radius_km": NEARBY_ATM_KM,
        "directions_url": f"https://www.openstreetmap.org/directions?to={b['lat']},{b['lng']}",
        "structured_data": _structured_data(b),
        "data_note": BRANCH_DATA_NOTE,
        "as_of": now.isoformat(timespec="minutes"),
    }


@router.get("/public/ifsc/{ifsc}")
def ifsc_lookup(ifsc: str, conn: sqlite3.Connection = Depends(get_db)):
    code = (ifsc or "").strip().upper()
    if not IFSC_RE.match(code):
        raise invalid("ifsc", "Enter a valid 11-character IFSC, for example EXMB0000101")
    row = one(conn, "SELECT * FROM branches WHERE ifsc = ?", (code,))
    if not row:
        raise api_error(404, "ifsc_not_found", "No branch found for this IFSC", field="ifsc")
    b = branch_out(row)
    now = _now_ist()
    b.update(open_status(b, now, _holidays_on(conn, now.date())))
    return {"ifsc": code, "micr": b["micr"], "bank": BRAND_NAME, "branch": b, "data_note": BRANCH_DATA_NOTE}


@router.get("/public/holidays")
def list_holidays(state: Optional[str] = Query(None, max_length=60), year: Optional[int] = Query(None),
                  conn: sqlite3.Connection = Depends(get_db)):
    y = year if year is not None else today_ist().year
    if y < 2000 or y > 2100:
        raise invalid("year", "Enter a year between 2000 and 2100")
    st = (state or "").strip()
    if st and not STATE_RE.match(st):
        raise invalid("state", "Enter a valid state name, for example Maharashtra")
    if st and st.upper() != "ALL":
        rows = many(conn, """SELECT * FROM holidays WHERE (state = 'ALL' OR state = ? COLLATE NOCASE) AND substr(date, 1, 4) = ?
                             ORDER BY date, name""", (st, str(y)))
    elif st:
        rows = many(conn, "SELECT * FROM holidays WHERE state = 'ALL' AND substr(date, 1, 4) = ? ORDER BY date, name", (str(y),))
    else:
        rows = many(conn, "SELECT * FROM holidays WHERE substr(date, 1, 4) = ? ORDER BY date, state, name", (str(y),))
    states = sorted({r["state"] for r in many(conn, "SELECT DISTINCT state FROM branches")}
                    | {r["state"] for r in many(conn, "SELECT DISTINCT state FROM holidays WHERE state != 'ALL'")})
    return {"state": st or None, "year": y, "count": len(rows), "holidays": [_holiday_out(h) for h in rows],
            "states": states, "data_note": HOLIDAY_DATA_NOTE}

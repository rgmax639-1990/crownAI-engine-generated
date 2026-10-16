"""Public calculators: EMI, FD, RD, SIP, income tax (old vs new regime), LRS/TCS, and shareable links.

Every result carries an echo of the inputs, the results (paise integers plus lakh/crore
strings), a schedule where relevant, the assumptions made and the rule sources with
their "as on" dates. Regulatory numbers (TDS, TCS) are read from CMS-held regulatory
values; deposit rates come from the live published rate tables.
"""
from __future__ import annotations

import json
import math
import re
import secrets
import sqlite3
from datetime import timedelta
from typing import Any, Callable, Optional

from fastapi import APIRouter, Body, Depends, Query

import rates_service
from core import (PAN_RE, api_error, financial_year, get_reg_value, get_reg_value_meta, invalid, iso, jdump, jload, not_found,
                  one, parse_iso, today_ist, utcnow, valid_email)
from db import get_db

router = APIRouter(tags=["calculators"])

CALCULATORS = ("emi", "fd", "rd", "sip", "income_tax", "lrs_tcs")
SHARE_TTL_DAYS = 30
SHARE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"  # RFC 4648 base32
SHARE_ID_RE = re.compile(r"^[A-Z2-7]{8}$")
MAX_SHARE_JSON = 4000
MAX_PAISE = 100 * 10_000_000 * 100  # Rs. 100 crore
SIMPLE_INTEREST_MAX_DAYS = 179  # deposits shorter than ~6 months earn simple interest
RD_SENIOR_PREMIUM = 0.50  # bank policy assumption when the RD rate is taken from the published table
DISCLAIMER = ("Illustrative estimate only, not an offer or tax advice. Actual amounts depend on the terms on the date of "
              "booking or filing.")

# Keys that would carry personal data. A shared link must never contain these.
PII_TOKENS = {"name", "fullname", "firstname", "lastname", "mobile", "phone", "email", "mail", "pan", "aadhaar", "aadhar",
              "uid", "address", "dob", "birth", "account", "acct", "ifsc", "card", "otp", "password", "passport", "customer"}

# ------------------------------------------------------------------ income-tax rules (config)
# Amounts in rupees. RULES AS ON the date below. These values are a configured sample and
# MUST be verified by the bank's compliance team before publication.
INCOME_TAX_RULES: dict[str, Any] = {
    "rules_as_on": "2026-04-01",
    "verification_note": "Slab tables, rebate, surcharge and cess are a configured sample and must be verified by the "
                         "bank's compliance team against the Finance Act / Income-tax Act before publication.",
    "source": "Finance Act 2025 (s.115BAC new regime, s.87A rebate); Income-tax Act provisions for FY 2026-27",
    "cess_percent": 4,
    "financial_years": {
        fy: {
            "new": {
                "slabs": [(0, 400_000, 0), (400_000, 800_000, 5), (800_000, 1_200_000, 10), (1_200_000, 1_600_000, 15),
                          (1_600_000, 2_000_000, 20), (2_000_000, 2_400_000, 25), (2_400_000, None, 30)],
                "standard_deduction": 75_000,
                "rebate_limit": 1_200_000,
                "rebate_max": 60_000,
                "marginal_relief": True,
                "surcharge": [(5_000_000, 10), (10_000_000, 15), (20_000_000, 25)],
                "deductions_allowed": False,
            },
            "old": {
                "slabs_by_age": {
                    "below_60": [(0, 250_000, 0), (250_000, 500_000, 5), (500_000, 1_000_000, 20), (1_000_000, None, 30)],
                    "60_80": [(0, 300_000, 0), (300_000, 500_000, 5), (500_000, 1_000_000, 20), (1_000_000, None, 30)],
                    "above_80": [(0, 500_000, 0), (500_000, 1_000_000, 20), (1_000_000, None, 30)],
                },
                "standard_deduction": 50_000,
                "rebate_limit": 500_000,
                "rebate_max": 12_500,
                "marginal_relief": False,
                "surcharge": [(5_000_000, 10), (10_000_000, 15), (20_000_000, 25), (50_000_000, 37)],
                "deductions_allowed": True,
            },
        }
        for fy in ("2025-26", "2026-27")
    },
}
AGE_BANDS = ("below_60", "60_80", "above_80")
LRS_PURPOSES = ("education_loan", "education", "medical", "other")
LRS_RATE_KEYS = {
    "education_loan": ("tcs_rate_percent_education_loan", 0),
    "education": ("tcs_rate_percent_education_medical", 5),
    "medical": ("tcs_rate_percent_education_medical", 5),
    "other": ("tcs_rate_percent_general", 20),
}


# ------------------------------------------------------------------ formatting
def _r(x: float) -> int:
    """Round half away from zero to an integer (paise)."""
    return int(math.floor(abs(x) + 0.5)) * (1 if x >= 0 else -1)


def _group_indian(rupees: int) -> str:
    s = str(rupees)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups) + "," + tail


def inr(paise: int) -> str:
    sign = "-" if paise < 0 else ""
    return f"{sign}₹{_group_indian(_r(abs(paise) / 100))}"


def lakh_crore(paise: int) -> str:
    """₹12.5 lakh, ₹1.2 crore; below one lakh the full rupee amount (₹45,000)."""
    rupees = abs(paise) / 100
    sign = "-" if paise < 0 else ""

    def trim(x: float) -> str:
        return f"{x:.2f}".rstrip("0").rstrip(".")

    if rupees >= 1e7:
        return f"{sign}₹{trim(rupees / 1e7)} crore"
    if rupees >= 1e5:
        return f"{sign}₹{trim(rupees / 1e5)} lakh"
    return inr(paise)


def _money(res: dict, key: str, paise: int) -> None:
    res[f"{key}_paise"] = int(paise)
    res.setdefault("formatted", {})[key] = lakh_crore(int(paise))


# ------------------------------------------------------------------ input parsing
def _bound_label(v: float, money: bool) -> str:
    return inr(int(v)) if money else f"{v:g}"


def _int(raw: dict, field: str, *, lo: int, hi: int, required: bool = True, default: int | None = None,
         money: bool = False) -> int | None:
    v = raw.get(field)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required and default is None:
            raise invalid(field, "This field is required")
        return default
    msg = "Enter an amount in paise (a whole number)" if money else "Enter a whole number"
    if isinstance(v, bool):
        raise invalid(field, msg)
    if isinstance(v, float):
        if not math.isfinite(v) or not v.is_integer():
            raise invalid(field, msg)
        v = int(v)
    elif isinstance(v, str):
        s = v.strip().replace(",", "")
        if not re.fullmatch(r"-?\d{1,15}", s):
            raise invalid(field, msg)
        v = int(s)
    elif not isinstance(v, int):
        raise invalid(field, msg)
    if v < lo or v > hi:
        raise invalid(field, f"Must be between {_bound_label(lo, money)} and {_bound_label(hi, money)}")
    return v


def _float(raw: dict, field: str, *, lo: float, hi: float, required: bool = True, default: float | None = None) -> float | None:
    v = raw.get(field)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required and default is None:
            raise invalid(field, "This field is required")
        return default
    if isinstance(v, bool):
        raise invalid(field, "Enter a number, for example 7.25")
    try:
        f = float(str(v).strip().rstrip("%"))
    except (TypeError, ValueError):
        raise invalid(field, "Enter a number, for example 7.25")
    if not math.isfinite(f):
        raise invalid(field, "Enter a number, for example 7.25")
    if f < lo or f > hi:
        raise invalid(field, f"Must be between {lo:g} and {hi:g}")
    return round(f, 4)


def _bool(raw: dict, field: str, default: bool) -> bool:
    v = raw.get(field)
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    if isinstance(v, str) and v.strip().lower() in ("true", "false", "1", "0", "yes", "no"):
        return v.strip().lower() in ("true", "1", "yes")
    raise invalid(field, "Must be true or false")


def _choice(raw: dict, field: str, choices: tuple[str, ...], default: str | None = None) -> str:
    v = raw.get(field)
    if v is None or (isinstance(v, str) and not v.strip()):
        if default is None:
            raise invalid(field, "This field is required")
        return default
    s = str(v).strip().lower()
    if s not in choices:
        raise invalid(field, f"Choose one of: {', '.join(choices)}")
    return s


# ------------------------------------------------------------------ rule sources
def _reg(conn: sqlite3.Connection, key: str, default: Any) -> tuple[Any, dict]:
    meta = get_reg_value_meta(conn, key)
    if not meta or meta.get("value") is None:
        return default, {"rule": f"{key} (default; CMS value not published)", "as_on": today_ist().isoformat()}
    label = meta.get("label") or key
    src = meta.get("source")
    rule = f"{label}: {meta['value']} {meta.get('unit') or ''}".strip() + (f" ({src})" if src else "")
    if meta.get("needs_compliance_verification"):
        rule += " — pending compliance verification"
    return meta["value"], {"rule": rule, "as_on": (meta.get("published_at") or "")[:10] or None}


def _tds(conn: sqlite3.Connection, interest: int, years: float, senior: bool, apply_tds: bool) -> tuple[dict, list[dict], list[str]]:
    rate, src_rate = _reg(conn, "tds_rate_percent", 10)
    thr_key = "tds_threshold_senior_paise" if senior else "tds_threshold_general_paise"
    threshold, src_thr = _reg(conn, thr_key, 10_000_000 if senior else 5_000_000)
    rate, threshold = float(rate), int(threshold)
    per_fy = interest / max(1.0, years)
    applicable = apply_tds and per_fy > threshold
    tds = _r(interest * rate / 100) if applicable else 0
    res: dict = {"tds_rate_percent": rate, "tds_applicable": applicable}
    _money(res, "tds_threshold", threshold)
    _money(res, "tds", tds)
    notes = [
        f"TDS is estimated at {rate:g}% (PAN furnished) on the whole interest when the interest per financial year is more than "
        f"{lakh_crore(threshold)}{' (senior citizen threshold)' if senior else ''}.",
        "Interest from other deposits with the bank is not counted towards the TDS threshold in this estimate.",
        "No TDS is deducted if a valid Form 15G/15H is submitted; TDS can be claimed against your final tax liability.",
    ]
    if not apply_tds:
        notes = ["TDS is not applied in this estimate (you turned TDS off)."]
    return res, [src_rate, src_thr], notes


# ------------------------------------------------------------------ calculators
def calc_emi(conn: sqlite3.Connection, raw: dict) -> dict:
    p = _int(raw, "principal_paise", lo=100_000, hi=MAX_PAISE, money=True)
    rate = _float(raw, "annual_rate_percent", lo=0, hi=50)
    n = _int(raw, "tenure_months", lo=1, hi=480)
    r = rate / 1200
    if r == 0:
        emi = _r(p / n)
    else:
        f = (1 + r) ** n
        emi = _r(p * r * f / (f - 1))
    bal, total_int = p, 0
    schedule, yearly = [], []
    y_principal = y_interest = 0
    for m in range(1, n + 1):
        interest = _r(bal * r)
        principal = emi - interest
        if m == n or principal >= bal:
            principal = bal
        pay = principal + interest
        bal -= principal
        total_int += interest
        y_principal += principal
        y_interest += interest
        schedule.append({"month": m, "emi_paise": pay, "principal_paise": principal, "interest_paise": interest,
                         "balance_paise": bal})
        if m % 12 == 0 or bal == 0 or m == n:
            yearly.append({"year": (m - 1) // 12 + 1, "principal_paise": y_principal, "interest_paise": y_interest,
                           "balance_paise": bal})
            y_principal = y_interest = 0
        if bal == 0:
            break
    res: dict = {"annual_rate_percent": rate, "tenure_months": n,
                 "interest_share_percent": round(total_int / (p + total_int) * 100, 2) if p + total_int else 0}
    _money(res, "principal", p)
    _money(res, "emi", emi)
    _money(res, "total_interest", total_int)
    _money(res, "total_payment", p + total_int)
    return {
        "inputs": {"principal_paise": p, "annual_rate_percent": rate, "tenure_months": n},
        "results": res,
        "schedule": schedule,
        "yearly_schedule": yearly,
        "assumptions": [
            "EMI on a reducing balance with monthly rests: EMI = P × r × (1+r)^n / ((1+r)^n − 1).",
            "The interest rate stays the same for the whole tenure. Floating rates linked to an external benchmark can change.",
            "Processing fee, insurance, GST and other charges are not included.",
            "The last instalment is adjusted for rounding.",
        ],
        "rule_sources": [{"rule": "Reducing-balance EMI method (RBI Key Fact Statement guidelines for loan pricing disclosure)",
                          "as_on": today_ist().isoformat()}],
    }


def calc_fd(conn: sqlite3.Connection, raw: dict) -> dict:
    p = _int(raw, "principal_paise", lo=100_000, hi=MAX_PAISE, money=True)
    days = _int(raw, "tenure_days", lo=7, hi=3650)
    senior = _bool(raw, "senior_citizen", False)
    apply_tds = _bool(raw, "apply_tds", True)
    rate_in = _float(raw, "annual_rate_percent", lo=0, hi=20, required=False)
    sources: list[dict] = []
    if rate_in is None:
        row = rates_service.fd_rate(conn, days, senior, p)
        if not row:
            raise invalid("tenure_days", "No published deposit rate covers this tenure and amount. Enter a rate to estimate.")
        rate = float(row["rate_percent"])
        rate_source, rate_as_on = "published_rate", row.get("as_on_date")
        sources.append({"rule": f"Published {'senior citizen' if senior else 'general'} FD rate card", "as_on": rate_as_on})
    else:
        rate, rate_source, rate_as_on = rate_in, "user_input", None
    r = rate / 100
    schedule: list[dict] = []
    if days <= SIMPLE_INTEREST_MAX_DAYS:
        interest = _r(p * r * days / 365)
        compounding = "simple"
    else:
        quarters = days / 365 * 4
        growth = 1 + r / 4
        interest = _r(p * growth ** quarters) - p
        compounding = "quarterly"
        prev = p
        for q in range(1, math.ceil(quarters) + 1):
            close = _r(p * growth ** min(q, quarters))
            schedule.append({"period": q, "label": f"Quarter {q}", "opening_paise": prev, "interest_paise": close - prev,
                             "closing_paise": close})
            prev = close
        if schedule:  # make the schedule end exactly on the maturity value
            last = schedule[-1]
            last["closing_paise"] = p + interest
            last["interest_paise"] = last["closing_paise"] - last["opening_paise"]
    maturity = p + interest
    tds_res, tds_sources, tds_notes = _tds(conn, interest, days / 365, senior, apply_tds)
    tds = tds_res["tds_paise"]
    res: dict = {"rate_percent": rate, "rate_source": rate_source, "rate_as_on": rate_as_on, "compounding": compounding,
                 "tenure_days": days, "senior_citizen": senior,
                 "maturity_date": (today_ist() + timedelta(days=days)).isoformat(),
                 "effective_annual_yield_percent": round(((maturity / p) ** (365 / days) - 1) * 100, 2) if p else 0}
    _money(res, "principal", p)
    _money(res, "interest", interest)
    _money(res, "maturity", maturity)
    _money(res, "net_interest", interest - tds)
    _money(res, "net_maturity", maturity - tds)
    for k, v in tds_res.items():
        if k == "formatted":
            res["formatted"].update(v)
        else:
            res[k] = v
    return {
        "inputs": {"principal_paise": p, "tenure_days": days, "senior_citizen": senior, "apply_tds": apply_tds,
                   "annual_rate_percent": rate_in},
        "results": res,
        "schedule": schedule,
        "assumptions": [
            "Cumulative deposit: interest is reinvested and paid at maturity." if compounding == "quarterly"
            else "Deposits shorter than six months earn simple interest paid at maturity.",
            "Interest compounds every quarter (A = P × (1 + r/4)^(4 × days/365))." if compounding == "quarterly"
            else "Simple interest = P × r × days / 365.",
            "The rate shown is the rate on today's published rate card; the rate on the booking date applies."
            if rate_source == "published_rate" else "Uses the rate you entered.",
            *tds_notes,
        ],
        "rule_sources": sources + tds_sources,
    }


def calc_rd(conn: sqlite3.Connection, raw: dict) -> dict:
    m_amt = _int(raw, "monthly_investment_paise", lo=10_000, hi=100_000_000, money=True)
    n = _int(raw, "tenure_months", lo=6, hi=120)
    senior = _bool(raw, "senior_citizen", False)
    apply_tds = _bool(raw, "apply_tds", True)
    rate_in = _float(raw, "annual_rate_percent", lo=0, hi=20, required=False)
    sources: list[dict] = []
    extra_notes: list[str] = []
    if rate_in is None:
        row = rates_service.find_rate(conn, "rd", tenure_days=round(n * 365 / 12))
        if not row:
            raise invalid("annual_rate_percent", "No published RD rate covers this tenure. Enter a rate to estimate.")
        rate = float(row["rate_percent"]) + (RD_SENIOR_PREMIUM if senior else 0)
        rate_source, rate_as_on = "published_rate", row.get("as_on_date")
        sources.append({"rule": "Published recurring deposit rate card", "as_on": rate_as_on})
        if senior:
            extra_notes.append(f"Senior citizen premium of {RD_SENIOR_PREMIUM:.2f}% p.a. added (bank policy; check the rates page).")
    else:
        rate, rate_source, rate_as_on = rate_in, "user_input", None
    g = (1 + rate / 400) ** (1 / 3)  # monthly growth equivalent to quarterly compounding
    value = 0.0
    schedule = []
    for k in range(1, n + 1):
        value = (value + m_amt) * g
        schedule.append({"month": k, "deposit_paise": m_amt, "total_deposited_paise": m_amt * k, "value_paise": _r(value)})
    maturity = _r(value)
    deposited = m_amt * n
    interest = maturity - deposited
    tds_res, tds_sources, tds_notes = _tds(conn, interest, n / 12, senior, apply_tds)
    tds = tds_res["tds_paise"]
    res: dict = {"rate_percent": round(rate, 4), "rate_source": rate_source, "rate_as_on": rate_as_on, "compounding": "quarterly",
                 "tenure_months": n, "senior_citizen": senior}
    _money(res, "monthly_investment", m_amt)
    _money(res, "total_deposit", deposited)
    _money(res, "interest", interest)
    _money(res, "maturity", maturity)
    _money(res, "net_maturity", maturity - tds)
    for k, v in tds_res.items():
        if k == "formatted":
            res["formatted"].update(v)
        else:
            res[k] = v
    return {
        "inputs": {"monthly_investment_paise": m_amt, "tenure_months": n, "senior_citizen": senior, "apply_tds": apply_tds,
                   "annual_rate_percent": rate_in},
        "results": res,
        "schedule": schedule,
        "assumptions": [
            "Instalments are paid at the start of every month without a miss.",
            "Interest compounds every quarter; each instalment earns interest for the months it stays deposited.",
            *extra_notes,
            *tds_notes,
        ],
        "rule_sources": sources + tds_sources,
    }


def calc_sip(conn: sqlite3.Connection, raw: dict) -> dict:
    p = _int(raw, "monthly_investment_paise", lo=10_000, hi=1_000_000_000, money=True)
    rate = _float(raw, "expected_return_percent", lo=0, hi=50)
    n = _int(raw, "tenure_months", lo=1, hi=600)
    i = rate / 1200
    value = 0.0
    yearly = []
    for k in range(1, n + 1):
        value = (value + p) * (1 + i)
        if k % 12 == 0 or k == n:
            v = _r(value)
            yearly.append({"year": (k - 1) // 12 + 1, "months": k, "invested_paise": p * k, "value_paise": v,
                           "returns_paise": v - p * k})
    invested = p * n
    final = _r(value)
    res: dict = {"expected_return_percent": rate, "tenure_months": n}
    _money(res, "monthly_investment", p)
    _money(res, "invested", invested)
    _money(res, "estimated_returns", final - invested)
    _money(res, "value", final)
    return {
        "inputs": {"monthly_investment_paise": p, "expected_return_percent": rate, "tenure_months": n},
        "results": res,
        "schedule": yearly,
        "assumptions": [
            "Investments are made at the start of every month and the return is constant, compounded monthly.",
            "Actual returns vary. Mutual fund investments are subject to market risks; read all scheme-related documents carefully.",
            "Exit load, expense ratio, stamp duty and capital-gains tax are not considered.",
        ],
        "rule_sources": [{"rule": "Future value of a monthly annuity due: FV = P × ((1+i)^n − 1) / i × (1+i)",
                          "as_on": today_ist().isoformat()}],
    }


def _regime_tax(income: int, deductions: int, salaried: bool, cfg: dict, slabs: list, cess_pct: float, regime: str) -> dict:
    std = min(cfg["standard_deduction"] * 100, income) if salaried else 0
    ded = deductions if cfg["deductions_allowed"] else 0
    taxable = max(0, income - std - ded)
    taxable = _r(taxable / 1000) * 1000  # rounded to the nearest Rs. 10
    breakup, tax = [], 0
    for lo, hi, rate in slabs:
        lo_p, hi_p = lo * 100, (hi * 100 if hi is not None else None)
        portion = 0 if taxable <= lo_p else (min(taxable, hi_p) if hi_p is not None else taxable) - lo_p
        t = _r(portion * rate / 100)
        tax += t
        breakup.append({"from_paise": lo_p, "to_paise": hi_p, "rate_percent": rate, "taxable_paise": portion, "tax_paise": t})
    rebate = marginal = 0
    limit = cfg["rebate_limit"] * 100
    if taxable <= limit:
        rebate = min(tax, cfg["rebate_max"] * 100)
    elif cfg["marginal_relief"] and tax > taxable - limit:
        marginal = tax - (taxable - limit)
    after = tax - rebate - marginal
    sur_rate = 0
    for thr, rate in cfg["surcharge"]:
        if taxable > thr * 100:
            sur_rate = rate
    surcharge = _r(after * sur_rate / 100)
    cess = _r((after + surcharge) * cess_pct / 100)
    total = _r((after + surcharge + cess) / 1000) * 1000  # rounded to the nearest Rs. 10
    out: dict = {"regime": regime, "surcharge_rate_percent": sur_rate,
                 "effective_rate_percent": round(total / income * 100, 2) if income else 0.0, "slabs": breakup}
    for key, val in (("gross_income", income), ("standard_deduction", std), ("deductions", ded), ("taxable_income", taxable),
                     ("tax_on_slabs", tax), ("rebate_87a", rebate), ("marginal_relief", marginal), ("surcharge", surcharge),
                     ("cess", cess), ("total_tax", total)):
        _money(out, key, val)
    return out


def calc_income_tax(conn: sqlite3.Connection, raw: dict) -> dict:
    income = _int(raw, "annual_income_paise", lo=0, hi=MAX_PAISE, money=True)
    deductions = _int(raw, "deductions_paise", lo=0, hi=MAX_PAISE, required=False, default=0, money=True)
    age = _choice(raw, "age_band", AGE_BANDS, "below_60")
    years = INCOME_TAX_RULES["financial_years"]
    default_fy = financial_year()
    fy = str(raw.get("financial_year") or (default_fy if default_fy in years else max(years))).strip()
    if fy not in years:
        raise invalid("financial_year", f"Choose one of: {', '.join(sorted(years))}")
    salaried = _bool(raw, "salaried", True)
    cfg = years[fy]
    cess = INCOME_TAX_RULES["cess_percent"]
    old = _regime_tax(income, deductions, salaried, cfg["old"], cfg["old"]["slabs_by_age"][age], cess, "old")
    new = _regime_tax(income, deductions, salaried, cfg["new"], cfg["new"]["slabs"], cess, "new")
    diff = old["total_tax_paise"] - new["total_tax_paise"]
    if diff > 0:
        rec, msg = "new", f"The new regime saves you about {lakh_crore(diff)} in tax."
    elif diff < 0:
        rec, msg = "old", f"The old regime saves you about {lakh_crore(-diff)} in tax with the deductions you entered."
    else:
        rec, msg = "either", "Both regimes give the same tax for these inputs."
    res: dict = {"financial_year": fy, "age_band": age, "old_regime": old, "new_regime": new, "recommended_regime": rec,
                 "recommendation": msg}
    _money(res, "old_tax", old["total_tax_paise"])
    _money(res, "new_tax", new["total_tax_paise"])
    _money(res, "savings", abs(diff))
    return {
        "inputs": {"annual_income_paise": income, "deductions_paise": deductions, "age_band": age, "financial_year": fy,
                   "salaried": salaried},
        "results": res,
        "schedule": [],
        "assumptions": [
            "Income is treated as salary/other income taxed at slab rates for a resident individual. Special-rate income "
            "(capital gains, lottery) is not included.",
            "Standard deduction applies only to salaried individuals/pensioners." if salaried
            else "No standard deduction (you are not salaried).",
            "Deductions you enter (80C, 80D, home-loan interest and so on) are used only in the old regime.",
            "Rebate u/s 87A is applied (with marginal relief in the new regime); marginal relief on surcharge is not computed.",
            f"Health and education cess at {cess}% is added. Tax is rounded to the nearest ₹10.",
            INCOME_TAX_RULES["verification_note"],
        ],
        "rule_sources": [{"rule": f"Income-tax slabs for FY {fy} — {INCOME_TAX_RULES['source']}",
                          "as_on": INCOME_TAX_RULES["rules_as_on"]}],
    }


def calc_lrs_tcs(conn: sqlite3.Connection, raw: dict) -> dict:
    amt = _int(raw, "remittance_paise", lo=100, hi=MAX_PAISE, money=True)
    prior = _int(raw, "already_remitted_paise", lo=0, hi=MAX_PAISE, required=False, default=0, money=True)
    purpose = _choice(raw, "lrs_purpose", LRS_PURPOSES)
    threshold, src_thr = _reg(conn, "tcs_threshold_paise", 100_000_000)
    rate_key, rate_default = LRS_RATE_KEYS[purpose]
    rate, src_rate = _reg(conn, rate_key, rate_default)
    limit_usd, src_limit = _reg(conn, "lrs_annual_limit_usd", 250_000)
    threshold, rate = int(threshold), float(rate)
    above = max(0, prior + amt - threshold) - max(0, prior - threshold)
    tcs = _r(above * rate / 100)
    res: dict = {"lrs_purpose": purpose, "tcs_rate_percent": rate, "lrs_annual_limit_usd": limit_usd,
                 "effective_tcs_percent": round(tcs / amt * 100, 2) if amt else 0.0}
    _money(res, "remittance", amt)
    _money(res, "already_remitted", prior)
    _money(res, "threshold", threshold)
    _money(res, "amount_above_threshold", above)
    _money(res, "tcs", tcs)
    _money(res, "total_outflow", amt + tcs)
    return {
        "inputs": {"remittance_paise": amt, "already_remitted_paise": prior, "lrs_purpose": purpose},
        "results": res,
        "schedule": [],
        "assumptions": [
            f"TCS applies only to the part of your remittances in this financial year above {lakh_crore(threshold)} "
            "(across all banks — include remittances made through other banks).",
            "The education-loan rate applies only when the remittance is funded by a loan from a financial institution.",
            "TCS is not an extra cost: it is credited against your PAN and can be claimed in your income-tax return.",
            f"Remittances must stay within the LRS limit of USD {limit_usd:,} per financial year and permitted purposes under FEMA.",
            "Bank charges, GST on currency conversion and the exchange-rate margin are not included.",
        ],
        "rule_sources": [src_thr, src_rate, src_limit],
    }


CALC_FUNCS: dict[str, Callable[[sqlite3.Connection, dict], dict]] = {
    "emi": calc_emi, "fd": calc_fd, "rd": calc_rd, "sip": calc_sip, "income_tax": calc_income_tax, "lrs_tcs": calc_lrs_tcs,
}


def run_calculator(conn: sqlite3.Connection, calculator: str, raw: dict) -> dict:
    out = CALC_FUNCS[calculator](conn, raw)
    return {"calculator": calculator, **out, "disclaimer": DISCLAIMER, "calculated_at": iso(utcnow())}


# ------------------------------------------------------------------ shares (registered BEFORE /{calculator})
def _key_tokens(key: str) -> set[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
    tokens = {t for t in re.split(r"[^a-z0-9]+", spaced.lower()) if t}
    tokens.add(re.sub(r"[^a-z0-9]", "", str(key).lower()))
    return tokens


def _pii_problem(value: Any, path: str = "") -> str | None:
    if isinstance(value, dict):
        for k, v in value.items():
            here = f"{path}.{k}" if path else str(k)
            if _key_tokens(k) & PII_TOKENS:
                return f"'{here}' looks like personal data and cannot be shared"
            p = _pii_problem(v, here)
            if p:
                return p
    elif isinstance(value, list):
        for i, v in enumerate(value):
            p = _pii_problem(v, f"{path}[{i}]")
            if p:
                return p
    elif isinstance(value, str):
        s = value.strip()
        digits = re.sub(r"[\s-]", "", s)
        if (valid_email(s) or PAN_RE.match(s.upper()) or re.fullmatch(r"(\+?91)?[6-9]\d{9}", digits)
                or re.fullmatch(r"[2-9]\d{11}", digits)):
            return f"'{path}' looks like personal data and cannot be shared"
    return None


def _share_url(share_id: str, locale: str) -> str:
    return f"/{locale}/tools?share={share_id}"


@router.post("/public/calculators/shares", status_code=201)
def create_share(payload: dict = Body(...), conn: sqlite3.Connection = Depends(get_db)):
    calculator = str(payload.get("calculator") or "").strip().lower()
    if calculator not in CALCULATORS:
        raise invalid("calculator", f"Choose one of: {', '.join(CALCULATORS)}")
    locale = str(payload.get("locale") or "en").strip().lower()
    if locale not in ("en", "hi"):
        raise invalid("locale", "Choose en or hi")
    inputs = payload.get("inputs_json", payload.get("inputs"))
    if isinstance(inputs, str):
        try:
            inputs = json.loads(inputs)
        except ValueError:
            raise invalid("inputs_json", "inputs_json must be a JSON object")
    if not isinstance(inputs, dict) or not inputs:
        raise invalid("inputs_json", "inputs_json must be a non-empty JSON object")
    if len(jdump(inputs)) > MAX_SHARE_JSON:
        raise invalid("inputs_json", "inputs_json is too large")
    problem = _pii_problem(inputs)
    if problem:
        raise api_error(422, "pii_not_allowed", f"Shared calculations must not contain personal details: {problem}",
                        field="inputs_json")
    # Validate by calculating; only the calculator's normalised inputs are stored.
    normalised = run_calculator(conn, calculator, inputs)["inputs"]
    share_id = None
    for _ in range(10):
        candidate = "".join(secrets.choice(SHARE_ALPHABET) for _ in range(8))
        if not one(conn, "SELECT id FROM calculator_shares WHERE id = ?", (candidate,)):
            share_id = candidate
            break
    if share_id is None:
        raise api_error(503, "share_unavailable", "Could not create a share link right now. Please try again.")
    now = utcnow()
    expires = now + timedelta(days=SHARE_TTL_DAYS)
    conn.execute("INSERT INTO calculator_shares (id, calculator, inputs, created_at, expires_at) VALUES (?,?,?,?,?)",
                 (share_id, calculator, jdump(normalised), iso(now), iso(expires)))
    return {"share_id": share_id, "url": _share_url(share_id, locale), "calculator": calculator, "inputs": normalised,
            "created_at": iso(now), "expires_at": iso(expires)}


@router.get("/public/calculators/shares/{share_id}")
def get_share(share_id: str, locale: Optional[str] = Query("en"), conn: sqlite3.Connection = Depends(get_db)):
    sid = (share_id or "").strip().upper()
    if not SHARE_ID_RE.match(sid):
        raise not_found("Shared calculation")
    row = one(conn, "SELECT * FROM calculator_shares WHERE id = ?", (sid,))
    if not row:
        raise not_found("Shared calculation")
    exp = parse_iso(row["expires_at"])
    if exp is None or exp <= utcnow():
        raise api_error(410, "share_expired", f"This shared calculation has expired. Shared links are kept for {SHARE_TTL_DAYS} days.")
    loc = locale if locale in ("en", "hi") else "en"
    return {"share_id": sid, "url": _share_url(sid, loc), "calculator": row["calculator"], "inputs": jload(row["inputs"], {}),
            "created_at": row["created_at"], "expires_at": row["expires_at"]}


# ------------------------------------------------------------------ calculate
@router.post("/public/calculators/{calculator}")
def calculate(calculator: str, payload: Optional[dict] = Body(None), conn: sqlite3.Connection = Depends(get_db)):
    calc = (calculator or "").strip().lower()
    if calc not in CALC_FUNCS:
        raise not_found("Calculator")
    raw = payload or {}
    if isinstance(raw.get("inputs"), dict):
        raw = raw["inputs"]
    return run_calculator(conn, calc, raw)

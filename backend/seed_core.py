"""Synthetic seed data shared by every module: staff, demo customers, corporate, adapters,
CMS-held regulatory values. All names, numbers and contacts are synthetic.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import timedelta

from banking import link_account, upsert_customer_from_finacle
from cms_seed import seed_published
from core import jdump, new_id, now_iso, today_ist
from db import register_seed
from integrations import finacle
from integrations.base import ENVIRONMENTS, PROVIDERS
from security import hash_password

SEED_PASSWORD = os.getenv("SEED_PASSWORD", "Demo@12345")

STAFF = [
    ("maker@example.bank.in", "Priya Nair", ["content_maker"]),
    ("checker@example.bank.in", "Arjun Rao", ["content_checker"]),
    ("legal@example.bank.in", "Meera Iyer", ["legal_approver", "compliance_reviewer"]),
    ("compliance@example.bank.in", "Farah Khan", ["compliance_reviewer"]),
    ("grievance@example.bank.in", "Kavita Desai", ["grievance_officer"]),
    ("nodal@example.bank.in", "Rahul Verma", ["nodal_officer", "grievance_officer"]),
    ("pno@example.bank.in", "Anita Bose", ["principal_nodal_officer", "grievance_officer"]),
    ("io@example.bank.in", "Justice (Retd.) S. Gupta", ["internal_ombudsman"]),
    ("rm@example.bank.in", "Vikram Malhotra", ["relationship_manager"]),
    ("vcip@example.bank.in", "Sneha Kulkarni", ["vcip_officer"]),
    ("ops@example.bank.in", "Imran Shaikh", ["ops_admin"]),
    ("soc@example.bank.in", "Deepa Menon", ["soc_analyst"]),
    ("approver@example.bank.in", "Suresh Pillai", ["integration_approver"]),
]

REGULATORY_VALUES = [
    ("dicgc_limit_paise", 50000000, "paise", "DICGC deposit insurance cover per depositor per bank", "DICGC Act / DICGC website"),
    ("beneficiary_cooling_limit_paise", 5000000, "paise", "Limit for a new beneficiary during the cooling period", "Bank policy (assumption)"),
    ("beneficiary_cooling_hours", 24, "hours", "Beneficiary cooling period", "Bank policy (assumption)"),
    ("positive_pay_threshold_paise", 5000000, "paise", "Positive Pay threshold for cheques", "RBI Positive Pay circular / NPCI CTS"),
    ("lrs_annual_limit_usd", 250000, "USD", "LRS limit per resident individual per financial year", "RBI LRS Master Direction"),
    ("tcs_threshold_paise", 100000000, "paise", "TCS-free LRS remittance per financial year", "Income-tax Act s.206C(1G), Finance Act"),
    ("tcs_rate_percent_general", 20, "percent", "TCS above threshold: other purposes", "Income-tax Act s.206C(1G)"),
    ("tcs_rate_percent_education_medical", 5, "percent", "TCS above threshold: education (own funds) / medical", "Income-tax Act s.206C(1G)"),
    ("tcs_rate_percent_education_loan", 0, "percent", "TCS: education financed by a loan from a financial institution", "Income-tax Act s.206C(1G)"),
    ("tds_rate_percent", 10, "percent", "TDS on deposit interest with PAN", "Income-tax Act s.194A"),
    ("tds_threshold_general_paise", 5000000, "paise", "TDS threshold on deposit interest (non-senior) per FY", "Income-tax Act s.194A"),
    ("tds_threshold_senior_paise", 10000000, "paise", "TDS threshold on deposit interest (senior citizen) per FY", "Income-tax Act s.194A"),
    ("senior_citizen_age", 60, "years", "Senior citizen age for deposit rates", "Bank policy"),
    ("rbi_ombudsman_days", 30, "days", "Days after which an unresolved complaint can go to the RBI Ombudsman", "RBI Integrated Ombudsman Scheme"),
    ("complaint_tat_days", {"atm_card": 5, "digital_payments": 5, "account": 15, "loan": 21, "deposit": 15, "charges": 15,
                            "fraud": 10, "staff_behaviour": 15, "other": 21}, "days", "Complaint resolution TAT by category", "Bank grievance policy / RBI TAT harmonisation"),
    ("complaint_level_days", {"branch": 7, "nodal": 7, "principal_nodal": 7, "internal_ombudsman": 9}, "days",
     "Days at each grievance level before the next escalation", "Bank grievance redressal policy"),
    ("zero_liability_report_days", 3, "working days", "Zero customer liability if unauthorised transaction reported within", "RBI customer liability circular (2017)"),
    ("cert_in_reporting_hours", 6, "hours", "Cyber incident reporting deadline to CERT-In", "CERT-In Directions (28 April 2022)"),
    ("maintenance_notice_hours", 48, "hours", "Minimum advance notice for planned downtime", "Bank service policy"),
    ("imps_max_paise", 50000000, "paise", "IMPS per-transaction limit", "NPCI IMPS"),
    ("upi_max_paise", 10000000, "paise", "UPI per-transaction limit (general)", "NPCI UPI"),
    ("rtgs_min_paise", 20000000, "paise", "RTGS minimum amount", "RBI RTGS"),
]


def _user(conn, *, email, name, user_type, mobile, roles=None, customer_id=None, corporate_id=None) -> str:
    uid = new_id()
    conn.execute(
        """INSERT INTO users (id, user_type, email, mobile, password_hash, display_name, customer_id, corporate_id, staff_roles,
           created_at, last_login_at, last_login_ip) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (uid, user_type, email, mobile, hash_password(SEED_PASSWORD), name, customer_id, corporate_id, jdump(roles or []), now_iso(),
         now_iso(), "203.0.113.10"),
    )
    conn.execute(
        "INSERT INTO mfa_factors (id, user_id, factor_type, credential_ref, label, is_primary, created_at) VALUES (?,?,?,?,?,1,?)",
        (new_id(), uid, "totp", f"hsm://totp/{uid}", "Authenticator app", now_iso()),
    )
    return uid


def _card(conn, customer_id, account_id, kind, network, last4, variant, *, international=False):
    conn.execute(
        """INSERT INTO cards (id, customer_id, account_id, cms_card_ref, card_kind, network, variant, last4, expiry, status,
           international_enabled, created_at, updated_at, last_change_summary) VALUES (?,?,?,?,?,?,?,?,?,'active',?,?,?,?)""",
        (new_id(), customer_id, account_id, "MOCK-CARD-" + new_id(), kind, network, variant, last4, "08/30", int(international),
         now_iso(), now_iso(), "Issued with default controls"),
    )


def seed_core(conn: sqlite3.Connection) -> None:
    env_now = now_iso()
    # Integration adapters: every provider in every environment starts on MOCK.
    for provider in PROVIDERS:
        for env in ENVIRONMENTS:
            conn.execute(
                """INSERT INTO integration_adapters (id, provider, environment, mode, last_health_check, health_status)
                   VALUES (?,?,?,'mock',?,'healthy')""",
                (new_id(), provider, env, env_now),
            )

    staff_ids = {}
    for i, (email, name, roles) in enumerate(STAFF):
        staff_ids[email] = _user(conn, email=email, name=name, user_type="staff", mobile=f"+9190000000{i:02d}", roles=roles)

    core = finacle.MockFinacle(conn)

    # Retail demo customer (affluent, re-KYC due soon, with RM).
    cif = core.create_customer(full_name="Ananya Sharma", mobile="+919876543210", email="customer@example.bank.in",
                               date_of_birth="1988-04-12", pan="ABCPS1234K",
                               address="Flat 1203, Synthetic Heights, Powai, Mumbai, Maharashtra 400076", segment="affluent")
    core.open_account(cif, "savings", account_number="50100012344821", opening_balance_paise=42500000, seed_history=True)
    core.open_account(cif, "savings", account_number="50100012347733", opening_balance_paise=8500000, seed_history=True)
    cust = upsert_customer_from_finacle(conn, cif)
    conn.execute(
        """UPDATE customers SET kyc_status = 'due', kyc_due_date = ?, rm_staff_id = ?, aadhaar_last4 = '4821',
           ckyc_number = '30012345678901' WHERE id = ?""",
        ((today_ist() + timedelta(days=20)).isoformat(), staff_ids["rm@example.bank.in"], cust["id"]),
    )
    conn.execute("UPDATE accounts SET nickname = 'Salary account' WHERE customer_id = ? AND masked_number = 'XXXX4821'", (cust["id"],))
    conn.execute("UPDATE accounts SET nickname = 'Household' WHERE customer_id = ? AND masked_number = 'XXXX7733'", (cust["id"],))
    retail_uid = _user(conn, email="customer@example.bank.in", name="Ananya Sharma", user_type="retail", mobile="+919876543210",
                       customer_id=cust["id"])
    acct = conn.execute("SELECT id FROM accounts WHERE customer_id = ? AND masked_number = 'XXXX4821'", (cust["id"],)).fetchone()
    _card(conn, cust["id"], acct["id"], "debit", "RuPay", "4821", "Platinum Debit")
    _card(conn, cust["id"], acct["id"], "credit", "Visa", "9034", "Signature Credit", international=True)

    # NRI / HNI demo customer with NRE, NRO and FCNR accounts.
    cif2 = core.create_customer(full_name="Rohan Mehta", mobile="+971501234567", email="nri@example.bank.in",
                                date_of_birth="1979-11-02", pan="BCDPM5678L",
                                address="Synthetic Tower, Dubai Marina, Dubai, UAE (Indian address: Pune 411001)",
                                segment="nri", residency_status="nri")
    core.open_account(cif2, "nre", account_number="50200098761234", opening_balance_paise=325000000, seed_history=True)
    core.open_account(cif2, "nro", account_number="50200098765678", opening_balance_paise=12000000, seed_history=True)
    core.open_account(cif2, "fcnr", account_number="50200098769012", currency="USD", opening_balance_paise=5000000)
    cust2 = upsert_customer_from_finacle(conn, cif2)
    conn.execute("UPDATE customers SET rm_staff_id = ?, kyc_due_date = ?, aadhaar_last4 = '7765' WHERE id = ?",
                 (staff_ids["rm@example.bank.in"], (today_ist() + timedelta(days=400)).isoformat(), cust2["id"]))
    _user(conn, email="nri@example.bank.in", name="Rohan Mehta", user_type="retail", mobile="+971501234567", customer_id=cust2["id"])
    nre = conn.execute("SELECT id FROM accounts WHERE customer_id = ? AND account_type = 'nre'", (cust2["id"],)).fetchone()
    _card(conn, cust2["id"], nre["id"], "debit", "Visa", "1234", "Global Debit", international=True)

    # Corporate with maker / checker / authoriser / admin.
    corp_cif = core.create_customer(full_name="Synthetic Traders Pvt Ltd", mobile="+912240000000", email="accounts@synthetic-traders.example",
                                    date_of_birth=None, pan="AAACS1234F", address="Synthetic Industrial Estate, Andheri East, Mumbai 400093",
                                    segment="corporate")
    corp_id = new_id()
    rule = [
        {"max_amount_paise": 100000000, "checkers": 1, "authorisers": 0, "label": "Up to Rs. 10 lakh"},
        {"max_amount_paise": None, "checkers": 1, "authorisers": 1, "label": "Above Rs. 10 lakh"},
    ]
    conn.execute("INSERT INTO corporates (id, legal_name, finacle_cif, gstin, approval_rule) VALUES (?,?,?,?,?)",
                 (corp_id, "Synthetic Traders Pvt Ltd", corp_cif, "27AAACS1234F1Z5", jdump(rule)))
    corp_acct = core.open_account(corp_cif, "current", account_number="50300011112222", opening_balance_paise=2500000000)
    link_account(conn, corp_id, corp_acct["account_number"], "current", owner_type="corporate", nickname="Operating account")
    for email, name, role, limit in (
        ("corp.maker@example.bank.in", "Neha Joshi", "maker", 0),
        ("corp.checker@example.bank.in", "Amit Patel", "checker", 100000000),
        ("corp.authoriser@example.bank.in", "Ritu Agarwal", "authoriser", 1000000000),
        ("corp.admin@example.bank.in", "Sanjay Kapoor", "admin", 0),
    ):
        uid = _user(conn, email=email, name=name, user_type="corporate", mobile="+9191" + new_id().replace("-", "")[:8].translate(
            str.maketrans("abcdef", "123456")), corporate_id=corp_id)
        conn.execute(
            "INSERT INTO corporate_user_limits (id, corporate_id, user_id, role, approval_limit_paise, updated_at) VALUES (?,?,?,?,?,?)",
            (new_id(), corp_id, uid, role, limit, now_iso()),
        )

    # CMS-held regulatory values (each flagged for the compliance team to verify).
    for key, value, unit, label, source in REGULATORY_VALUES:
        seed_published(conn, content_type="regulatory_value", template="generic", slug=f"reg-{key}", locale="en",
                       body={"key": key, "value": value, "unit": unit, "label": label, "source": source,
                             "verification_note": "To be verified by the bank's compliance team against the official source"},
                       needs_compliance_verification=True)
    _ = retail_uid


register_seed(10, "core", seed_core)

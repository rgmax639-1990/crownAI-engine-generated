"""Synthetic payments data for the retail demo customer (customer@example.bank.in).

Three beneficiaries (bank account active, UPI ID active, one still in cooling), a few
historic transfers with matching Finacle postings and rail records (success, a pending
NEFT, a failed IMPS that was reversed), and two open reconciliation exceptions
(rail success without a Finacle posting, and a conflicting duplicate rail callback).
All names and numbers are synthetic.
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta

from banking import account_number
from core import get_reg_value, iso, jdump, mask_account, new_id, one, request_fingerprint, utcnow
from crypto import encrypt_str
from db import register_seed
from integrations import finacle
from integrations.neft_rtgs import MockNeftRtgs
from integrations.npci_imps import MockImps
from integrations.npci_upi import MockUpi


def _beneficiary(conn, customer_id, *, name, payee_type, account=None, ifsc=None, vpa=None, confirmed, cooling_hours, limit,
                 status) -> dict:
    ben = {
        "id": new_id(), "customer_id": customer_id, "name": name, "nickname": None, "payee_type": payee_type,
        "account_number_enc": encrypt_str(account) if account else None, "account_masked": mask_account(account) if account else None,
        "ifsc": ifsc, "vpa": vpa, "name_at_bank": name.upper(), "cooling_until": iso(confirmed + timedelta(hours=cooling_hours)),
        "cooling_limit_paise": limit, "status": status, "created_at": iso(confirmed - timedelta(minutes=3)), "confirmed_at": iso(confirmed),
    }
    conn.execute(
        """INSERT INTO beneficiaries (id, customer_id, name, nickname, payee_type, account_number_enc, account_masked, ifsc, vpa,
           name_at_bank, cooling_until, cooling_limit_paise, status, created_at, confirmed_at) VALUES (:id, :customer_id, :name,
           :nickname, :payee_type, :account_number_enc, :account_masked, :ifsc, :vpa, :name_at_bank, :cooling_until,
           :cooling_limit_paise, :status, :created_at, :confirmed_at)""",
        ben,
    )
    return ben


def seed_transfers(conn: sqlite3.Connection) -> None:
    user = one(conn, "SELECT * FROM users WHERE email = 'customer@example.bank.in'")
    if not user or not user.get("customer_id"):
        return
    cid = user["customer_id"]
    acct = one(conn, "SELECT * FROM accounts WHERE customer_id = ? AND masked_number = 'XXXX4821'", (cid,))
    if not acct:
        return
    number = account_number(acct)
    core = finacle.MockFinacle(conn)
    rails = {"IMPS": MockImps(conn), "UPI": MockUpi(conn), "NEFT": MockNeftRtgs(conn)}
    now = utcnow()
    try:
        hours = int(get_reg_value(conn, "beneficiary_cooling_hours", 24))
        limit = int(get_reg_value(conn, "beneficiary_cooling_limit_paise", 5_000_000))
    except (TypeError, ValueError):
        hours, limit = 24, 5_000_000

    b_bank = _beneficiary(conn, cid, name="Rahul Verma", payee_type="bank_account", account="918273645501", ifsc="SYNB0004521",
                          confirmed=now - timedelta(days=30), cooling_hours=hours, limit=limit, status="active")
    b_vpa = _beneficiary(conn, cid, name="Priya Nair", payee_type="vpa", vpa="priya.nair@oksynth",
                         confirmed=now - timedelta(days=20), cooling_hours=hours, limit=limit, status="active")
    b_new = _beneficiary(conn, cid, name="Kiran Patel", payee_type="bank_account", account="502233445566", ifsc="SYNB0007788",
                         confirmed=now - timedelta(hours=3), cooling_hours=hours, limit=limit, status="cooling")

    def transfer(ben: dict, rail: str, amount: int, remarks: str, ago: timedelta, outcome: str, *, post: bool = True) -> dict:
        tid = new_id()
        created = now - ago
        payee = ben["vpa"] if ben["payee_type"] == "vpa" else f"{ben['account_masked']}@{ben['ifsc']}"
        rail_obj = rails[rail]
        if outcome == "success":
            rec = rail_obj.record(rail, tid, amount, status="success", final_status="success", payee=payee, created_at=iso(created),
                                  settled_at=iso(created + timedelta(seconds=4)))
        elif outcome == "pending":
            rec = rail_obj.record(rail, tid, amount, status="pending", final_status="success", payee=payee, created_at=iso(created),
                                  settle_at=iso(created + timedelta(seconds=60)))
        else:
            rec = rail_obj.record(rail, tid, amount, status="rejected", final_status="rejected", payee=payee, created_at=iso(created),
                                  reason="Beneficiary account is frozen (synthetic)")
        posting_ref = None
        if post:
            p = core.post(number, "DR", amount, f"{rail}/DR/{tid[:8].upper()}/{ben['name']}/{remarks}"[:140],
                          idempotency_ref=f"TRF-{tid}", channel_ref=tid, posted_at=created)
            posting_ref = p["posting_ref"]
            if outcome == "failed":
                core.reverse(posting_ref, f"REV {rail} {tid[:8].upper()} rejected by beneficiary bank")
        key = f"seed-{tid}"
        status = {"success": "success", "pending": "pending", "failed": "failed"}[outcome]
        row = {
            "id": tid, "customer_id": cid, "from_account_id": acct["id"], "beneficiary_id": ben["id"], "rail": rail,
            "amount_paise": amount, "remarks": remarks, "idempotency_key": key,
            "request_hash": request_fingerprint({"from_account_id": acct["id"], "beneficiary_id": ben["id"], "rail": rail,
                                                 "amount_paise": amount, "remarks": remarks}),
            "rail_reference": rec["rail_reference"], "finacle_posting_ref": posting_ref, "status": status,
            "failure_reason": rec["reason"] if outcome == "failed" else None,
            "created_at": iso(created), "updated_at": iso(created + timedelta(seconds=5)),
            "settled_at": iso(created + timedelta(seconds=4)) if outcome == "success" else None,
        }
        conn.execute(
            """INSERT INTO transfers (id, customer_id, from_account_id, beneficiary_id, rail, amount_paise, remarks, idempotency_key,
               request_hash, rail_reference, finacle_posting_ref, status, failure_reason, created_at, updated_at, settled_at)
               VALUES (:id, :customer_id, :from_account_id, :beneficiary_id, :rail, :amount_paise, :remarks, :idempotency_key,
               :request_hash, :rail_reference, :finacle_posting_ref, :status, :failure_reason, :created_at, :updated_at, :settled_at)""",
            row,
        )
        return row

    transfer(b_bank, "IMPS", 250000, "Rent share", timedelta(days=12), "success")
    transfer(b_vpa, "UPI", 120000, "Dinner", timedelta(days=8), "success")
    dup = transfer(b_vpa, "UPI", 75000, "Movie tickets", timedelta(days=5), "success")
    no_post = transfer(b_bank, "IMPS", 500000, "Loan repayment", timedelta(days=3), "success", post=False)
    transfer(b_new, "IMPS", 300000, "Advance", timedelta(hours=2), "failed")
    transfer(b_new, "IMPS", 1000000, "Deposit", timedelta(hours=1), "success")
    transfer(b_bank, "NEFT", 1500000, "Tuition fees", timedelta(minutes=20), "pending")

    # Rail callbacks for the UPI payment: the first confirms, a later duplicate contradicts it.
    first = {"rail_reference": dup["rail_reference"], "status": "success", "amount_paise": 75000, "payment_id": dup["id"]}
    second = {"rail_reference": dup["rail_reference"], "status": "failed", "amount_paise": 75000, "payment_id": dup["id"],
              "reason": "U30 debit not confirmed (synthetic)"}
    conn.execute("INSERT INTO rail_callbacks (id, rail, rail_reference, payload, duplicate, received_at) VALUES (?,?,?,?,0,?)",
                 (new_id(), "UPI", dup["rail_reference"], jdump(first), iso(now - timedelta(days=5) + timedelta(seconds=10))))
    conn.execute("INSERT INTO rail_callbacks (id, rail, rail_reference, payload, duplicate, received_at) VALUES (?,?,?,?,1,?)",
                 (new_id(), "UPI", dup["rail_reference"], jdump(second), iso(now - timedelta(days=5) + timedelta(minutes=7))))

    exceptions = [
        ("transfer", no_post["id"], "rail_success_no_posting", no_post["rail_reference"], no_post["amount_paise"],
         {"transfer_status": "success", "rail": "IMPS", "finacle": {"posted": False, "net_debited": False},
          "note": "Rail confirmed the credit but no Finacle debit was found (synthetic)"}, now - timedelta(days=3) + timedelta(hours=1)),
        ("transfer", dup["id"], "duplicate_callback", dup["rail_reference"], dup["amount_paise"],
         {"first_callback": first, "duplicate_callback": second, "transfer_status": "success"},
         now - timedelta(days=5) + timedelta(minutes=7)),
    ]
    for ptype, pid, mtype, ref, amount, details, created in exceptions:
        conn.execute(
            """INSERT INTO reconciliation_exceptions (id, payment_type, payment_id, mismatch_type, rail_reference, amount_paise, details,
               status, created_at) VALUES (?,?,?,?,?,?,?,'open',?)""",
            (new_id(), ptype, pid, mtype, ref, amount, jdump(details), iso(created)),
        )


register_seed(60, "transfers", seed_transfers)

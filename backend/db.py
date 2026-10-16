"""SQLite persistence for the platform.

The platform database never holds authoritative balances: those live in Finacle
(simulated by the mock adapter in tables prefixed ``mock_finacle_``).

Every request gets its own connection inside a ``BEGIN IMMEDIATE`` transaction so
that writers are serialised. This keeps idempotency checks, the audit hash chain and
the "one active session" rule correct under concurrent requests.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Iterator

DATA_DIR = Path(os.getenv("ASR_DATA_DIR") or Path(__file__).resolve().parent / "data")
DB_PATH = DATA_DIR / "bank.db"

_init_lock = threading.Lock()
_initialized = False
_extra_ddl: list[str] = []
_seeds: list[tuple[int, str, Callable[[sqlite3.Connection], None]]] = []


def register_ddl(ddl: str) -> None:
    """Modules may register additional tables (executed with CREATE ... IF NOT EXISTS)."""
    _extra_ddl.append(ddl)


def register_seed(order: int, name: str, fn: Callable[[sqlite3.Connection], None]) -> None:
    """Register a synthetic-data seed function, run once on first start in ``order``."""
    _seeds.append((order, name, fn))


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- ------------------------------------------------------------------ identity
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  user_type TEXT NOT NULL CHECK (user_type IN ('retail','corporate','staff','prospect')),
  email TEXT NOT NULL UNIQUE COLLATE NOCASE,
  mobile TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  display_name TEXT,
  customer_id TEXT,
  corporate_id TEXT,
  staff_roles TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','locked','disabled')),
  failed_login_count INTEGER NOT NULL DEFAULT 0,
  locked_until TEXT,
  preferred_language TEXT NOT NULL DEFAULT 'en',
  senior_mode INTEGER NOT NULL DEFAULT 0,
  notification_channels TEXT NOT NULL DEFAULT '["sms","email","push"]',
  last_login_at TEXT,
  last_login_ip TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_users_customer ON users(customer_id);
CREATE INDEX IF NOT EXISTS ix_users_corporate ON users(corporate_id);

CREATE TABLE IF NOT EXISTS mfa_factors (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  factor_type TEXT NOT NULL CHECK (factor_type IN ('passkey','totp','push','sms_otp')),
  credential_ref TEXT NOT NULL,
  label TEXT,
  is_primary INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_mfa_user ON mfa_factors(user_id);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  mfa_level TEXT NOT NULL CHECK (mfa_level IN ('password_only','mfa_complete')),
  risk_score INTEGER NOT NULL DEFAULT 0,
  device_fingerprint TEXT,
  ip TEXT,
  created_at TEXT NOT NULL,
  last_activity_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active','expired','terminated','superseded')),
  ended_at TEXT,
  ended_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id, status);

CREATE TABLE IF NOT EXISTS access_tokens (
  token_hash TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('user','onboarding')),
  user_id TEXT,
  session_id TEXT,
  application_id TEXT,
  scope TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_tokens_session ON access_tokens(session_id);

CREATE TABLE IF NOT EXISTS login_attempts (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  user_id TEXT,
  success INTEGER NOT NULL,
  ip TEXT,
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_login_attempts ON login_attempts(email, at);

CREATE TABLE IF NOT EXISTS otp_challenges (
  id TEXT PRIMARY KEY,
  mobile TEXT NOT NULL,
  purpose TEXT NOT NULL,
  code_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  consumed_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_otp_mobile ON otp_challenges(mobile, purpose, created_at);

CREATE TABLE IF NOT EXISTS mfa_challenges (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  session_id TEXT,
  factor_type TEXT NOT NULL,
  factor_id TEXT,
  purpose TEXT NOT NULL CHECK (purpose IN ('login','transaction')),
  code_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  consumed_at TEXT
);

CREATE TABLE IF NOT EXISTS mfa_tokens (
  token_hash TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  session_id TEXT,
  purpose TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT,
  used_for TEXT
);

-- ------------------------------------------------------------------ customers
CREATE TABLE IF NOT EXISTS customers (
  id TEXT PRIMARY KEY,
  finacle_cif TEXT NOT NULL UNIQUE,
  full_name TEXT NOT NULL,
  pan_masked TEXT,
  aadhaar_vault_ref TEXT,
  aadhaar_last4 TEXT,
  date_of_birth TEXT,
  segment TEXT NOT NULL DEFAULT 'retail' CHECK (segment IN ('retail','affluent','hni','uhni','private','nri','msme','corporate')),
  residency_status TEXT NOT NULL DEFAULT 'resident' CHECK (residency_status IN ('resident','nri','pio_oci')),
  kyc_status TEXT NOT NULL DEFAULT 'full' CHECK (kyc_status IN ('full','min_kyc','due','expired')),
  kyc_due_date TEXT,
  ckyc_number TEXT,
  rm_staff_id TEXT,
  high_risk_cooling_until TEXT,
  address TEXT,
  email TEXT,
  mobile TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corporates (
  id TEXT PRIMARY KEY,
  legal_name TEXT NOT NULL,
  finacle_cif TEXT NOT NULL UNIQUE,
  gstin TEXT,
  approval_rule TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS corporate_user_limits (
  id TEXT PRIMARY KEY,
  corporate_id TEXT NOT NULL REFERENCES corporates(id),
  user_id TEXT NOT NULL REFERENCES users(id),
  role TEXT NOT NULL CHECK (role IN ('maker','checker','authoriser','admin')),
  approval_limit_paise INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT,
  UNIQUE (corporate_id, user_id)
);

CREATE TABLE IF NOT EXISTS accounts (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  owner_type TEXT NOT NULL DEFAULT 'customer' CHECK (owner_type IN ('customer','corporate')),
  finacle_account_ref TEXT NOT NULL,
  account_ref_hash TEXT NOT NULL UNIQUE,
  masked_number TEXT NOT NULL,
  account_type TEXT NOT NULL CHECK (account_type IN ('savings','current','fd','rd','loan','nre','nro','fcnr')),
  currency TEXT NOT NULL DEFAULT 'INR',
  ifsc TEXT,
  nickname TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_accounts_customer ON accounts(customer_id);

-- ------------------------------------------------------------------ content (CMS)
CREATE TABLE IF NOT EXISTS content_items (
  id TEXT PRIMARY KEY,
  content_type TEXT NOT NULL CHECK (content_type IN ('home_section','product_page','rate_table','notice','policy','footer_disclosure','faq','branch_page','offer','consent_notice','regulatory_value')),
  template TEXT NOT NULL CHECK (template IN ('product_savings','product_fd','product_loan','product_card','notice','policy','generic')),
  slug TEXT NOT NULL,
  locale TEXT NOT NULL,
  translation_group_id TEXT NOT NULL,
  published_version_id TEXT,
  needs_compliance_verification INTEGER NOT NULL DEFAULT 0,
  created_by TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (slug, locale)
);
CREATE INDEX IF NOT EXISTS ix_content_type ON content_items(content_type, locale);

CREATE TABLE IF NOT EXISTS content_versions (
  id TEXT PRIMARY KEY,
  content_item_id TEXT NOT NULL REFERENCES content_items(id),
  version_no INTEGER NOT NULL,
  body TEXT NOT NULL,
  change_note TEXT,
  status TEXT NOT NULL CHECK (status IN ('draft','submitted','checker_approved','legal_approved','published','rejected','superseded')),
  maker_id TEXT NOT NULL,
  checker_id TEXT,
  legal_approver_id TEXT,
  created_at TEXT NOT NULL,
  submitted_at TEXT,
  checker_approved_at TEXT,
  legal_approved_at TEXT,
  scheduled_publish_at TEXT,
  published_at TEXT,
  published_by TEXT,
  rejected_by TEXT,
  rejected_reason TEXT,
  UNIQUE (content_item_id, version_no)
);
CREATE INDEX IF NOT EXISTS ix_versions_item ON content_versions(content_item_id, status);

CREATE TABLE IF NOT EXISTS rates (
  id TEXT PRIMARY KEY,
  content_version_id TEXT NOT NULL REFERENCES content_versions(id),
  product_type TEXT NOT NULL CHECK (product_type IN ('savings','fd_general','fd_senior','rd','home_loan','personal_loan','vehicle_loan','msme_loan','nre_fd','fcnr')),
  tenor_min_days INTEGER,
  tenor_max_days INTEGER,
  slab_min_paise INTEGER,
  slab_max_paise INTEGER,
  rate_percent REAL NOT NULL,
  benchmark TEXT NOT NULL DEFAULT 'none' CHECK (benchmark IN ('none','EBLR','MCLR','RLLR')),
  benchmark_rate_percent REAL,
  spread_percent REAL,
  as_on_date TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rates_version ON rates(content_version_id);

CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,
  owner_type TEXT NOT NULL CHECK (owner_type IN ('content_item','loan_application','tax_form','onboarding','statement','fixed_deposit','bill_payment','evidence','profile_change')),
  owner_id TEXT NOT NULL,
  doc_type TEXT NOT NULL CHECK (doc_type IN ('kfs','mitc','schedule_of_charges','policy','form_15g','form_15h','form_16a','kyc_doc','esigned_agreement','receipt','fd_advice','statement','evidence_artifact','address_proof')),
  locale TEXT NOT NULL DEFAULT 'en',
  storage_key TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  file_name TEXT,
  content_type TEXT,
  size_bytes INTEGER,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_documents_owner ON documents(owner_type, owner_id);

CREATE TABLE IF NOT EXISTS uploads (
  id TEXT PRIMARY KEY,
  owner_user_id TEXT,
  storage_key TEXT NOT NULL,
  file_name TEXT NOT NULL,
  content_type TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  consumed_at TEXT
);

-- ------------------------------------------------------------------ consent, leads, grievance
CREATE TABLE IF NOT EXISTS consent_records (
  id TEXT PRIMARY KEY,
  subject_type TEXT NOT NULL CHECK (subject_type IN ('visitor','user')),
  subject_id TEXT NOT NULL,
  purpose TEXT NOT NULL CHECK (purpose IN ('essential','analytics','marketing','personalisation','lead_contact','aa_data_fetch','bureau_pull','gstn_itr_fetch','onboarding_kyc','aadhaar_ekyc')),
  granted INTEGER NOT NULL,
  notice_version_id TEXT,
  language TEXT NOT NULL DEFAULT 'en',
  recorded_at TEXT NOT NULL,
  withdrawn_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_consent_subject ON consent_records(subject_id, purpose);

CREATE TABLE IF NOT EXISTS branches (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('branch','atm','cash_recycler')),
  name TEXT NOT NULL,
  name_hi TEXT,
  slug TEXT NOT NULL UNIQUE,
  ifsc TEXT UNIQUE,
  micr TEXT,
  address TEXT NOT NULL,
  city TEXT NOT NULL,
  state TEXT NOT NULL,
  pin_code TEXT NOT NULL,
  lat REAL NOT NULL,
  lng REAL NOT NULL,
  timings TEXT NOT NULL DEFAULT '{}',
  services TEXT NOT NULL DEFAULT '[]',
  phone TEXT,
  is_24x7 INTEGER NOT NULL DEFAULT 0,
  cash_deposit INTEGER NOT NULL DEFAULT 0,
  locker INTEGER NOT NULL DEFAULT 0,
  wheelchair_access INTEGER NOT NULL DEFAULT 0,
  forex INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_branches_pin ON branches(pin_code);

CREATE TABLE IF NOT EXISTS holidays (
  id TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  date TEXT NOT NULL,
  name TEXT NOT NULL,
  name_hi TEXT
);
CREATE INDEX IF NOT EXISTS ix_holidays_state ON holidays(state, date);

CREATE TABLE IF NOT EXISTS calculator_shares (
  id TEXT PRIMARY KEY,
  calculator TEXT NOT NULL CHECK (calculator IN ('emi','fd','rd','sip','income_tax','lrs_tcs')),
  inputs TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
  id TEXT PRIMARY KEY,
  reference_no TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  mobile TEXT NOT NULL,
  email TEXT,
  pin_code TEXT,
  product_slug TEXT NOT NULL,
  channel TEXT NOT NULL CHECK (channel IN ('web','mobile_web','calculator','assistant','branch_page')),
  utm TEXT NOT NULL DEFAULT '{}',
  consent_record_id TEXT NOT NULL,
  calculator_share_id TEXT,
  crm_ref TEXT,
  status TEXT NOT NULL CHECK (status IN ('created','synced_to_crm','contacted','closed')),
  language TEXT NOT NULL DEFAULT 'en',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS complaints (
  id TEXT PRIMARY KEY,
  ticket_id TEXT NOT NULL UNIQUE,
  customer_id TEXT,
  name TEXT NOT NULL,
  mobile TEXT NOT NULL,
  email TEXT,
  category TEXT NOT NULL CHECK (category IN ('atm_card','digital_payments','account','loan','deposit','charges','fraud','staff_behaviour','other')),
  description TEXT NOT NULL,
  account_last4 TEXT,
  current_level TEXT NOT NULL CHECK (current_level IN ('branch','nodal','principal_nodal','internal_ombudsman')),
  status TEXT NOT NULL,
  tat_due_at TEXT NOT NULL,
  rbi_ombudsman_eligible_at TEXT NOT NULL,
  assigned_officer_id TEXT,
  resolution_note TEXT,
  language TEXT NOT NULL DEFAULT 'en',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_complaints_customer ON complaints(customer_id);

CREATE TABLE IF NOT EXISTS complaint_events (
  id TEXT PRIMARY KEY,
  complaint_id TEXT NOT NULL REFERENCES complaints(id),
  from_status TEXT,
  to_status TEXT NOT NULL,
  from_level TEXT,
  to_level TEXT,
  actor_type TEXT NOT NULL,
  actor_id TEXT,
  note TEXT,
  customer_visible INTEGER NOT NULL DEFAULT 1,
  occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_complaint_events ON complaint_events(complaint_id, occurred_at);

CREATE TABLE IF NOT EXISTS fraud_reports (
  id TEXT PRIMARY KEY,
  reference_no TEXT NOT NULL UNIQUE,
  customer_id TEXT,
  card_id TEXT,
  card_last4 TEXT,
  mobile_masked TEXT,
  category TEXT NOT NULL CHECK (category IN ('card_misuse','upi_fraud','phishing','account_takeover','other')),
  description TEXT,
  reported_at TEXT NOT NULL,
  card_blocked_at TEXT,
  status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cards (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  account_id TEXT,
  cms_card_ref TEXT NOT NULL UNIQUE,
  card_kind TEXT NOT NULL CHECK (card_kind IN ('debit','credit','virtual_debit')),
  network TEXT NOT NULL DEFAULT 'RuPay',
  variant TEXT,
  last4 TEXT NOT NULL,
  expiry TEXT,
  status TEXT NOT NULL CHECK (status IN ('active','temporarily_blocked','permanently_blocked')),
  domestic_enabled INTEGER NOT NULL DEFAULT 1,
  international_enabled INTEGER NOT NULL DEFAULT 0,
  online_enabled INTEGER NOT NULL DEFAULT 1,
  contactless_enabled INTEGER NOT NULL DEFAULT 1,
  atm_limit_paise INTEGER NOT NULL DEFAULT 4000000,
  pos_limit_paise INTEGER NOT NULL DEFAULT 10000000,
  online_limit_paise INTEGER NOT NULL DEFAULT 10000000,
  max_atm_limit_paise INTEGER NOT NULL DEFAULT 10000000,
  max_pos_limit_paise INTEGER NOT NULL DEFAULT 50000000,
  max_online_limit_paise INTEGER NOT NULL DEFAULT 50000000,
  blocked_at TEXT,
  updated_at TEXT,
  last_change_summary TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cards_customer ON cards(customer_id);

-- ------------------------------------------------------------------ onboarding & V-CIP
CREATE TABLE IF NOT EXISTS onboarding_applications (
  id TEXT PRIMARY KEY,
  product_code TEXT NOT NULL,
  mobile TEXT NOT NULL,
  email TEXT NOT NULL,
  full_name TEXT,
  date_of_birth TEXT,
  pan_masked TEXT,
  pan_verified INTEGER NOT NULL DEFAULT 0,
  ekyc_method TEXT CHECK (ekyc_method IN ('aadhaar_otp','digilocker')),
  ekyc_completed INTEGER NOT NULL DEFAULT 0,
  ekyc_txn_id TEXT,
  aadhaar_vault_ref TEXT,
  aadhaar_last4 TEXT,
  ekyc_address TEXT,
  vcip_session_id TEXT,
  ckyc_result TEXT NOT NULL DEFAULT 'not_run' CHECK (ckyc_result IN ('not_run','found','not_found_uploaded','mismatch')),
  ckyc_number TEXT,
  finacle_cif TEXT,
  account_masked_number TEXT,
  customer_id TEXT,
  virtual_card_id TEXT,
  consent_record_ids TEXT NOT NULL DEFAULT '[]',
  resume_token_hash TEXT,
  resume_expires_at TEXT,
  language TEXT NOT NULL DEFAULT 'en',
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vcip_sessions (
  id TEXT PRIMARY KEY,
  application_id TEXT NOT NULL REFERENCES onboarding_applications(id),
  officer_id TEXT,
  scheduled_at TEXT,
  joined_at TEXT,
  geo_lat REAL,
  geo_lng REAL,
  liveness_passed INTEGER,
  face_match_score REAL,
  random_code TEXT,
  recording_storage_key TEXT,
  status TEXT NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled','waiting','in_call','completed','cancelled')),
  decision TEXT NOT NULL DEFAULT 'pending' CHECK (decision IN ('pending','approved','rejected','reschedule')),
  remarks TEXT,
  decided_at TEXT,
  created_at TEXT NOT NULL
);

-- ------------------------------------------------------------------ loans
CREATE TABLE IF NOT EXISTS loan_applications (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  loan_type TEXT NOT NULL CHECK (loan_type IN ('personal','msme')),
  amount_paise INTEGER NOT NULL,
  tenure_months INTEGER NOT NULL,
  gstin TEXT,
  calculator_share_id TEXT,
  consent_record_ids TEXT NOT NULL DEFAULT '[]',
  aa_consent_handle TEXT,
  bureau_score INTEGER,
  underwriting_result TEXT,
  interest_rate_percent REAL,
  apr_percent REAL,
  processing_fee_paise INTEGER,
  emi_paise INTEGER,
  kfs_document_id TEXT,
  kfs_accepted_at TEXT,
  esign_ref TEXT,
  enach_mandate_ref TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- ------------------------------------------------------------------ payments
CREATE TABLE IF NOT EXISTS beneficiaries (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  name TEXT NOT NULL,
  nickname TEXT,
  payee_type TEXT NOT NULL CHECK (payee_type IN ('bank_account','vpa')),
  account_number_enc TEXT,
  account_masked TEXT,
  ifsc TEXT,
  vpa TEXT,
  name_at_bank TEXT,
  cooling_until TEXT,
  cooling_limit_paise INTEGER,
  status TEXT NOT NULL CHECK (status IN ('pending_otp','cooling','active','deleted')),
  created_at TEXT NOT NULL,
  confirmed_at TEXT,
  deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_beneficiaries_customer ON beneficiaries(customer_id);

CREATE TABLE IF NOT EXISTS transfers (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  from_account_id TEXT NOT NULL,
  beneficiary_id TEXT,
  rail TEXT NOT NULL CHECK (rail IN ('IMPS','NEFT','RTGS','UPI','INTERNAL')),
  amount_paise INTEGER NOT NULL,
  remarks TEXT,
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  rail_reference TEXT UNIQUE,
  finacle_posting_ref TEXT,
  status TEXT NOT NULL,
  failure_reason TEXT,
  bulk_file_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  settled_at TEXT,
  UNIQUE (customer_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_transfers_customer ON transfers(customer_id, created_at);

CREATE TABLE IF NOT EXISTS rail_callbacks (
  id TEXT PRIMARY KEY,
  rail TEXT NOT NULL,
  rail_reference TEXT NOT NULL,
  payload TEXT NOT NULL,
  duplicate INTEGER NOT NULL DEFAULT 0,
  received_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rail_callbacks_ref ON rail_callbacks(rail_reference);

CREATE TABLE IF NOT EXISTS reconciliation_exceptions (
  id TEXT PRIMARY KEY,
  payment_type TEXT NOT NULL CHECK (payment_type IN ('transfer','bill_payment','lrs_remittance')),
  payment_id TEXT NOT NULL,
  mismatch_type TEXT NOT NULL CHECK (mismatch_type IN ('rail_success_no_posting','posting_no_rail_success','amount_mismatch','duplicate_callback')),
  rail_reference TEXT,
  amount_paise INTEGER,
  details TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved')),
  resolution TEXT,
  resolution_note TEXT,
  resolved_by TEXT,
  resolved_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bill_payments (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  payment_kind TEXT NOT NULL CHECK (payment_kind IN ('bbps_bill','gst_challan','income_tax_challan')),
  biller_id TEXT,
  biller_name TEXT,
  consumer_number TEXT,
  cpin TEXT,
  pan TEXT,
  assessment_year TEXT,
  tax_type TEXT,
  amount_paise INTEGER NOT NULL,
  from_account_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  rail_reference TEXT,
  finacle_posting_ref TEXT,
  receipt_document_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('initiated','pending','success','failed')),
  failure_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (customer_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS fixed_deposits (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  from_account_id TEXT NOT NULL,
  finacle_deposit_ref TEXT,
  principal_paise INTEGER NOT NULL,
  tenure_days INTEGER NOT NULL,
  rate_percent REAL NOT NULL,
  senior_citizen INTEGER NOT NULL DEFAULT 0,
  payout TEXT NOT NULL CHECK (payout IN ('cumulative','monthly','quarterly')),
  maturity_instruction TEXT NOT NULL CHECK (maturity_instruction IN ('auto_renew_principal_interest','auto_renew_principal','credit_to_account')),
  maturity_date TEXT,
  maturity_amount_paise INTEGER,
  advice_document_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('requested','booked','failed','closed')),
  failure_reason TEXT,
  created_at TEXT NOT NULL,
  booked_at TEXT
);

CREATE TABLE IF NOT EXISTS tax_forms (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  form_type TEXT NOT NULL CHECK (form_type IN ('15G','15H','16A')),
  financial_year TEXT NOT NULL,
  quarter TEXT,
  estimated_income_paise INTEGER,
  acknowledgement_no TEXT,
  document_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('submitted','recorded_in_finacle','generated')),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positive_pay_entries (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  customer_id TEXT NOT NULL,
  reference_no TEXT NOT NULL,
  cheque_number TEXT NOT NULL,
  cheque_date TEXT NOT NULL,
  amount_paise INTEGER NOT NULL,
  payee_name TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('submitted','sent_to_clearing','matched','mismatch')),
  created_at TEXT NOT NULL,
  UNIQUE (account_id, cheque_number)
);

CREATE TABLE IF NOT EXISTS cheque_service_requests (
  id TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  customer_id TEXT NOT NULL,
  reference_no TEXT NOT NULL,
  request_type TEXT NOT NULL CHECK (request_type IN ('cheque_book','stop_payment')),
  leaves INTEGER,
  cheque_from TEXT,
  cheque_to TEXT,
  reason TEXT,
  delivery_address TEXT,
  status TEXT NOT NULL CHECK (status IN ('submitted','processed','rejected')),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_change_requests (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  change_type TEXT NOT NULL CHECK (change_type IN ('mobile','email','address','rekyc')),
  old_value_masked TEXT,
  new_value_enc TEXT,
  new_value_masked TEXT,
  supporting_document_id TEXT,
  no_change_declaration INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  cooling_until TEXT,
  created_at TEXT NOT NULL,
  verified_at TEXT,
  applied_at TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type IN ('login','transaction','beneficiary_added','card_control','profile_change','complaint_update','security')),
  channels TEXT NOT NULL DEFAULT '[]',
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  triggered_at TEXT NOT NULL,
  delivered_at TEXT,
  read_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_notifications_user ON notifications(user_id, triggered_at);

-- ------------------------------------------------------------------ integrations & governance
CREATE TABLE IF NOT EXISTS integration_adapters (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  environment TEXT NOT NULL CHECK (environment IN ('dev','qa','uat','production')),
  mode TEXT NOT NULL DEFAULT 'mock' CHECK (mode IN ('mock','live_requested','live_approved','live')),
  active_approval_id TEXT,
  last_health_check TEXT,
  health_status TEXT NOT NULL DEFAULT 'healthy',
  UNIQUE (provider, environment)
);

CREATE TABLE IF NOT EXISTS integration_approvals (
  id TEXT PRIMARY KEY,
  adapter_id TEXT NOT NULL REFERENCES integration_adapters(id),
  requested_by TEXT NOT NULL,
  approved_by TEXT,
  evidence_refs TEXT NOT NULL,
  justification TEXT NOT NULL,
  decision TEXT NOT NULL DEFAULT 'pending' CHECK (decision IN ('pending','approved','rejected')),
  decision_comment TEXT,
  requested_at TEXT NOT NULL,
  decided_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT NOT NULL UNIQUE,
  category TEXT NOT NULL CHECK (category IN ('login','financial','admin','content','consent','integration')),
  actor_type TEXT NOT NULL CHECK (actor_type IN ('customer','staff','system','visitor')),
  actor_id TEXT,
  action TEXT NOT NULL,
  entity_type TEXT,
  entity_id TEXT,
  details TEXT NOT NULL DEFAULT '{}',
  ip TEXT,
  occurred_at TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL,
  retain_until TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_entity ON audit_events(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS ix_audit_time ON audit_events(occurred_at);
CREATE TRIGGER IF NOT EXISTS audit_events_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_events_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;

CREATE TABLE IF NOT EXISTS incidents (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  severity TEXT NOT NULL CHECK (severity IN ('low','medium','high','critical')),
  cert_in_category TEXT,
  detected_at TEXT NOT NULL,
  cert_in_due_at TEXT NOT NULL,
  reported_at TEXT,
  cert_in_reference TEXT,
  status TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '[]',
  created_by TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_records (
  id TEXT PRIMARY KEY,
  evidence_type TEXT NOT NULL CHECK (evidence_type IN ('load_test','dr_drill','vapt','test_run','accessibility_audit','architecture_review')),
  srs_ids TEXT NOT NULL DEFAULT '[]',
  environment TEXT NOT NULL,
  executed_at TEXT NOT NULL,
  result TEXT NOT NULL CHECK (result IN ('passed','failed','partial')),
  metrics TEXT NOT NULL DEFAULT '{}',
  artifact_storage_key TEXT NOT NULL,
  artifact_file_name TEXT,
  artifact_sha256 TEXT,
  recorded_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS maintenance_notices (
  id TEXT PRIMARY KEY,
  service TEXT NOT NULL CHECK (service IN ('website','internet_banking','upi','cards')),
  starts_at TEXT NOT NULL,
  ends_at TEXT NOT NULL,
  message_en TEXT NOT NULL,
  message_hi TEXT NOT NULL,
  created_by TEXT,
  created_at TEXT NOT NULL
);

-- ------------------------------------------------------------------ corporate, wealth, NRI, insights
CREATE TABLE IF NOT EXISTS bulk_payment_files (
  id TEXT PRIMARY KEY,
  corporate_id TEXT NOT NULL,
  file_kind TEXT NOT NULL CHECK (file_kind IN ('vendor_payment','payroll')),
  debit_account_id TEXT NOT NULL,
  upload_ref TEXT,
  file_name TEXT,
  record_count INTEGER NOT NULL DEFAULT 0,
  total_amount_paise INTEGER NOT NULL DEFAULT 0,
  records TEXT NOT NULL DEFAULT '[]',
  validation TEXT NOT NULL DEFAULT '{}',
  created_by TEXT NOT NULL,
  approvals TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL,
  rejected_reason TEXT,
  created_at TEXT NOT NULL,
  submitted_at TEXT,
  released_at TEXT
);

CREATE TABLE IF NOT EXISTS mutual_fund_orders (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  scheme_code TEXT NOT NULL,
  scheme_name TEXT,
  order_type TEXT NOT NULL CHECK (order_type IN ('purchase','redeem','sip')),
  amount_paise INTEGER,
  units REAL,
  folio TEXT,
  rta_ref TEXT,
  status TEXT NOT NULL CHECK (status IN ('placed','confirmed','rejected')),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rm_contact_requests (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  rm_staff_id TEXT,
  channel TEXT NOT NULL CHECK (channel IN ('call','whatsapp','video')),
  preferred_time TEXT,
  message TEXT,
  status TEXT NOT NULL CHECK (status IN ('requested','scheduled','done')),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lrs_quotes (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  currency TEXT NOT NULL,
  fx_amount_minor INTEGER NOT NULL,
  purpose_code TEXT NOT NULL,
  fx_rate REAL NOT NULL,
  inr_amount_paise INTEGER NOT NULL,
  tcs_paise INTEGER NOT NULL,
  details TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at TEXT
);

CREATE TABLE IF NOT EXISTS lrs_remittances (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  quote_id TEXT,
  from_account_id TEXT NOT NULL,
  beneficiary_name TEXT NOT NULL,
  beneficiary_account TEXT NOT NULL,
  beneficiary_address TEXT,
  swift_bic TEXT NOT NULL,
  currency TEXT NOT NULL,
  fx_amount_minor INTEGER NOT NULL,
  fx_rate REAL,
  inr_amount_paise INTEGER NOT NULL,
  purpose_code TEXT NOT NULL,
  tcs_paise INTEGER NOT NULL,
  form_a2 TEXT NOT NULL DEFAULT '{}',
  swift_uetr TEXT,
  finacle_posting_ref TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budgets (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  category TEXT NOT NULL CHECK (category IN ('food','travel','shopping','bills','fuel','entertainment','health','other')),
  monthly_limit_paise INTEGER NOT NULL,
  updated_at TEXT,
  UNIQUE (customer_id, category)
);

CREATE TABLE IF NOT EXISTS bill_reminders (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL,
  biller_id TEXT NOT NULL,
  biller_name TEXT,
  due_day INTEGER NOT NULL CHECK (due_day BETWEEN 1 AND 31),
  amount_paise INTEGER,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS offers (
  id TEXT PRIMARY KEY,
  content_item_id TEXT NOT NULL,
  segment_rules TEXT NOT NULL DEFAULT '{}',
  valid_from TEXT NOT NULL,
  valid_to TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assistant_sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT,
  locale TEXT NOT NULL CHECK (locale IN ('en','hi','hinglish')),
  messages TEXT NOT NULL DEFAULT '[]',
  handoff_status TEXT NOT NULL DEFAULT 'none' CHECK (handoff_status IN ('none','requested','with_agent','closed')),
  queue_position INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- ------------------------------------------------------------------ transactional outbox
CREATE TABLE IF NOT EXISTS outbox (
  id TEXT PRIMARY KEY,
  topic TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL,
  processed_at TEXT
);
"""


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _initialize() -> None:
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        conn = _connect(DB_PATH)
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            for ddl in _extra_ddl:
                conn.executescript(ddl)
            seeded = conn.execute("SELECT value FROM meta WHERE key = 'seeded'").fetchone()
            if not seeded:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    for _order, _name, fn in sorted(_seeds, key=lambda s: s[0]):
                        fn(conn)
                    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('seeded', '1')")
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
        finally:
            conn.close()
        _initialized = True


def connect() -> sqlite3.Connection:
    """A raw connection (autocommit mode). Callers manage transactions."""
    _initialize()
    return _connect(DB_PATH)


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: one connection and one IMMEDIATE transaction per request.

    Commits when the handler returns, rolls back when it raises. Use
    :func:`commit_now` when something must persist even though the handler then
    raises (for example counting a failed login before returning 401).
    """
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        if conn.in_transaction:
            conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def commit_now(conn: sqlite3.Connection) -> None:
    """Commit the current transaction and immediately start a new IMMEDIATE one."""
    if conn.in_transaction:
        conn.execute("COMMIT")
    conn.execute("BEGIN IMMEDIATE")

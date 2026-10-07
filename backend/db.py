"""SQLite persistence layer for the CrownAI-website backend.

A fresh connection is opened per call (SQLite connections are not safe to
share across threads, and FastAPI's sync path operations run in a
threadpool). WAL mode (set once in init_db -- it is persistent in the file)
+ a busy timeout keep concurrent requests from tripping over each other.
Any exception inside a `get_conn()` block rolls the whole request's writes
back, because commit only happens on a clean exit.
"""
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("CROWNAI_DB_PATH", os.path.join(os.path.dirname(__file__), "crownai.db"))


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_conn():
    # busy_timeout bounds how long a request waits on another writer; it is
    # kept well under the frontend's mutation timeout (15s) so a contended
    # write fails fast with a clear error instead of the browser aborting.
    conn = sqlite3.connect(DB_PATH, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate_payments_user_id_nullable(conn):
    """Older databases created payments.user_id as NOT NULL; anonymous
    checkout (lead captured, no session yet) needs it nullable."""
    cols = conn.execute("PRAGMA table_info(payments)").fetchall()
    user_col = next((c for c in cols if c["name"] == "user_id"), None)
    if not user_col or not user_col["notnull"]:
        return
    # Runs inside init_db's write transaction: plain execute() calls, since
    # executescript() would commit that transaction first.
    for stmt in (
        """CREATE TABLE payments_new (
            id TEXT PRIMARY KEY,
            user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
            lead_id TEXT REFERENCES leads(id),
            plan TEXT NOT NULL,
            amount_cents INTEGER NOT NULL,
            currency TEXT NOT NULL,
            stripe_session_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        )""",
        """INSERT INTO payments_new (id, user_id, lead_id, plan, amount_cents, currency, stripe_session_id, status, created_at)
            SELECT id, user_id, lead_id, plan, amount_cents, currency, stripe_session_id, status, created_at FROM payments""",
        "DROP TABLE payments",
        "ALTER TABLE payments_new RENAME TO payments",
        "CREATE INDEX IF NOT EXISTS idx_payments_user_id ON payments(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_payments_stripe_session_id ON payments(stripe_session_id)",
    ):
        conn.execute(stmt)


# Serialises boots within one process; boots in separate processes are
# serialised by the BEGIN IMMEDIATE write lock in init_db.
_init_lock = threading.Lock()


def _enable_wal(attempts: int = 50, delay: float = 0.1):
    """Switching journal mode needs an exclusive lock and SQLite reports
    SQLITE_BUSY for it without consulting the busy handler, so retry."""
    for attempt in range(attempts):
        try:
            with get_conn() as conn:
                mode = conn.execute("PRAGMA journal_mode=WAL;").fetchone()[0]
            if str(mode).lower() == "wal":
                return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
        if attempt < attempts - 1:
            time.sleep(delay)


def init_db():
    with _init_lock:
        _enable_wal()
        _create_schema()


def _create_schema():
    with get_conn() as conn:
        # BEGIN IMMEDIATE takes the write lock up front (honouring the busy
        # timeout), so concurrent boots run one after another instead of
        # failing with "database is locked" when upgrading a read lock.
        conn.executescript(
            """
            BEGIN IMMEDIATE;

            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                provider TEXT NOT NULL,
                provider_sub TEXT NOT NULL,
                tier TEXT NOT NULL DEFAULT 'free',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                requirements TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'requirements',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                stage TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS leads (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                company TEXT NOT NULL,
                phone TEXT NOT NULL,
                plan TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS payments (
                id TEXT PRIMARY KEY,
                -- NULL for an anonymous checkout; claimed by the user whose
                -- email matches the lead once they sign in (see upsert_user).
                user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
                lead_id TEXT REFERENCES leads(id),
                plan TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                currency TEXT NOT NULL,
                stripe_session_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS contact_inquiries (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS usage_counters (
                user_id TEXT NOT NULL,
                day TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, day)
            );

            -- SQLite does not auto-index a foreign-key column (only the
            -- referenced primary key), so every workspace/project-detail
            -- load and download would otherwise full-table-scan these on
            -- their hot-path lookups (list_projects, list_artifacts,
            -- get_artifact_by_stage) as the tables grow.
            CREATE INDEX IF NOT EXISTS idx_projects_user_id ON projects(user_id);
            CREATE INDEX IF NOT EXISTS idx_artifacts_project_id ON artifacts(project_id);
            CREATE INDEX IF NOT EXISTS idx_payments_user_id ON payments(user_id);
            CREATE INDEX IF NOT EXISTS idx_payments_stripe_session_id ON payments(stripe_session_id);
            -- Email lookups are case-insensitive (lower(email) = lower(?)).
            CREATE INDEX IF NOT EXISTS idx_users_email_lower ON users(lower(email));
            CREATE INDEX IF NOT EXISTS idx_leads_email_lower ON leads(lower(email));
            """
        )
        _migrate_payments_user_id_nullable(conn)


# ---------- users ----------

def get_user(conn, user_id: str):
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def normalize_email(email: str) -> str:
    """Email addresses are compared case-insensitively everywhere (OAuth
    providers and people typing into the lead form capitalise differently),
    so users and leads are stored in this canonical form."""
    return (email or "").strip().lower()


def get_user_by_email(conn, email: str):
    # lower() on both sides also matches rows saved before emails were
    # normalised; if legacy case-only duplicates exist, prefer the exact
    # match, then the oldest account.
    return conn.execute(
        """SELECT * FROM users WHERE lower(email) = lower(?)
           ORDER BY (email = ?) DESC, created_at ASC LIMIT 1""",
        (email.strip(), email.strip()),
    ).fetchone()


def upsert_user(conn, email: str, name: str, provider: str, provider_sub: str):
    email = normalize_email(email)
    existing = get_user_by_email(conn, email)
    if existing:
        uid = existing["id"]
    else:
        uid = new_id()
        conn.execute(
            "INSERT INTO users (id, email, name, provider, provider_sub, tier, created_at) VALUES (?, ?, ?, ?, ?, 'free', ?)",
            (uid, email, name, provider, provider_sub, now()),
        )
    claim_anonymous_payments(conn, uid, email)
    return get_user(conn, uid)


def claim_anonymous_payments(conn, user_id: str, email: str) -> bool:
    """Attaches anonymous (pre-sign-in) payments whose lead email matches this
    user, and upgrades the user if any of them already succeeded."""
    rows = conn.execute(
        """SELECT p.id, p.status, p.plan FROM payments p JOIN leads l ON l.id = p.lead_id
           WHERE p.user_id IS NULL AND lower(l.email) = lower(?)""",
        (email.strip(),),
    ).fetchall()
    if not rows:
        return False
    conn.execute(
        f"UPDATE payments SET user_id = ? WHERE id IN ({','.join('?' * len(rows))})",
        (user_id, *[r["id"] for r in rows]),
    )
    paid = [r for r in rows if r["status"] == "succeeded"]
    for r in paid:
        upgrade_user_tier(conn, user_id, r["plan"])
    return bool(paid)


def set_user_tier(conn, user_id: str, tier: str):
    conn.execute("UPDATE users SET tier = ? WHERE id = ?", (tier, user_id))


def delete_user(conn, user_id: str):
    """Erases an account and its Crown AI data (data-rights erasure request).
    Projects/artifacts cascade; payment records are kept for statutory
    accounting but detached from the deleted account."""
    conn.execute("UPDATE payments SET user_id = NULL WHERE user_id = ?", (user_id,))
    conn.execute(
        "DELETE FROM artifacts WHERE project_id IN (SELECT id FROM projects WHERE user_id = ?)", (user_id,)
    )
    conn.execute("DELETE FROM projects WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM usage_counters WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


# "pro"/"enterprise" are legacy tiers from earlier plans, kept so existing
# paid accounts still rank above free.
TIER_RANK = {"free": 0, "pro": 1, "mid": 1, "enterprise": 2, "large": 2, "global": 3}


def upgrade_user_tier(conn, user_id: str, plan: str):
    """Raises the user's tier to the purchased plan; a purchase never lowers
    it (e.g. a Global user buying Mid stays Global)."""
    user = get_user(conn, user_id)
    if user and TIER_RANK.get(plan, 0) > TIER_RANK.get(user["tier"], 0):
        set_user_tier(conn, user_id, plan)


# ---------- projects ----------

def create_project(conn, user_id: str, name: str, requirements: str):
    pid = new_id()
    conn.execute(
        "INSERT INTO projects (id, user_id, name, requirements, status, created_at) VALUES (?, ?, ?, ?, 'requirements', ?)",
        (pid, user_id, name, requirements, now()),
    )
    return get_project(conn, pid)


def get_project(conn, project_id: str):
    return conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()


def list_projects(conn, user_id: str):
    return conn.execute(
        "SELECT * FROM projects WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
    ).fetchall()


def delete_project(conn, project_id: str):
    conn.execute("DELETE FROM artifacts WHERE project_id = ?", (project_id,))
    conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


def set_project_status(conn, project_id: str, status: str):
    conn.execute("UPDATE projects SET status = ? WHERE id = ?", (status, project_id))


# ---------- artifacts ----------

def add_artifact(conn, project_id: str, stage: str, content: str):
    aid = new_id()
    conn.execute(
        "INSERT INTO artifacts (id, project_id, stage, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (aid, project_id, stage, content, now()),
    )
    return conn.execute("SELECT * FROM artifacts WHERE id = ?", (aid,)).fetchone()


def list_artifacts(conn, project_id: str):
    return conn.execute(
        "SELECT * FROM artifacts WHERE project_id = ? ORDER BY created_at ASC", (project_id,)
    ).fetchall()


def get_artifact_by_stage(conn, project_id: str, stage: str):
    return conn.execute(
        "SELECT * FROM artifacts WHERE project_id = ? AND stage = ? ORDER BY created_at DESC LIMIT 1",
        (project_id, stage),
    ).fetchone()


# ---------- leads ----------

def create_lead(conn, name: str, email: str, company: str, phone: str, plan: str):
    email = normalize_email(email)
    lid = new_id()
    conn.execute(
        "INSERT INTO leads (id, name, email, company, phone, plan, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (lid, name, email, company, phone, plan, now()),
    )
    return conn.execute("SELECT * FROM leads WHERE id = ?", (lid,)).fetchone()


def get_lead(conn, lead_id: str):
    return conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()


# ---------- payments ----------

def create_payment(conn, user_id: str | None, lead_id: str | None, plan: str, amount_cents: int, currency: str, stripe_session_id: str):
    pid = new_id()
    conn.execute(
        """INSERT INTO payments (id, user_id, lead_id, plan, amount_cents, currency, stripe_session_id, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (pid, user_id, lead_id, plan, amount_cents, currency, stripe_session_id, now()),
    )
    return get_payment(conn, pid)


def get_payment(conn, payment_id: str):
    return conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()


def get_payment_by_session(conn, stripe_session_id: str):
    return conn.execute(
        "SELECT * FROM payments WHERE stripe_session_id = ?", (stripe_session_id,)
    ).fetchone()


def set_payment_status(conn, payment_id: str, status: str):
    conn.execute("UPDATE payments SET status = ? WHERE id = ?", (status, payment_id))


def set_payment_user(conn, payment_id: str, user_id: str):
    conn.execute("UPDATE payments SET user_id = ? WHERE id = ?", (user_id, payment_id))


# ---------- contact ----------

def create_contact_inquiry(conn, name: str, email: str, message: str):
    cid = new_id()
    conn.execute(
        "INSERT INTO contact_inquiries (id, name, email, message, created_at) VALUES (?, ?, ?, ?, ?)",
        (cid, name, email, message, now()),
    )
    return conn.execute("SELECT * FROM contact_inquiries WHERE id = ?", (cid,)).fetchone()


# ---------- usage / rate limiting ----------

def reserve_generation_quota(conn, user_id: str, day: str, limit: int) -> bool:
    """Atomically claims one generation slot for (user, day).

    Takes the database write lock first (BEGIN IMMEDIATE) and then does a
    single conditional upsert that only bumps the counter while it is below
    `limit`, so concurrent requests can never all read "under the cap" and
    all proceed. Returns False (nothing written) when the cap is reached.
    The lock is held until the caller's `get_conn()` block commits, and any
    later failure in that request rolls the reservation back.
    """
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    cur = conn.execute(
        """INSERT INTO usage_counters (user_id, day, count)
           SELECT ?, ?, 1 WHERE ? > 0
           ON CONFLICT(user_id, day) DO UPDATE SET count = count + 1 WHERE count < ?""",
        (user_id, day, limit, limit),
    )
    return cur.rowcount == 1


def get_usage(conn, user_id: str, day: str) -> int:
    row = conn.execute(
        "SELECT count FROM usage_counters WHERE user_id = ? AND day = ?", (user_id, day)
    ).fetchone()
    return row["count"] if row else 0

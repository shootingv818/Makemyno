"""
db.py — the CUSTOMER-facing operational database (data/customer.db).
====================================================================

This is one of the project's two logically separate databases. It holds
everything that belongs to customers: their subscription, their Rubika and
Telegram accounts, their content, their jobs, their per-customer settings and
their usage counters. Plus the shared worker fleet rows (the fleet is the
owner's, but the customer bot needs to read it to pick a worker) and the
notification outbox the owner panel uses to reach customers.

The owner-only database is central_db.py and the CUSTOMER PROCESS NEVER IMPORTS
IT — isolation by architecture, not by permission check.

THE GOLDEN RULE OF THIS FILE
----------------------------
Every function that touches customer-owned data REQUIRES a customer_id and
raises ScopeError when it is missing. There is deliberately NO "return
everything" default anywhere: in a multi-tenant service, forgetting to pass the
customer id must produce a loud error, never a silent cross-customer leak.

The only unscoped readers are:
  * the worker fleet helpers          (fleet is global by nature)
  * the number-status cache           (a number being on Rubika is not customer data)
  * admin/maintenance helpers used exclusively by the owner process, each of
    which is named with an `owner_` prefix so it is obvious at the call site.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

import config

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "data", "customer.db")


class ScopeError(RuntimeError):
    """Raised when a customer-scoped query is attempted without a customer id.

    Seeing this in the logs is a BUG in the caller, and a good outcome: it means
    the guard stopped a query that would otherwise have crossed tenants.
    """


def _require_cid(customer_id) -> int:
    """Validate + normalise a customer id. Raises ScopeError when absent."""
    if customer_id is None or customer_id == "":
        raise ScopeError("customer_id is required (refusing an unscoped query)")
    try:
        cid = int(customer_id)
    except (TypeError, ValueError) as exc:
        raise ScopeError(f"customer_id must be an int, got {customer_id!r}") from exc
    if cid == 0:
        raise ScopeError("customer_id must not be 0")
    return cid


def _now() -> str:
    return config.now_str()


def _today() -> str:
    return config.today_str()


def _conn() -> sqlite3.Connection:
    """A short-lived connection. WAL + a long busy timeout, because the owner
    process and the customer process both write to this file."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _rows(cur) -> list:
    return [dict(r) for r in cur.fetchall()]


def _row(cur):
    r = cur.fetchone()
    return dict(r) if r else None


# =========================================================================== #
# Schema
# =========================================================================== #
SCHEMA_VERSION = 2


def _ensure_columns(cursor, table: str, columns: dict) -> None:
    """Add columns that a newer version expects but an older database lacks.

    `CREATE TABLE IF NOT EXISTS` silently skips an existing table, so a column
    introduced later never appears on a database created by an earlier build, and
    the first read of it crashes on startup. SQLite has no "ADD COLUMN IF NOT
    EXISTS", so the existing columns are read first.
    """
    have = {r[1] for r in cursor.execute(f"PRAGMA table_info({table})")}
    for name, spec in columns.items():
        if name not in have:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")


def init() -> None:
    conn = _conn()
    c = conn.cursor()

    # ---- schema bookkeeping: lets a future release migrate deterministically
    c.execute("""
        CREATE TABLE IF NOT EXISTS schema_meta (
            id      INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL
        )
    """)
    c.execute("INSERT OR IGNORE INTO schema_meta (id, version) VALUES (1, ?)",
              (SCHEMA_VERSION,))

    # ---- customers -------------------------------------------------------- #
    c.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            telegram_id INTEGER PRIMARY KEY,
            name        TEXT DEFAULT '',
            username    TEXT DEFAULT '',
            created_at  TEXT,
            expires_at  TEXT DEFAULT '',
            blocked     INTEGER DEFAULT 0,
            warned      INTEGER DEFAULT 0,
            total_sends INTEGER DEFAULT 0,
            note        TEXT DEFAULT '',
            last_seen   TEXT DEFAULT ''
        )
    """)

    # ---- Rubika accounts -------------------------------------------------- #
    # phone is unique PER CUSTOMER, not globally: two customers may legitimately
    # own the same number (bought/resold SIMs). Session files are namespaced per
    # customer on the worker so the two can never collide.
    c.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id  INTEGER NOT NULL,
            phone        TEXT NOT NULL,
            name         TEXT DEFAULT '',
            user_id      TEXT DEFAULT '',
            session      TEXT DEFAULT '',
            session_blob TEXT DEFAULT '',
            added_at     TEXT,
            status       TEXT DEFAULT 'active',
            worker_id    INTEGER,
            sent_total   INTEGER DEFAULT 0,
            contacts     INTEGER DEFAULT 0,
            UNIQUE (customer_id, phone)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_accounts_cid "
              "ON accounts(customer_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_accounts_worker "
              "ON accounts(worker_id)")

    # ---- Telegram accounts ------------------------------------------------ #
    c.execute("""
        CREATE TABLE IF NOT EXISTS tg_accounts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id   INTEGER NOT NULL,
            phone         TEXT NOT NULL,
            name          TEXT DEFAULT '',
            username      TEXT DEFAULT '',
            session       TEXT DEFAULT '',
            added_at      TEXT,
            status        TEXT DEFAULT 'active',
            contacts      INTEGER DEFAULT 0,
            mutuals       INTEGER DEFAULT 0,
            groups        INTEGER DEFAULT 0,
            sent_total    INTEGER DEFAULT 0,
            replied_total INTEGER DEFAULT 0,
            UNIQUE (customer_id, phone)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_tgacc_cid "
              "ON tg_accounts(customer_id)")

    # ---- per-customer settings (replaces the base project's single-row
    #      `settings` table and its flat global key/value store) ------------- #
    c.execute("""
        CREATE TABLE IF NOT EXISTS customer_settings (
            customer_id INTEGER NOT NULL,
            key         TEXT NOT NULL,
            value       TEXT,
            PRIMARY KEY (customer_id, key)
        )
    """)

    # ---- send content, per customer + platform ---------------------------- #
    # kind: 'rb_marker' | 'rb_text2' | 'rb_plain' are single values kept in
    # customer_settings; this table holds the ORDERED Telegram content list.
    c.execute("""
        CREATE TABLE IF NOT EXISTS tg_content (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            kind        TEXT DEFAULT 'text',
            text        TEXT DEFAULT '',
            file_path   TEXT DEFAULT '',
            file_name   TEXT DEFAULT '',
            added_at    TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_tgcontent_cid "
              "ON tg_content(customer_id)")

    # ---- anti-flood window (survives a restart, so it cannot be reset by
    #      forcing a crash) -------------------------------------------------- #
    c.execute("""
        CREATE TABLE IF NOT EXISTS rate_limit (
            customer_id  INTEGER PRIMARY KEY,
            window_start REAL DEFAULT 0,
            count        INTEGER DEFAULT 0
        )
    """)

    # ---- /start events, for the anti-spam shield --------------------------- #
    c.execute("""
        CREATE TABLE IF NOT EXISTS start_events (
            user_id INTEGER NOT NULL,
            ts      REAL NOT NULL
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_start_ts ON start_events(ts)")

    # ---- runtime state of the customer bot. The OWNER panel writes here to
    #      lift/lower the shield; the customer bot only reads it. This is
    #      operational state, not owner-secret data, so it lives in this DB. -- #
    c.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            id            INTEGER PRIMARY KEY CHECK (id = 1),
            online        INTEGER DEFAULT 1,
            offline_by    TEXT DEFAULT '',
            offline_at    TEXT DEFAULT '',
            offline_note  TEXT DEFAULT '',
            sends_frozen  INTEGER DEFAULT 0,
            frozen_at     TEXT DEFAULT '',
            -- the last health sweep, so the owner's panel (a different process)
            -- can show what the engine in the customer process found
            health_report TEXT DEFAULT '',
            health_at     TEXT DEFAULT ''
        )
    """)
    c.execute("INSERT OR IGNORE INTO bot_state (id, online) VALUES (1, 1)")
    # CREATE TABLE IF NOT EXISTS does nothing to a table that already exists, so
    # a column added in a later version never appears on an existing database and
    # every read of it crashes. Adding columns is the one migration this schema
    # actually needs, so it gets a helper rather than a hand-written ALTER each
    # time somebody forgets.
    _ensure_columns(c, "bot_state", {"health_report": "TEXT DEFAULT ''",
                                     "health_at": "TEXT DEFAULT ''"})

    # ---- anti-tamper clock ------------------------------------------------ #
    c.execute("""
        CREATE TABLE IF NOT EXISTS clock_state (
            id        INTEGER PRIMARY KEY CHECK (id = 1),
            last_seen REAL DEFAULT 0
        )
    """)
    c.execute("INSERT OR IGNORE INTO clock_state (id, last_seen) VALUES (1, 0)")

    # ---- daily usage counters (drive the probe budget + owner stats) ------- #
    c.execute("""
        CREATE TABLE IF NOT EXISTS usage_daily (
            customer_id INTEGER NOT NULL,
            day         TEXT NOT NULL,
            kind        TEXT NOT NULL,
            count       INTEGER DEFAULT 0,
            PRIMARY KEY (customer_id, day, kind)
        )
    """)

    # ---- the worker fleet (global: owned by the owner, read by everyone) --- #
    c.execute("""
        CREATE TABLE IF NOT EXISTS workers (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            tag          TEXT UNIQUE,
            ip           TEXT DEFAULT '',
            ssh_port     INTEGER DEFAULT 22,
            ssh_user     TEXT DEFAULT '',
            ssh_pass_enc TEXT DEFAULT '',
            api_port     INTEGER,
            api_token_enc TEXT DEFAULT '',
            is_master    INTEGER DEFAULT 0,
            enabled      INTEGER DEFAULT 1,
            status       TEXT DEFAULT 'unknown',
            ping_ms      INTEGER DEFAULT -1,
            file_ok      INTEGER DEFAULT 0,
            last_checked TEXT DEFAULT '',
            created_at   TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS worker_daily (
            worker_id INTEGER NOT NULL,
            day       TEXT NOT NULL,
            sent      INTEGER DEFAULT 0,
            PRIMARY KEY (worker_id, day)
        )
    """)
    # Round-robin pointer for login placement, persisted so a restart does not
    # send everyone back to worker #1.
    c.execute("""
        CREATE TABLE IF NOT EXISTS fleet_state (
            id      INTEGER PRIMARY KEY CHECK (id = 1),
            rr_next INTEGER DEFAULT 0
        )
    """)
    c.execute("INSERT OR IGNORE INTO fleet_state (id, rr_next) VALUES (1, 0)")

    # ---- outbox: how the OWNER panel reaches a customer. The owner bot cannot
    #      DM a customer (they never started it), so it queues here and the
    #      customer bot delivers. ------------------------------------------- #
    c.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            text        TEXT,
            sent        INTEGER DEFAULT 0,
            created_at  TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_notif_pending "
              "ON notifications(sent, id)")

    # ---- interrupted sends, so "continue" can resume instead of restarting
    #      from zero (restarting would re-message everybody). --------------- #
    c.execute("""
        CREATE TABLE IF NOT EXISTS paused_sends (
            account_id  INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            phone       TEXT,
            payload     TEXT,
            created_at  TEXT
        )
    """)

    # ---- anti-duplicate ledgers, scoped per customer ---------------------- #
    c.execute("""
        CREATE TABLE IF NOT EXISTS rb_sent (
            customer_id INTEGER NOT NULL,
            account_id  INTEGER NOT NULL,
            target      TEXT NOT NULL,
            sent_at     TEXT,
            PRIMARY KEY (customer_id, account_id, target)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS tg_sent (
            customer_id INTEGER NOT NULL,
            account_id  INTEGER NOT NULL,
            target      TEXT NOT NULL,
            sent_at     TEXT,
            PRIMARY KEY (customer_id, account_id, target)
        )
    """)

    # ---- number-status cache. GLOBAL on purpose: whether a phone number has a
    #      Rubika account is a property of the number, not of a customer, and
    #      sharing it means we never probe the same number twice across the
    #      whole service (less load on Rubika and on the accounts). It holds no
    #      customer-identifying data. ---------------------------------------- #
    c.execute("""
        CREATE TABLE IF NOT EXISTS number_status (
            phone      TEXT PRIMARY KEY,
            on_rubika  INTEGER DEFAULT 0,
            checked_at TEXT
        )
    """)

    # ---- persisted contact-build / discovery jobs (restart-safe) ---------- #
    c.execute("""
        CREATE TABLE IF NOT EXISTS contact_jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            account_id  INTEGER NOT NULL,
            phone       TEXT,
            kind        TEXT DEFAULT 'import',
            status      TEXT DEFAULT 'running',
            payload     TEXT,
            cursor      INTEGER DEFAULT 0,
            added       INTEGER DEFAULT 0,
            failed      INTEGER DEFAULT 0,
            created_at  TEXT,
            updated_at  TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_cjobs_cid "
              "ON contact_jobs(customer_id, status)")

    # ---- Pool brain: several accounts probing ONE number space in parallel -- #
    # The suffix space is walked as an affine permutation rather than randomly,
    # so leasing disjoint index ranges yields disjoint phone numbers with no
    # collision checks at all. Random generation across parallel accounts would
    # mean every account re-probing numbers another one already burned.
    c.execute("""
        CREATE TABLE IF NOT EXISTS pool_jobs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id   INTEGER NOT NULL,
            prefix        TEXT,
            target        INTEGER,
            suffix_width  INTEGER,
            affine_a      INTEGER,
            affine_offset INTEGER,
            cursor        INTEGER DEFAULT 0,
            mode          TEXT DEFAULT 'text',
            content       TEXT DEFAULT '',
            status        TEXT DEFAULT 'leeching',
            probed        INTEGER DEFAULT 0,
            -- why leeching stopped: a fact about the job, since every account
            -- hits the budget or the end of the space at the same moment
            halt_reason   TEXT DEFAULT '',
            created_at    TEXT,
            updated_at    TEXT
        )
    """)
    _ensure_columns(c, "pool_jobs", {"halt_reason": "TEXT DEFAULT ''"})
    c.execute("CREATE INDEX IF NOT EXISTS idx_pool_cid "
              "ON pool_jobs(customer_id, status)")
    c.execute("""
        CREATE TABLE IF NOT EXISTS pool_job_accounts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id      INTEGER NOT NULL,
            customer_id INTEGER NOT NULL,
            account_id  INTEGER NOT NULL,
            phone       TEXT,
            found       INTEGER DEFAULT 0,
            sent        INTEGER DEFAULT 0,
            status      TEXT DEFAULT 'active',
            note        TEXT DEFAULT '',
            UNIQUE(job_id, account_id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS pool_contacts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id      INTEGER NOT NULL,
            customer_id INTEGER NOT NULL,
            account_id  INTEGER NOT NULL,
            phone       TEXT,
            guid        TEXT,
            sent        INTEGER DEFAULT 0,
            UNIQUE(job_id, guid)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_pool_contacts "
              "ON pool_contacts(job_id, account_id, sent)")

    # ---- Tabchi (group engine) + the Secretary that lives inside it -------- #
    c.execute("""
        CREATE TABLE IF NOT EXISTS tabchi (
            account_id   INTEGER PRIMARY KEY,
            customer_id  INTEGER NOT NULL,
            enabled      INTEGER DEFAULT 0,
            interval_sec INTEGER DEFAULT 1800,
            sent_total   INTEGER DEFAULT 0,
            last_run     TEXT DEFAULT '',
            updated_at   TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS tabchi_texts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            account_id  INTEGER NOT NULL,
            text        TEXT,
            added_at    TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_tabtexts "
              "ON tabchi_texts(customer_id, account_id)")
    # Group links are PER ACCOUNT and PER CUSTOMER. There is deliberately no
    # shared "verified links" pool: in a multi-tenant service that would push
    # one customer's hard-won groups onto every other customer's accounts, and
    # get everybody banned in the same groups.
    c.execute("""
        CREATE TABLE IF NOT EXISTS tabchi_groups (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            account_id  INTEGER NOT NULL,
            link        TEXT,
            guid        TEXT DEFAULT '',
            name        TEXT DEFAULT '',
            joined      INTEGER DEFAULT 0,
            fails       INTEGER DEFAULT 0,
            muted       INTEGER DEFAULT 0,
            added_at    TEXT,
            UNIQUE (customer_id, account_id, link)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS secretary (
            account_id   INTEGER PRIMARY KEY,
            customer_id  INTEGER NOT NULL,
            enabled      INTEGER DEFAULT 0,
            mode         TEXT DEFAULT 'text',
            text         TEXT DEFAULT '',
            interval_sec INTEGER DEFAULT 600,
            replied_total INTEGER DEFAULT 0,
            updated_at   TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS secretary_replied (
            customer_id INTEGER NOT NULL,
            account_id  INTEGER NOT NULL,
            target      TEXT NOT NULL,
            replied_at  TEXT,
            PRIMARY KEY (customer_id, account_id, target)
        )
    """)

    # ---- Telegram multi-account send jobs ---------------------------------- #
    # Persisted so a restart resumes instead of starting over. Starting over
    # would message everybody a second time, which is what gets accounts
    # reported and banned.
    c.execute("""
        CREATE TABLE IF NOT EXISTS tg_multi_jobs (
            job_id       TEXT PRIMARY KEY,
            customer_id  INTEGER NOT NULL,
            state        TEXT NOT NULL DEFAULT 'queued',
            content_json TEXT DEFAULT '[]',
            delay        REAL DEFAULT 0.2,
            target_mode  TEXT DEFAULT 'both',
            total        INTEGER DEFAULT 0,
            mutual_total INTEGER DEFAULT 0,
            sent_count   INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0,
            skipped_count INTEGER DEFAULT 0,
            current_phone TEXT DEFAULT '',
            stop_requested INTEGER DEFAULT 0,
            last_error   TEXT DEFAULT '',
            msg_id       INTEGER,
            created_at   TEXT,
            updated_at   TEXT,
            finished_at  TEXT DEFAULT ''
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_tgm_jobs "
              "ON tg_multi_jobs(customer_id, state)")
    c.execute("""
        CREATE TABLE IF NOT EXISTS tg_multi_accounts (
            job_id       TEXT NOT NULL,
            customer_id  INTEGER NOT NULL,
            account_id   INTEGER NOT NULL,
            phone        TEXT NOT NULL,
            ordinal      INTEGER DEFAULT 0,
            state        TEXT DEFAULT 'pending',
            total        INTEGER DEFAULT 0,
            sent_count   INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0,
            consec_fail  INTEGER DEFAULT 0,
            last_error   TEXT DEFAULT '',
            PRIMARY KEY (job_id, account_id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS tg_multi_recipients (
            job_id      TEXT NOT NULL,
            idx         INTEGER NOT NULL,
            customer_id INTEGER NOT NULL,
            account_id  INTEGER NOT NULL,
            target_key  TEXT NOT NULL,
            target_json TEXT DEFAULT '',
            mutual      INTEGER DEFAULT 0,
            state       TEXT DEFAULT 'pending',
            attempts    INTEGER DEFAULT 0,
            last_error  TEXT DEFAULT '',
            PRIMARY KEY (job_id, idx)
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_tgm_recip "
              "ON tg_multi_recipients(job_id, account_id, state, idx)")
    # Cross-account anti-duplicate WITHIN one job: once any account has reached a
    # person, a later account in the same job skips them. Without it a customer
    # with five accounts messages every shared contact five times.
    c.execute("""
        CREATE TABLE IF NOT EXISTS tg_multi_sent (
            job_id TEXT NOT NULL,
            uid    TEXT NOT NULL,
            PRIMARY KEY (job_id, uid)
        )
    """)

    # ---- support tickets --------------------------------------------------- #
    # These live in the CUSTOMER database, not the owner's one: a ticket is a
    # message between a customer and the owner, not owner-secret state. Putting
    # them here is what lets the customer bot file one at all — that process
    # deliberately cannot open central_db.
    c.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            text        TEXT,
            answered    INTEGER DEFAULT 0,
            answer      TEXT DEFAULT '',
            created_at  TEXT,
            answered_at TEXT DEFAULT ''
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_tickets_open "
              "ON tickets(answered, id)")

    conn.commit()
    conn.close()


def schema_version() -> int:
    conn = _conn()
    row = _row(conn.execute("SELECT version FROM schema_meta WHERE id = 1"))
    conn.close()
    return int(row["version"]) if row else 0


# =========================================================================== #
# Anti-tamper clock
# =========================================================================== #
def monotonic_now() -> float:
    """Epoch seconds that never go backwards.

    Subscriptions are time-based, so the cheapest attack (or the commonest
    server misconfiguration) is a clock rewind. We persist the highest epoch we
    have ever seen; if the wall clock is behind it we keep returning the stored
    value instead.
    """
    wall = time.time()
    conn = _conn()
    row = _row(conn.execute("SELECT last_seen FROM clock_state WHERE id = 1"))
    last = float(row["last_seen"]) if row else 0.0
    effective = wall if wall >= last else last
    if effective > last:
        conn.execute("UPDATE clock_state SET last_seen = ? WHERE id = 1",
                     (effective,))
        conn.commit()
    conn.close()
    return effective


def clock_tampered() -> bool:
    """True when the wall clock currently sits behind the highest seen time."""
    wall = time.time()
    conn = _conn()
    row = _row(conn.execute("SELECT last_seen FROM clock_state WHERE id = 1"))
    conn.close()
    last = float(row["last_seen"]) if row else 0.0
    return wall < last - config.CLOCK_BACKWARD_TOLERANCE


# =========================================================================== #
# Customers / subscription
# =========================================================================== #
def ensure_customer(telegram_id, name: str = "", username: str = "") -> dict:
    """Create the customer row on first contact (idempotent) and return it.

    A brand-new customer is granted TRIAL_DAYS of access. An existing customer
    keeps whatever expiry they already have.
    """
    cid = _require_cid(telegram_id)
    conn = _conn()
    c = conn.cursor()
    existing = _row(c.execute(
        "SELECT telegram_id FROM customers WHERE telegram_id = ?", (cid,)))
    if not existing:
        expires = ""
        if config.TRIAL_DAYS > 0:
            expires = _iso_after_days(config.TRIAL_DAYS)
        c.execute(
            "INSERT INTO customers (telegram_id, name, username, created_at, "
            "expires_at, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
            (cid, name or "", username or "", _now(), expires, _now()))
    else:
        c.execute("UPDATE customers SET name = ?, username = ?, last_seen = ? "
                  "WHERE telegram_id = ?",
                  (name or "", username or "", _now(), cid))
    conn.commit()
    row = _row(c.execute("SELECT * FROM customers WHERE telegram_id = ?", (cid,)))
    conn.close()
    return row or {}


def _iso_after_days(days: float) -> str:
    from datetime import timedelta
    return (config.now_dt() + timedelta(days=float(days))
            ).strftime("%Y-%m-%d %H:%M:%S")


def _parse_iso(text: str):
    from datetime import datetime
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def get_customer(telegram_id) -> dict | None:
    cid = _require_cid(telegram_id)
    conn = _conn()
    row = _row(conn.execute("SELECT * FROM customers WHERE telegram_id = ?", (cid,)))
    conn.close()
    return row


def touch_customer(telegram_id) -> None:
    cid = _require_cid(telegram_id)
    conn = _conn()
    conn.execute("UPDATE customers SET last_seen = ? WHERE telegram_id = ?",
                 (_now(), cid))
    conn.commit()
    conn.close()


def seconds_left(telegram_id) -> float:
    """Seconds of access remaining. 0 when expired, and a large number when the
    customer has no expiry set at all (an owner-granted unlimited account)."""
    cust = get_customer(telegram_id)
    if not cust:
        return 0.0
    raw = (cust.get("expires_at") or "").strip()
    if not raw:
        return 0.0
    when = _parse_iso(raw)
    if not when:
        return 0.0
    tz = config.now_dt().tzinfo
    if tz is not None and when.tzinfo is None:
        when = when.replace(tzinfo=tz)
    return max(0.0, (when - config.now_dt()).total_seconds())


def days_left(telegram_id) -> int:
    return int(seconds_left(telegram_id) // 86400)


def is_blocked(telegram_id) -> bool:
    cust = get_customer(telegram_id)
    return bool(cust and cust.get("blocked"))


def is_active(telegram_id) -> bool:
    """True when the customer may use the service right now."""
    cust = get_customer(telegram_id)
    if not cust or cust.get("blocked"):
        return False
    return seconds_left(telegram_id) > 0


def set_blocked(telegram_id, blocked: bool) -> None:
    cid = _require_cid(telegram_id)
    conn = _conn()
    conn.execute("UPDATE customers SET blocked = ? WHERE telegram_id = ?",
                 (1 if blocked else 0, cid))
    conn.commit()
    conn.close()


def add_days(telegram_id, days: float) -> str:
    """Extend (or reduce, with a negative value) a customer's access.

    Extension is measured from *now* when the subscription has already lapsed,
    and from the current expiry when it is still running — so topping up early
    never loses the remaining days.
    """
    cid = _require_cid(telegram_id)
    cust = get_customer(cid) or {}
    base = _parse_iso(cust.get("expires_at") or "")
    now_dt = config.now_dt().replace(tzinfo=None)
    if base is None or base < now_dt:
        base = now_dt
    from datetime import timedelta
    new = base + timedelta(days=float(days))
    value = new.strftime("%Y-%m-%d %H:%M:%S")
    conn = _conn()
    conn.execute("UPDATE customers SET expires_at = ?, warned = 0 "
                 "WHERE telegram_id = ?", (value, cid))
    conn.commit()
    conn.close()
    return value


def set_expiry(telegram_id, value: str) -> None:
    cid = _require_cid(telegram_id)
    conn = _conn()
    conn.execute("UPDATE customers SET expires_at = ?, warned = 0 "
                 "WHERE telegram_id = ?", (value or "", cid))
    conn.commit()
    conn.close()


def set_note(telegram_id, note: str) -> None:
    cid = _require_cid(telegram_id)
    conn = _conn()
    conn.execute("UPDATE customers SET note = ? WHERE telegram_id = ?",
                 (note or "", cid))
    conn.commit()
    conn.close()


def set_warned(telegram_id, warned: bool = True) -> None:
    cid = _require_cid(telegram_id)
    conn = _conn()
    conn.execute("UPDATE customers SET warned = ? WHERE telegram_id = ?",
                 (1 if warned else 0, cid))
    conn.commit()
    conn.close()


def incr_customer_sends(telegram_id, n: int = 1) -> None:
    cid = _require_cid(telegram_id)
    conn = _conn()
    conn.execute("UPDATE customers SET total_sends = total_sends + ? "
                 "WHERE telegram_id = ?", (int(n), cid))
    conn.commit()
    conn.close()


def delete_customer(telegram_id) -> None:
    """Remove a customer and everything they own."""
    cid = _require_cid(telegram_id)
    conn = _conn()
    c = conn.cursor()
    # Derived from the schema rather than hand-listed. A hand-written list has to
    # be updated by whoever adds a feature, and the one time it is forgotten the
    # deleted customer's rows stay behind forever — invisible, because nothing
    # reads them. Asking sqlite which tables carry a customer_id cannot be
    # forgotten.
    for table in _customer_scoped_tables(c):
        c.execute(f"DELETE FROM {table} WHERE customer_id = ?", (cid,))  # noqa: S608
    c.execute("DELETE FROM customers WHERE telegram_id = ?", (cid,))
    conn.commit()
    conn.close()


def _customer_scoped_tables(cursor) -> list:
    """Every table with a customer_id column."""
    names = [r[0] for r in cursor.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%'")]
    scoped = []
    for name in names:
        columns = {r[1] for r in cursor.execute(f"PRAGMA table_info({name})")}
        if "customer_id" in columns:
            scoped.append(name)
    return scoped


# ---- owner-only readers (unscoped BY DESIGN, hence the owner_ prefix) ------ #
def owner_list_customers(order: str = "created") -> list:
    orders = {
        "created": "created_at DESC",
        "sends": "total_sends DESC",
        "name": "name COLLATE NOCASE ASC",
        "expiry": "expires_at ASC",
    }
    conn = _conn()
    rows = _rows(conn.execute(
        f"SELECT * FROM customers ORDER BY {orders.get(order, orders['created'])}"))
    conn.close()
    return rows


def owner_count_customers() -> dict:
    """Totals for the owner dashboard: active / expired / blocked."""
    out = {"total": 0, "active": 0, "expired": 0, "blocked": 0}
    for cust in owner_list_customers():
        out["total"] += 1
        if cust.get("blocked"):
            out["blocked"] += 1
        elif seconds_left(cust["telegram_id"]) > 0:
            out["active"] += 1
        else:
            out["expired"] += 1
    return out


def owner_search_customers(term: str) -> list:
    """Find customers by id, username, name — or by one of their account phones."""
    term = (term or "").strip().lstrip("@")
    if not term:
        return []
    like = f"%{term}%"
    digits = "".join(ch for ch in term if ch.isdigit())
    conn = _conn()
    sql = ("SELECT * FROM customers WHERE CAST(telegram_id AS TEXT) LIKE ? "
           "OR username LIKE ? OR name LIKE ?")
    params = [like, like, like]
    if digits:
        sql += (" OR telegram_id IN (SELECT customer_id FROM accounts "
                "WHERE phone LIKE ?)"
                " OR telegram_id IN (SELECT customer_id FROM tg_accounts "
                "WHERE phone LIKE ?)")
        params += [f"%{digits}%", f"%{digits}%"]
    rows = _rows(conn.execute(sql + " ORDER BY total_sends DESC", params))
    conn.close()
    return rows


def owner_customers_expiring(days: int = 2) -> list:
    """Active customers whose access ends within `days` and who were not warned."""
    out = []
    for cust in owner_list_customers():
        if cust.get("blocked") or cust.get("warned"):
            continue
        left = seconds_left(cust["telegram_id"])
        if 0 < left <= days * 86400:
            out.append(cust)
    return out


def owner_customers_idle(days: int = 10) -> list:
    """Customers with no activity for `days` — the churn-warning list."""
    cutoff = config.now_dt().replace(tzinfo=None)
    from datetime import timedelta
    cutoff = cutoff - timedelta(days=int(days))
    out = []
    for cust in owner_list_customers():
        if cust.get("blocked"):
            continue
        seen = _parse_iso(cust.get("last_seen") or "")
        if seen and seen < cutoff:
            out.append(cust)
    return out



# =========================================================================== #
# Rubika accounts — every reader/writer is customer-scoped
# =========================================================================== #
def add_account(customer_id, phone: str, name: str = "", user_id: str = "",
                session: str = "", worker_id=None) -> int:
    cid = _require_cid(customer_id)
    conn = _conn()
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO accounts (customer_id, phone, name, user_id, "
        "session, added_at, status, worker_id) VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
        (cid, phone, name or "", str(user_id or ""), session or "", _now(),
         worker_id))
    c.execute(
        "UPDATE accounts SET name = ?, user_id = ?, status = 'active' "
        "WHERE customer_id = ? AND phone = ?",
        (name or "", str(user_id or ""), cid, phone))
    conn.commit()
    row = _row(c.execute(
        "SELECT id FROM accounts WHERE customer_id = ? AND phone = ?", (cid, phone)))
    conn.close()
    return int(row["id"]) if row else 0


def list_accounts(customer_id) -> list:
    """Every Rubika account of ONE customer. There is no unscoped variant."""
    cid = _require_cid(customer_id)
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT * FROM accounts WHERE customer_id = ? ORDER BY id", (cid,)))
    conn.close()
    return rows


def get_account(customer_id, account_id) -> dict | None:
    """Fetch one account, PROVING it belongs to this customer.

    Callers hand us the id straight out of a button payload, so the ownership
    check has to happen here — this is the function that stops customer A from
    poking at customer B's account by replaying a callback.
    """
    cid = _require_cid(customer_id)
    conn = _conn()
    row = _row(conn.execute(
        "SELECT * FROM accounts WHERE id = ? AND customer_id = ?",
        (int(account_id), cid)))
    conn.close()
    return row


def get_account_by_phone(customer_id, phone: str) -> dict | None:
    cid = _require_cid(customer_id)
    conn = _conn()
    row = _row(conn.execute(
        "SELECT * FROM accounts WHERE customer_id = ? AND phone = ?", (cid, phone)))
    conn.close()
    return row


def owner_all_accounts(status: str = "active", platform: str = "rb") -> list:
    """Every account of every customer — for the health engine only.

    Deliberately named `owner_` and deliberately the only unscoped reader of the
    accounts table. The health engine is a service-wide sweep, so it genuinely
    has no single customer to scope to; every other caller must go through
    list_accounts(customer_id).
    """
    table = "tg_accounts" if platform == "tg" else "accounts"
    conn = _conn()
    sql = f"SELECT * FROM {table}"                       # noqa: S608 - fixed set
    args: tuple = ()
    if status:
        sql += " WHERE status = ?"
        args = (status,)
    rows = _rows(conn.execute(sql + " ORDER BY customer_id, id", args))
    conn.close()
    return rows


def count_accounts(customer_id) -> dict:
    cid = _require_cid(customer_id)
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT status, COUNT(*) AS n FROM accounts WHERE customer_id = ? "
        "GROUP BY status", (cid,)))
    conn.close()
    total = sum(r["n"] for r in rows)
    healthy = sum(r["n"] for r in rows if r["status"] == "active")
    return {"total": total, "healthy": healthy, "dead": total - healthy}


def set_status(customer_id, account_id, status: str) -> None:
    cid = _require_cid(customer_id)
    conn = _conn()
    conn.execute("UPDATE accounts SET status = ? WHERE id = ? AND customer_id = ?",
                 (status, int(account_id), cid))
    conn.commit()
    conn.close()


def set_account_worker(customer_id, account_id, worker_id) -> None:
    cid = _require_cid(customer_id)
    conn = _conn()
    conn.execute("UPDATE accounts SET worker_id = ? WHERE id = ? AND customer_id = ?",
                 (worker_id, int(account_id), cid))
    conn.commit()
    conn.close()


def incr_account_sent(customer_id, account_id, n: int = 1) -> None:
    cid = _require_cid(customer_id)
    conn = _conn()
    conn.execute("UPDATE accounts SET sent_total = sent_total + ? "
                 "WHERE id = ? AND customer_id = ?", (int(n), int(account_id), cid))
    conn.commit()
    conn.close()


def set_account_contacts(customer_id, account_id, contacts: int) -> None:
    cid = _require_cid(customer_id)
    conn = _conn()
    conn.execute("UPDATE accounts SET contacts = ? WHERE id = ? AND customer_id = ?",
                 (int(contacts), int(account_id), cid))
    conn.commit()
    conn.close()


def delete_account(customer_id, account_id) -> None:
    cid = _require_cid(customer_id)
    aid = int(account_id)
    conn = _conn()
    c = conn.cursor()
    c.execute("DELETE FROM accounts WHERE id = ? AND customer_id = ?", (aid, cid))
    # Same reasoning as delete_customer: derived from the schema, because the
    # hand-written version of this list had already fallen behind the code. Note
    # the tg_* tables key account_id against a DIFFERENT table (tg_accounts), so
    # they are excluded — a Rubika account id must not delete a Telegram one.
    for table in _account_scoped_tables(c):
        c.execute(f"DELETE FROM {table} WHERE account_id = ? "        # noqa: S608
                  "AND customer_id = ?", (aid, cid))
    conn.commit()
    conn.close()


def _account_scoped_tables(cursor) -> list:
    """Tables holding rows that belong to ONE Rubika account."""
    scoped = []
    for name in _customer_scoped_tables(cursor):
        if name.startswith("tg_") or name == "accounts":
            continue
        columns = {r[1] for r in cursor.execute(f"PRAGMA table_info({name})")}
        if "account_id" in columns:
            scoped.append(name)
    return scoped


# ---- portable session blob (encrypted at rest) ---------------------------- #
def session_pack(values: dict) -> str:
    """Serialise the 5 portable session values into a copyable token."""
    import base64
    raw = json.dumps(values, ensure_ascii=False).encode("utf-8")
    return "MMSESS:" + base64.urlsafe_b64encode(raw).decode("ascii")


def session_unpack(token: str) -> dict | None:
    import base64
    token = (token or "").strip()
    for prefix in ("MMSESS:", "YDSESS:"):     # accept the base project's prefix
        if token.startswith(prefix):
            token = token[len(prefix):]
            break
    else:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def set_session_blob(customer_id, account_id, values: dict) -> None:
    cid = _require_cid(customer_id)
    if not values:
        return
    try:
        import crypto_util
        stored = crypto_util.encrypt(json.dumps(values, ensure_ascii=False))
    except Exception:
        stored = session_pack(values)
    conn = _conn()
    conn.execute("UPDATE accounts SET session_blob = ? "
                 "WHERE id = ? AND customer_id = ?",
                 (stored, int(account_id), cid))
    conn.commit()
    conn.close()


def get_session_blob(customer_id, account_id) -> dict | None:
    acc = get_account(customer_id, account_id)
    if not acc:
        return None
    stored = (acc.get("session_blob") or "").strip()
    if not stored:
        return None
    try:
        import crypto_util
        return json.loads(crypto_util.decrypt(stored))
    except Exception:
        return session_unpack(stored)


# =========================================================================== #
# Telegram accounts
# =========================================================================== #
def tg_add_account(customer_id, phone: str, name: str = "", username: str = "",
                   session: str = "", **stats) -> int:
    cid = _require_cid(customer_id)
    conn = _conn()
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO tg_accounts (customer_id, phone, name, username, "
        "session, added_at, status) VALUES (?, ?, ?, ?, ?, ?, 'active')",
        (cid, phone, name or "", username or "", session or "", _now()))
    c.execute(
        "UPDATE tg_accounts SET name = ?, username = ?, session = ?, "
        "status = 'active', contacts = ?, mutuals = ?, groups = ? "
        "WHERE customer_id = ? AND phone = ?",
        (name or "", username or "", session or "",
         int(stats.get("contacts", 0)), int(stats.get("mutuals", 0)),
         int(stats.get("groups", 0)), cid, phone))
    conn.commit()
    row = _row(c.execute(
        "SELECT id FROM tg_accounts WHERE customer_id = ? AND phone = ?",
        (cid, phone)))
    conn.close()
    return int(row["id"]) if row else 0


def tg_list_accounts(customer_id) -> list:
    cid = _require_cid(customer_id)
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT * FROM tg_accounts WHERE customer_id = ? ORDER BY id", (cid,)))
    conn.close()
    return rows


def tg_get_account(customer_id, account_id) -> dict | None:
    cid = _require_cid(customer_id)
    conn = _conn()
    row = _row(conn.execute(
        "SELECT * FROM tg_accounts WHERE id = ? AND customer_id = ?",
        (int(account_id), cid)))
    conn.close()
    return row


def tg_get_by_phone(customer_id, phone: str) -> dict | None:
    cid = _require_cid(customer_id)
    conn = _conn()
    row = _row(conn.execute(
        "SELECT * FROM tg_accounts WHERE customer_id = ? AND phone = ?",
        (cid, phone)))
    conn.close()
    return row


def tg_count_accounts(customer_id) -> dict:
    cid = _require_cid(customer_id)
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT status, COUNT(*) AS n FROM tg_accounts WHERE customer_id = ? "
        "GROUP BY status", (cid,)))
    conn.close()
    total = sum(r["n"] for r in rows)
    healthy = sum(r["n"] for r in rows if r["status"] == "active")
    return {"total": total, "healthy": healthy, "dead": total - healthy}


def tg_set_status(customer_id, account_id, status: str) -> None:
    cid = _require_cid(customer_id)
    conn = _conn()
    conn.execute("UPDATE tg_accounts SET status = ? WHERE id = ? AND customer_id = ?",
                 (status, int(account_id), cid))
    conn.commit()
    conn.close()


def tg_incr_sent(customer_id, account_id, n: int = 1) -> None:
    cid = _require_cid(customer_id)
    conn = _conn()
    conn.execute("UPDATE tg_accounts SET sent_total = sent_total + ? "
                 "WHERE id = ? AND customer_id = ?", (int(n), int(account_id), cid))
    conn.commit()
    conn.close()


def tg_set_session(customer_id, account_id, session: str) -> None:
    """Store the (already encrypted) StringSession for a Telegram account."""
    cid = _require_cid(customer_id)
    conn = _conn()
    conn.execute("UPDATE tg_accounts SET session = ? WHERE id = ? AND customer_id = ?",
                 (session or "", int(account_id), cid))
    conn.commit()
    conn.close()


def tg_set_stats(customer_id, account_id, **stats) -> None:
    cid = _require_cid(customer_id)
    allowed = {"contacts", "mutuals", "groups"}
    sets, params = [], []
    for key, value in stats.items():
        if key in allowed:
            sets.append(f"{key} = ?")
            params.append(int(value or 0))
    if not sets:
        return
    params += [int(account_id), cid]
    conn = _conn()
    conn.execute(f"UPDATE tg_accounts SET {', '.join(sets)} "
                 "WHERE id = ? AND customer_id = ?", params)
    conn.commit()
    conn.close()


def tg_delete_account(customer_id, account_id) -> None:
    cid = _require_cid(customer_id)
    aid = int(account_id)
    conn = _conn()
    c = conn.cursor()
    c.execute("DELETE FROM tg_accounts WHERE id = ? AND customer_id = ?", (aid, cid))
    c.execute("DELETE FROM tg_sent WHERE account_id = ? AND customer_id = ?",
              (aid, cid))
    c.execute("DELETE FROM tg_multi_accounts WHERE account_id = ? "
              "AND customer_id = ?", (aid, cid))
    conn.commit()
    conn.close()


# =========================================================================== #
# Per-customer settings (the multi-tenant replacement for the base project's
# single-row settings table — that one made every customer share one send text)
# =========================================================================== #
_DEFAULTS = {
    "rb_marker": lambda: config.FORWARD_MARKER,
    "rb_text2": lambda: "",
    "rb_plain": lambda: "",
    "send_delay": lambda: config.DEFAULT_DELAY,
    "contact_delay": lambda: config.CONTACT_ADD_DELAY,
    "max_errors": lambda: config.MAX_ERRORS,
    "resume_wait": lambda: config.RESUME_WAIT,
    "brain_cap": lambda: config.BRAIN_SEND_CAP,
    "discovery_target": lambda: config.DISCOVERY_TARGET,
    "discovery_attempts": lambda: config.DISCOVERY_MAX_ATTEMPTS,
    "discovery_delay": lambda: config.DISCOVERY_PROBE_DELAY,
    "campaign_enabled": lambda: "0",
    "tg_send_delay": lambda: config.TG_SEND_DELAY,
    "tg_target": lambda: "both",
    "tg_delete_after": lambda: "0",
    "pv_mode": lambda: config.PV_EXPORT_MODE_DEFAULT,
    "pv_parallel": lambda: config.PV_EXPORT_PARALLEL,
}


def get_setting(customer_id, key: str, default=None):
    cid = _require_cid(customer_id)
    conn = _conn()
    row = _row(conn.execute(
        "SELECT value FROM customer_settings WHERE customer_id = ? AND key = ?",
        (cid, key)))
    conn.close()
    if row is not None:
        return row["value"]
    if default is not None:
        return default
    maker = _DEFAULTS.get(key)
    return maker() if maker else None


def set_setting(customer_id, key: str, value) -> None:
    cid = _require_cid(customer_id)
    conn = _conn()
    conn.execute(
        "INSERT INTO customer_settings (customer_id, key, value) VALUES (?, ?, ?) "
        "ON CONFLICT(customer_id, key) DO UPDATE SET value = excluded.value",
        (cid, key, "" if value is None else str(value)))
    conn.commit()
    conn.close()


def get_float_setting(customer_id, key: str, default: float = 0.0) -> float:
    try:
        return float(get_setting(customer_id, key, default))
    except (TypeError, ValueError):
        return float(default)


def get_int_setting(customer_id, key: str, default: int = 0) -> int:
    try:
        return int(float(get_setting(customer_id, key, default)))
    except (TypeError, ValueError):
        return int(default)


def get_bool_setting(customer_id, key: str, default: bool = False) -> bool:
    raw = get_setting(customer_id, key, "1" if default else "0")
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


# convenience wrappers used all over the panels
def get_marker(customer_id) -> str:
    return get_setting(customer_id, "rb_marker") or config.FORWARD_MARKER


def get_delay(customer_id) -> float:
    return config.clamp_delay(get_float_setting(
        customer_id, "send_delay", config.DEFAULT_DELAY))


def get_contact_delay(customer_id) -> float:
    return config.clamp_contact_delay(get_float_setting(
        customer_id, "contact_delay", config.CONTACT_ADD_DELAY))


def get_max_errors(customer_id) -> int:
    return get_int_setting(customer_id, "max_errors", config.MAX_ERRORS)


def get_brain_cap(customer_id) -> int:
    return get_int_setting(customer_id, "brain_cap", config.BRAIN_SEND_CAP)


def get_discovery_target(customer_id) -> int:
    return get_int_setting(customer_id, "discovery_target", config.DISCOVERY_TARGET)


def copy_settings_to_all_accounts(customer_id, keys: list) -> int:
    """No-op placeholder kept for symmetry: per-customer settings already apply
    to every one of that customer's accounts, which is exactly what the
    "apply to all accounts" button promises for global values. Per-ACCOUNT
    values (tabchi text/interval) are copied by tabchi.apply_to_all."""
    _require_cid(customer_id)
    return len(keys or [])


# =========================================================================== #
# Telegram send content (ordered list: texts and media)
# =========================================================================== #
def tg_content_add(customer_id, kind: str, text: str = "", file_path: str = "",
                   file_name: str = "") -> int:
    cid = _require_cid(customer_id)
    conn = _conn()
    c = conn.cursor()
    c.execute("INSERT INTO tg_content (customer_id, kind, text, file_path, "
              "file_name, added_at) VALUES (?, ?, ?, ?, ?, ?)",
              (cid, kind or "text", text or "", file_path or "", file_name or "",
               _now()))
    conn.commit()
    new_id = c.lastrowid
    conn.close()
    return int(new_id)


def tg_content_list(customer_id) -> list:
    cid = _require_cid(customer_id)
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT * FROM tg_content WHERE customer_id = ? ORDER BY id", (cid,)))
    conn.close()
    return rows


def tg_content_clear(customer_id) -> int:
    cid = _require_cid(customer_id)
    conn = _conn()
    c = conn.cursor()
    c.execute("SELECT file_path FROM tg_content WHERE customer_id = ? "
              "AND file_path != ''", (cid,))
    paths = [r["file_path"] for r in c.fetchall()]
    c.execute("DELETE FROM tg_content WHERE customer_id = ?", (cid,))
    conn.commit()
    conn.close()
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    return len(paths)


# =========================================================================== #
# Rate limit (window persisted, so a restart cannot clear it)
# =========================================================================== #
def rate_hit(customer_id) -> tuple[bool, int]:
    """Record one rate-limited action. Returns (allowed, count_in_window)."""
    cid = _require_cid(customer_id)
    now = time.time()
    window = float(config.RATE_LIMIT_WINDOW)
    conn = _conn()
    c = conn.cursor()
    row = _row(c.execute(
        "SELECT window_start, count FROM rate_limit WHERE customer_id = ?", (cid,)))
    if not row or (now - float(row["window_start"])) > window:
        c.execute(
            "INSERT INTO rate_limit (customer_id, window_start, count) "
            "VALUES (?, ?, 1) ON CONFLICT(customer_id) DO UPDATE SET "
            "window_start = excluded.window_start, count = 1",
            (cid, now))
        conn.commit()
        conn.close()
        return True, 1
    count = int(row["count"]) + 1
    c.execute("UPDATE rate_limit SET count = ? WHERE customer_id = ?", (count, cid))
    conn.commit()
    conn.close()
    return count <= config.RATE_LIMIT_MAX, count


def rate_reset(customer_id) -> None:
    cid = _require_cid(customer_id)
    conn = _conn()
    conn.execute("DELETE FROM rate_limit WHERE customer_id = ?", (cid,))
    conn.commit()
    conn.close()


# =========================================================================== #
# /start flood tracking (feeds the anti-spam shield)
# =========================================================================== #
def record_start(user_id) -> int:
    """Log a /start and return how many DISTINCT users started inside the
    configured window. Distinct, because one curious person tapping /start ten
    times is not an attack — a hundred fresh accounts are."""
    now = time.time()
    conn = _conn()
    c = conn.cursor()
    c.execute("INSERT INTO start_events (user_id, ts) VALUES (?, ?)",
              (int(user_id), now))
    cutoff = now - float(config.START_FLOOD_WINDOW)
    c.execute("DELETE FROM start_events WHERE ts < ?", (now - 86400,))
    row = _row(c.execute(
        "SELECT COUNT(DISTINCT user_id) AS n FROM start_events WHERE ts >= ?",
        (cutoff,)))
    conn.commit()
    conn.close()
    return int(row["n"]) if row else 0


def recent_start_count() -> int:
    cutoff = time.time() - float(config.START_FLOOD_WINDOW)
    conn = _conn()
    row = _row(conn.execute(
        "SELECT COUNT(DISTINCT user_id) AS n FROM start_events WHERE ts >= ?",
        (cutoff,)))
    conn.close()
    return int(row["n"]) if row else 0


def clear_start_events() -> None:
    conn = _conn()
    conn.execute("DELETE FROM start_events")
    conn.commit()
    conn.close()


# =========================================================================== #
# Customer-bot runtime state (owner writes, customer bot reads)
# =========================================================================== #
def get_bot_state() -> dict:
    conn = _conn()
    row = _row(conn.execute("SELECT * FROM bot_state WHERE id = 1"))
    conn.close()
    return row or {"online": 1, "sends_frozen": 0}


def is_bot_online() -> bool:
    return bool(get_bot_state().get("online", 1))


def set_bot_online(online: bool, by: str = "", note: str = "") -> None:
    conn = _conn()
    conn.execute(
        "UPDATE bot_state SET online = ?, offline_by = ?, offline_at = ?, "
        "offline_note = ? WHERE id = 1",
        (1 if online else 0, by or "", "" if online else _now(), note or ""))
    conn.commit()
    conn.close()


def are_sends_frozen() -> bool:
    """The emergency stop: every send loop checks this and halts."""
    return bool(get_bot_state().get("sends_frozen", 0))


def set_sends_frozen(frozen: bool) -> None:
    conn = _conn()
    conn.execute("UPDATE bot_state SET sends_frozen = ?, frozen_at = ? WHERE id = 1",
                 (1 if frozen else 0, _now() if frozen else ""))
    conn.commit()
    conn.close()


def set_health_report(report: dict) -> None:
    """Park the last health sweep where the OWNER BOT can read it.

    The engine runs in the customer process (it needs the in-memory busy
    registry), but the owner's panel is a different process, so an in-memory
    report would be invisible to the only person who wants it. One row in the
    shared table is the whole bridge.
    """
    conn = _conn()
    conn.execute("UPDATE bot_state SET health_report = ?, health_at = ? WHERE id = 1",
                 (json.dumps(report, ensure_ascii=False), _now()))
    conn.commit()
    conn.close()


def get_health_report() -> dict:
    row = get_bot_state()
    try:
        report = json.loads(row.get("health_report") or "{}")
    except (TypeError, ValueError):
        return {}
    if isinstance(report, dict) and row.get("health_at"):
        report.setdefault("at", row["health_at"])
    return report if isinstance(report, dict) else {}


# =========================================================================== #
# Daily usage counters — stats AND the probe budget
# =========================================================================== #
def usage_incr(customer_id, kind: str, n: int = 1) -> int:
    cid = _require_cid(customer_id)
    day = _today()
    conn = _conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO usage_daily (customer_id, day, kind, count) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(customer_id, day, kind) DO UPDATE SET count = count + ?",
        (cid, day, kind, int(n), int(n)))
    conn.commit()
    row = _row(c.execute(
        "SELECT count FROM usage_daily WHERE customer_id = ? AND day = ? AND kind = ?",
        (cid, day, kind)))
    conn.close()
    return int(row["count"]) if row else 0


def usage_today(customer_id, kind: str) -> int:
    cid = _require_cid(customer_id)
    conn = _conn()
    row = _row(conn.execute(
        "SELECT count FROM usage_daily WHERE customer_id = ? AND day = ? AND kind = ?",
        (cid, _today(), kind)))
    conn.close()
    return int(row["count"]) if row else 0


def usage_report(customer_id, days: int = 7) -> list:
    cid = _require_cid(customer_id)
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT day, kind, count FROM usage_daily WHERE customer_id = ? "
        "ORDER BY day DESC LIMIT ?", (cid, int(days) * 8)))
    conn.close()
    return rows


def probe_budget_left(customer_id) -> int:
    """How many numbers this customer may still probe today.

    Probing is the operation that actually hammers Rubika, so it is the one
    thing we meter: without this a single customer could hand us a million
    numbers and burn the whole fleet's IP reputation in an afternoon.
    """
    used = usage_today(customer_id, "probe")
    return max(0, int(config.PROBE_DAILY_CAP) - used)


def probe_spend(customer_id, n: int = 1) -> int:
    return usage_incr(customer_id, "probe", n)


# =========================================================================== #
# Pool brain — several accounts leeching ONE shared number space in parallel
# =========================================================================== #
def pool_create_job(customer_id, prefix: str, target: int, suffix_width: int,
                    affine_a: int, affine_offset: int, mode: str, content: str,
                    accounts: list) -> int:
    cid = _require_cid(customer_id)
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO pool_jobs (customer_id, prefix, target, suffix_width, "
        "affine_a, affine_offset, cursor, mode, content, status, created_at, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, 'leeching', ?, ?)",
        (cid, str(prefix), int(target), int(suffix_width), int(affine_a),
         int(affine_offset), str(mode), str(content or ""), _now(), _now()))
    job_id = cur.lastrowid
    for acc in accounts:
        cur.execute(
            "INSERT OR IGNORE INTO pool_job_accounts (job_id, customer_id, "
            "account_id, phone) VALUES (?, ?, ?, ?)",
            (job_id, cid, int(acc["id"]), acc["phone"]))
    conn.commit()
    conn.close()
    return job_id


def pool_get_job(customer_id, job_id) -> dict | None:
    cid = _require_cid(customer_id)
    conn = _conn()
    row = _row(conn.execute(
        "SELECT * FROM pool_jobs WHERE id = ? AND customer_id = ?",
        (int(job_id), cid)))
    conn.close()
    return row


def pool_set_status(customer_id, job_id, status: str) -> None:
    cid = _require_cid(customer_id)
    conn = _conn()
    conn.execute("UPDATE pool_jobs SET status = ?, updated_at = ? "
                 "WHERE id = ? AND customer_id = ?",
                 (str(status), _now(), int(job_id), cid))
    conn.commit()
    conn.close()


def pool_lease_block(customer_id, job_id, size: int) -> tuple:
    """Hand out the next `size` indices of the number space, atomically.

    This is the heart of the parallelism. Several accounts ask at the same
    moment, and each must get a range nobody else has: the read and the bump
    happen in ONE immediate transaction, so two accounts cannot both see the same
    cursor and probe the same numbers twice.
    """
    cid = _require_cid(customer_id)
    conn = _conn()
    try:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        row = _row(conn.execute(
            "SELECT cursor, probed FROM pool_jobs WHERE id = ? AND customer_id = ?",
            (int(job_id), cid)))
        if not row:
            conn.execute("ROLLBACK")
            return (0, 0)
        start = int(row["cursor"] or 0)
        conn.execute("UPDATE pool_jobs SET cursor = ?, updated_at = ? WHERE id = ?",
                     (start + int(size), _now(), int(job_id)))
        conn.execute("COMMIT")
        return (start, start + int(size))
    finally:
        conn.close()


def pool_set_halt(customer_id, job_id, reason: str) -> None:
    """Record why leeching stopped, first writer wins.

    Several accounts notice the same wall at almost the same moment. The first
    reason is the true one; later ones are echoes, and letting them overwrite
    would report "reached target" for a job that actually ran out of budget.
    """
    cid = _require_cid(customer_id)
    conn = _conn()
    conn.execute("UPDATE pool_jobs SET halt_reason = ? WHERE id = ? "
                 "AND customer_id = ? AND (halt_reason IS NULL OR halt_reason = '')",
                 (str(reason), int(job_id), cid))
    conn.commit()
    conn.close()


def pool_incr_probed(customer_id, job_id, n: int) -> None:
    cid = _require_cid(customer_id)
    conn = _conn()
    conn.execute("UPDATE pool_jobs SET probed = probed + ?, updated_at = ? "
                 "WHERE id = ? AND customer_id = ?",
                 (int(n), _now(), int(job_id), cid))
    conn.commit()
    conn.close()


def pool_accounts(customer_id, job_id) -> list:
    cid = _require_cid(customer_id)
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT * FROM pool_job_accounts WHERE job_id = ? AND customer_id = ? "
        "ORDER BY id", (int(job_id), cid)))
    conn.close()
    return rows


def pool_set_account(customer_id, job_id, account_id, **fields) -> None:
    cid = _require_cid(customer_id)
    allowed = {"found", "sent", "status", "note"}
    sets, params = [], []
    for key, value in fields.items():
        if key in allowed:
            sets.append(f"{key} = ?")
            params.append(value)
    if not sets:
        return
    params += [int(job_id), int(account_id), cid]
    conn = _conn()
    conn.execute(
        f"UPDATE pool_job_accounts SET {', '.join(sets)} WHERE job_id = ? "
        "AND account_id = ? AND customer_id = ?", params)
    conn.commit()
    conn.close()


def pool_add_contact(customer_id, job_id, account_id, phone: str, guid: str) -> bool:
    """Record a hit. False if this guid is already in the job.

    The UNIQUE(job_id, guid) is what stops two accounts from both counting — and
    later both messaging — the same person when their blocks happen to contain
    two numbers belonging to one user.
    """
    cid = _require_cid(customer_id)
    if not guid:
        return False
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO pool_contacts (job_id, customer_id, account_id, "
        "phone, guid) VALUES (?, ?, ?, ?, ?)",
        (int(job_id), cid, int(account_id), str(phone), str(guid)))
    added = cur.rowcount > 0
    conn.commit()
    conn.close()
    return added


def pool_hit_count(customer_id, job_id) -> int:
    cid = _require_cid(customer_id)
    conn = _conn()
    row = _row(conn.execute(
        "SELECT COUNT(*) AS n FROM pool_contacts WHERE job_id = ? "
        "AND customer_id = ?", (int(job_id), cid)))
    conn.close()
    return int(row["n"]) if row else 0


def pool_account_guids(customer_id, job_id, account_id,
                       unsent_only: bool = False) -> list:
    cid = _require_cid(customer_id)
    sql = ("SELECT guid, phone FROM pool_contacts WHERE job_id = ? "
           "AND customer_id = ? AND account_id = ?")
    if unsent_only:
        sql += " AND sent = 0"
    conn = _conn()
    rows = _rows(conn.execute(sql + " ORDER BY id",
                              (int(job_id), cid, int(account_id))))
    conn.close()
    return rows


def pool_mark_sent(customer_id, job_id, guid: str) -> None:
    """Only CONFIRMED deliveries land here, which is what makes a resumed job
    pick up where it stopped instead of messaging people twice."""
    cid = _require_cid(customer_id)
    conn = _conn()
    conn.execute("UPDATE pool_contacts SET sent = 1 WHERE job_id = ? "
                 "AND customer_id = ? AND guid = ?",
                 (int(job_id), cid, str(guid)))
    conn.commit()
    conn.close()


def pool_counts(customer_id, job_id) -> dict:
    cid = _require_cid(customer_id)
    conn = _conn()
    row = _row(conn.execute(
        "SELECT COUNT(*) AS found, COALESCE(SUM(sent), 0) AS sent "
        "FROM pool_contacts WHERE job_id = ? AND customer_id = ?",
        (int(job_id), cid)))
    conn.close()
    return {"found": int((row or {}).get("found") or 0),
            "sent": int((row or {}).get("sent") or 0)}


def pool_list_jobs(customer_id, limit: int = 10) -> list:
    cid = _require_cid(customer_id)
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT * FROM pool_jobs WHERE customer_id = ? ORDER BY id DESC LIMIT ?",
        (cid, int(limit))))
    conn.close()
    return rows


def owner_pool_unfinished() -> list:
    """Jobs that were mid-flight when the process died — restart recovery only."""
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT * FROM pool_jobs WHERE status IN ('leeching', 'sending') "
        "ORDER BY id"))
    conn.close()
    return rows


def pool_delete_job(customer_id, job_id) -> None:
    cid = _require_cid(customer_id)
    conn = _conn()
    for table in ("pool_contacts", "pool_job_accounts", "pool_jobs"):
        column = "id" if table == "pool_jobs" else "job_id"
        conn.execute(f"DELETE FROM {table} WHERE {column} = ? AND customer_id = ?",
                     (int(job_id), cid))
    conn.commit()
    conn.close()


def owner_usage_totals(day: str = None) -> dict:
    """Service-wide totals for the owner dashboard."""
    day = day or _today()
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT kind, SUM(count) AS n FROM usage_daily WHERE day = ? GROUP BY kind",
        (day,)))
    conn.close()
    return {r["kind"]: int(r["n"] or 0) for r in rows}


def owner_usage_last_days(days: int = 7, kind: str = "send") -> list:
    """[(day, count)] oldest->newest, for the little text chart."""
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT day, SUM(count) AS n FROM usage_daily WHERE kind = ? "
        "GROUP BY day ORDER BY day DESC LIMIT ?", (kind, int(days))))
    conn.close()
    return [(r["day"], int(r["n"] or 0)) for r in reversed(rows)]


def owner_account_totals() -> dict:
    """Rubika + Telegram account and send totals across every customer."""
    conn = _conn()
    rb = _row(conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS healthy, "
        "COALESCE(SUM(sent_total), 0) AS sent FROM accounts")) or {}
    tg = _row(conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS healthy, "
        "COALESCE(SUM(sent_total), 0) AS sent FROM tg_accounts")) or {}
    conn.close()
    return {
        "rubika": {"total": int(rb.get("total") or 0),
                   "healthy": int(rb.get("healthy") or 0),
                   "sent": int(rb.get("sent") or 0)},
        "telegram": {"total": int(tg.get("total") or 0),
                     "healthy": int(tg.get("healthy") or 0),
                     "sent": int(tg.get("sent") or 0)},
    }



# =========================================================================== #
# Worker fleet — GLOBAL by nature (the fleet belongs to the owner and is shared
# by every customer). The customer bot reads these rows to place an account on a
# worker; it never sees the decrypted SSH credentials because it never calls the
# provisioning code.
# =========================================================================== #
def add_worker(tag: str, ip: str, ssh_port: int, ssh_user: str, ssh_pass_enc: str,
               api_port: int, api_token_enc: str, is_master: int = 0) -> int:
    conn = _conn()
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO workers (tag, ip, ssh_port, ssh_user, ssh_pass_enc, "
        "api_port, api_token_enc, is_master, enabled, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
        (tag, ip, int(ssh_port or 22), ssh_user or "", ssh_pass_enc or "",
         int(api_port or config.WORKER_API_PORT), api_token_enc or "",
         int(is_master or 0), _now()))
    conn.commit()
    row = _row(c.execute("SELECT id FROM workers WHERE tag = ?", (tag,)))
    conn.close()
    return int(row["id"]) if row else 0


def list_workers() -> list:
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT * FROM workers ORDER BY is_master DESC, id"))
    conn.close()
    return rows


def list_enabled_workers() -> list:
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT * FROM workers WHERE enabled = 1 ORDER BY is_master DESC, id"))
    conn.close()
    return rows


def get_worker(worker_id) -> dict | None:
    conn = _conn()
    row = _row(conn.execute("SELECT * FROM workers WHERE id = ?", (int(worker_id),)))
    conn.close()
    return row


def get_worker_by_tag(tag: str) -> dict | None:
    conn = _conn()
    row = _row(conn.execute("SELECT * FROM workers WHERE tag = ?", (tag,)))
    conn.close()
    return row


def get_master_worker() -> dict | None:
    conn = _conn()
    row = _row(conn.execute(
        "SELECT * FROM workers WHERE is_master = 1 ORDER BY id LIMIT 1"))
    conn.close()
    return row


def set_worker_enabled(worker_id, enabled: bool) -> None:
    conn = _conn()
    conn.execute("UPDATE workers SET enabled = ? WHERE id = ?",
                 (1 if enabled else 0, int(worker_id)))
    conn.commit()
    conn.close()


def update_worker_health(worker_id, status: str, ping_ms: int, file_ok: int) -> None:
    conn = _conn()
    conn.execute(
        "UPDATE workers SET status = ?, ping_ms = ?, file_ok = ?, last_checked = ? "
        "WHERE id = ?",
        (status, int(ping_ms), int(file_ok), _now(), int(worker_id)))
    conn.commit()
    conn.close()


def delete_worker(worker_id) -> None:
    wid = int(worker_id)
    conn = _conn()
    c = conn.cursor()
    c.execute("DELETE FROM workers WHERE id = ?", (wid,))
    c.execute("DELETE FROM worker_daily WHERE worker_id = ?", (wid,))
    c.execute("UPDATE accounts SET worker_id = NULL WHERE worker_id = ?", (wid,))
    conn.commit()
    conn.close()


def count_accounts_on_worker(worker_id) -> int:
    conn = _conn()
    row = _row(conn.execute(
        "SELECT COUNT(*) AS n FROM accounts WHERE worker_id = ?", (int(worker_id),)))
    conn.close()
    return int(row["n"]) if row else 0


def worker_account_stats(worker_id) -> dict:
    """Accounts on one worker split into healthy / dead — the numbers the owner's
    worker-stats card shows."""
    conn = _conn()
    row = _row(conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS healthy "
        "FROM accounts WHERE worker_id = ?", (int(worker_id),))) or {}
    customers = _row(conn.execute(
        "SELECT COUNT(DISTINCT customer_id) AS n FROM accounts WHERE worker_id = ?",
        (int(worker_id),))) or {}
    conn.close()
    total = int(row.get("total") or 0)
    healthy = int(row.get("healthy") or 0)
    return {"total": total, "healthy": healthy, "dead": total - healthy,
            "customers": int(customers.get("n") or 0)}


def worker_customers(worker_id) -> list:
    """Which customers have accounts on this worker — so that when its IP gets
    throttled the owner knows exactly who to warn."""
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT c.telegram_id, c.name, c.username, COUNT(a.id) AS accounts "
        "FROM accounts a JOIN customers c ON c.telegram_id = a.customer_id "
        "WHERE a.worker_id = ? GROUP BY c.telegram_id ORDER BY accounts DESC",
        (int(worker_id),)))
    conn.close()
    return rows


def accounts_per_worker() -> list:
    """One row per worker with its account tally — the fleet overview."""
    out = []
    for w in list_workers():
        stats = worker_account_stats(w["id"])
        out.append({**w, **stats, "sent_today": worker_sent_today(w["id"])})
    return out


def incr_worker_sent(worker_id, n: int = 1) -> None:
    if not worker_id:
        return
    conn = _conn()
    conn.execute(
        "INSERT INTO worker_daily (worker_id, day, sent) VALUES (?, ?, ?) "
        "ON CONFLICT(worker_id, day) DO UPDATE SET sent = sent + ?",
        (int(worker_id), _today(), int(n), int(n)))
    conn.commit()
    conn.close()


def worker_sent_today(worker_id) -> int:
    conn = _conn()
    row = _row(conn.execute(
        "SELECT sent FROM worker_daily WHERE worker_id = ? AND day = ?",
        (int(worker_id), _today())))
    conn.close()
    return int(row["sent"]) if row else 0


def fleet_rr_next(pool_size: int) -> int:
    """Sequential round-robin pointer for login placement.

    Persisted, because an in-memory pointer resets on every restart and sends
    the next N logins all to worker #1.
    """
    pool_size = max(1, int(pool_size))
    conn = _conn()
    c = conn.cursor()
    row = _row(c.execute("SELECT rr_next FROM fleet_state WHERE id = 1"))
    current = int(row["rr_next"]) if row else 0
    idx = current % pool_size
    c.execute("UPDATE fleet_state SET rr_next = ? WHERE id = 1", ((current + 1) % 100000,))
    conn.commit()
    conn.close()
    return idx


# =========================================================================== #
# Notification outbox (owner panel -> customer, delivered by the customer bot)
# =========================================================================== #
def queue_notification(customer_id, text: str) -> int:
    cid = _require_cid(customer_id)
    conn = _conn()
    c = conn.cursor()
    c.execute("INSERT INTO notifications (customer_id, text, created_at) "
              "VALUES (?, ?, ?)", (cid, text or "", _now()))
    conn.commit()
    nid = c.lastrowid
    conn.close()
    return int(nid)


def fetch_unsent_notifications(limit: int = 50) -> list:
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT * FROM notifications WHERE sent = 0 ORDER BY id LIMIT ?",
        (int(limit),)))
    conn.close()
    return rows


def mark_notification_sent(notification_id) -> None:
    conn = _conn()
    conn.execute("UPDATE notifications SET sent = 1 WHERE id = ?",
                 (int(notification_id),))
    conn.commit()
    conn.close()


# =========================================================================== #
# Paused sends — powers "continue" so a resumed send never re-messages people
# =========================================================================== #
def save_paused_send(customer_id, account_id, phone: str, payload: dict) -> None:
    cid = _require_cid(customer_id)
    conn = _conn()
    conn.execute(
        "INSERT INTO paused_sends (account_id, customer_id, phone, payload, "
        "created_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(account_id) DO UPDATE SET "
        "customer_id = excluded.customer_id, phone = excluded.phone, "
        "payload = excluded.payload, created_at = excluded.created_at",
        (int(account_id), cid, phone or "",
         json.dumps(payload or {}, ensure_ascii=False), _now()))
    conn.commit()
    conn.close()


def get_paused_send(customer_id, account_id) -> dict | None:
    cid = _require_cid(customer_id)
    conn = _conn()
    row = _row(conn.execute(
        "SELECT * FROM paused_sends WHERE account_id = ? AND customer_id = ?",
        (int(account_id), cid)))
    conn.close()
    if not row:
        return None
    try:
        row["payload"] = json.loads(row.get("payload") or "{}")
    except (TypeError, ValueError):
        row["payload"] = {}
    return row


def delete_paused_send(customer_id, account_id) -> None:
    cid = _require_cid(customer_id)
    conn = _conn()
    conn.execute("DELETE FROM paused_sends WHERE account_id = ? AND customer_id = ?",
                 (int(account_id), cid))
    conn.commit()
    conn.close()


def owner_list_paused_sends() -> list:
    conn = _conn()
    rows = _rows(conn.execute("SELECT * FROM paused_sends ORDER BY created_at"))
    conn.close()
    return rows


# =========================================================================== #
# Anti-duplicate ledgers (per customer AND per account)
# =========================================================================== #
def _sent_table(platform: str) -> str:
    return "tg_sent" if str(platform).lower().startswith("tg") else "rb_sent"


def mark_sent(customer_id, account_id, target, platform: str = "rb") -> None:
    cid = _require_cid(customer_id)
    table = _sent_table(platform)
    conn = _conn()
    conn.execute(
        f"INSERT OR IGNORE INTO {table} (customer_id, account_id, target, sent_at) "
        "VALUES (?, ?, ?, ?)", (cid, int(account_id), str(target), _now()))
    conn.commit()
    conn.close()


def was_sent(customer_id, account_id, target, platform: str = "rb") -> bool:
    cid = _require_cid(customer_id)
    table = _sent_table(platform)
    conn = _conn()
    row = _row(conn.execute(
        f"SELECT 1 AS x FROM {table} WHERE customer_id = ? AND account_id = ? "
        "AND target = ?", (cid, int(account_id), str(target))))
    conn.close()
    return bool(row)


def sent_targets(customer_id, account_id, platform: str = "rb") -> set:
    """The whole already-sent set for one account, for bulk filtering."""
    cid = _require_cid(customer_id)
    table = _sent_table(platform)
    conn = _conn()
    rows = _rows(conn.execute(
        f"SELECT target FROM {table} WHERE customer_id = ? AND account_id = ?",
        (cid, int(account_id))))
    conn.close()
    return {r["target"] for r in rows}


def reset_sent(customer_id, account_id, platform: str = "rb") -> int:
    """Clear the ledger so the customer can deliberately message everyone again."""
    cid = _require_cid(customer_id)
    table = _sent_table(platform)
    conn = _conn()
    c = conn.cursor()
    c.execute(f"DELETE FROM {table} WHERE customer_id = ? AND account_id = ?",
              (cid, int(account_id)))
    conn.commit()
    n = c.rowcount
    conn.close()
    return int(n or 0)


# =========================================================================== #
# Number-status cache (GLOBAL on purpose — see the schema comment)
# =========================================================================== #
def number_seen(phone: str) -> dict | None:
    conn = _conn()
    row = _row(conn.execute("SELECT * FROM number_status WHERE phone = ?",
                            (str(phone),)))
    conn.close()
    return row


def number_record(phone: str, on_rubika: bool) -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO number_status (phone, on_rubika, checked_at) VALUES (?, ?, ?) "
        "ON CONFLICT(phone) DO UPDATE SET on_rubika = excluded.on_rubika, "
        "checked_at = excluded.checked_at",
        (str(phone), 1 if on_rubika else 0, _now()))
    conn.commit()
    conn.close()


def numbers_known(phones: list) -> set:
    """Which of these numbers we have already probed (any customer)."""
    phones = [str(p) for p in (phones or [])]
    if not phones:
        return set()
    out = set()
    conn = _conn()
    for i in range(0, len(phones), 400):
        chunk = phones[i:i + 400]
        marks = ",".join("?" * len(chunk))
        rows = _rows(conn.execute(
            f"SELECT phone FROM number_status WHERE phone IN ({marks})", chunk))
        out.update(r["phone"] for r in rows)
    conn.close()
    return out


# =========================================================================== #
# Persisted contact-build / discovery jobs (restart-safe)
# =========================================================================== #
def cjob_create(customer_id, account_id, phone: str, kind: str,
                payload: dict) -> int:
    cid = _require_cid(customer_id)
    conn = _conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO contact_jobs (customer_id, account_id, phone, kind, status, "
        "payload, created_at, updated_at) VALUES (?, ?, ?, ?, 'running', ?, ?, ?)",
        (cid, int(account_id), phone or "", kind or "import",
         json.dumps(payload or {}, ensure_ascii=False), _now(), _now()))
    conn.commit()
    jid = c.lastrowid
    conn.close()
    return int(jid)


def cjob_update(customer_id, job_id, **fields) -> None:
    cid = _require_cid(customer_id)
    allowed = {"status", "cursor", "added", "failed", "payload"}
    sets, params = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "payload":
            value = json.dumps(value or {}, ensure_ascii=False)
        sets.append(f"{key} = ?")
        params.append(value)
    if not sets:
        return
    sets.append("updated_at = ?")
    params.append(_now())
    params += [int(job_id), cid]
    conn = _conn()
    conn.execute(
        f"UPDATE contact_jobs SET {', '.join(sets)} WHERE id = ? AND customer_id = ?",
        params)
    conn.commit()
    conn.close()


def cjob_get(customer_id, job_id) -> dict | None:
    cid = _require_cid(customer_id)
    conn = _conn()
    row = _row(conn.execute(
        "SELECT * FROM contact_jobs WHERE id = ? AND customer_id = ?",
        (int(job_id), cid)))
    conn.close()
    if row:
        try:
            row["payload"] = json.loads(row.get("payload") or "{}")
        except (TypeError, ValueError):
            row["payload"] = {}
    return row


def cjob_running(customer_id) -> list:
    cid = _require_cid(customer_id)
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT * FROM contact_jobs WHERE customer_id = ? AND status = 'running' "
        "ORDER BY id", (cid,)))
    conn.close()
    return rows


def owner_cjobs_running() -> list:
    """Every unfinished job across all customers — used once, at boot, to resume
    them (and to re-register each one in the busy registry)."""
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT * FROM contact_jobs WHERE status = 'running' ORDER BY id"))
    conn.close()
    for row in rows:
        try:
            row["payload"] = json.loads(row.get("payload") or "{}")
        except (TypeError, ValueError):
            row["payload"] = {}
    return rows



# =========================================================================== #
# Tabchi (the group engine) — rebuilt from the base project's automation core.
# Kept: rotating texts, own group list, join-by-link, interval, auto-mute of a
# failing group. Dropped: the shared verified-link pool (it would push one
# customer's groups onto every other customer's accounts).
# =========================================================================== #
def tabchi_get(customer_id, account_id) -> dict:
    cid = _require_cid(customer_id)
    aid = int(account_id)
    conn = _conn()
    c = conn.cursor()
    row = _row(c.execute(
        "SELECT * FROM tabchi WHERE account_id = ? AND customer_id = ?", (aid, cid)))
    if not row:
        c.execute(
            "INSERT OR IGNORE INTO tabchi (account_id, customer_id, enabled, "
            "interval_sec, sent_total, updated_at) VALUES (?, ?, 0, ?, 0, ?)",
            (aid, cid, config.TABCHI_DEFAULT_INTERVAL, _now()))
        conn.commit()
        row = _row(c.execute(
            "SELECT * FROM tabchi WHERE account_id = ? AND customer_id = ?",
            (aid, cid)))
    conn.close()
    return row or {}


def tabchi_set(customer_id, account_id, **fields) -> None:
    cid = _require_cid(customer_id)
    tabchi_get(cid, account_id)          # make sure the row exists
    allowed = {"enabled", "interval_sec", "last_run"}
    sets, params = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "enabled":
            value = 1 if value else 0
        if key == "interval_sec":
            value = config.clamp_tabchi_interval(value)
        sets.append(f"{key} = ?")
        params.append(value)
    if not sets:
        return
    sets.append("updated_at = ?")
    params.append(_now())
    params += [int(account_id), cid]
    conn = _conn()
    conn.execute(
        f"UPDATE tabchi SET {', '.join(sets)} WHERE account_id = ? AND customer_id = ?",
        params)
    conn.commit()
    conn.close()


def tabchi_incr_sent(customer_id, account_id, n: int = 1) -> None:
    cid = _require_cid(customer_id)
    # The row is created lazily by tabchi_get, so without this an UPDATE can
    # match zero rows and lose the count in total silence — the worst kind of
    # counter bug, because the feature looks like it did nothing.
    tabchi_get(cid, account_id)
    conn = _conn()
    conn.execute("UPDATE tabchi SET sent_total = sent_total + ?, updated_at = ? "
                 "WHERE account_id = ? AND customer_id = ?",
                 (int(n), _now(), int(account_id), cid))
    conn.commit()
    conn.close()


def tabchi_enabled_accounts(customer_id) -> list:
    cid = _require_cid(customer_id)
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT t.* FROM tabchi t JOIN accounts a ON a.id = t.account_id "
        "WHERE t.customer_id = ? AND t.enabled = 1 AND a.status = 'active' "
        "ORDER BY t.account_id", (cid,)))
    conn.close()
    return rows


def owner_tabchi_enabled() -> list:
    """Every enabled tabchi across all customers — boot-time relaunch only."""
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT t.* FROM tabchi t JOIN accounts a ON a.id = t.account_id "
        "WHERE t.enabled = 1 AND a.status = 'active' ORDER BY t.customer_id, "
        "t.account_id"))
    conn.close()
    return rows


# ---- tabchi texts (rotated, so the same message never repeats back-to-back) - #
def tabchi_add_text(customer_id, account_id, text: str) -> int:
    cid = _require_cid(customer_id)
    conn = _conn()
    c = conn.cursor()
    c.execute("INSERT INTO tabchi_texts (customer_id, account_id, text, added_at) "
              "VALUES (?, ?, ?, ?)", (cid, int(account_id), text or "", _now()))
    conn.commit()
    tid = c.lastrowid
    conn.close()
    return int(tid)


def tabchi_texts(customer_id, account_id) -> list:
    cid = _require_cid(customer_id)
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT * FROM tabchi_texts WHERE customer_id = ? AND account_id = ? "
        "ORDER BY id", (cid, int(account_id))))
    conn.close()
    return rows


def tabchi_clear_texts(customer_id, account_id) -> int:
    cid = _require_cid(customer_id)
    conn = _conn()
    c = conn.cursor()
    c.execute("DELETE FROM tabchi_texts WHERE customer_id = ? AND account_id = ?",
              (cid, int(account_id)))
    conn.commit()
    n = c.rowcount
    conn.close()
    return int(n or 0)


# ---- tabchi group links ---------------------------------------------------- #
def tabchi_add_group(customer_id, account_id, link: str) -> bool:
    cid = _require_cid(customer_id)
    conn = _conn()
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO tabchi_groups (customer_id, account_id, link, "
        "added_at) VALUES (?, ?, ?, ?)",
        (cid, int(account_id), (link or "").strip(), _now()))
    conn.commit()
    added = c.rowcount > 0
    conn.close()
    return added


def tabchi_groups(customer_id, account_id, joined_only: bool = False) -> list:
    cid = _require_cid(customer_id)
    sql = ("SELECT * FROM tabchi_groups WHERE customer_id = ? AND account_id = ?")
    if joined_only:
        sql += " AND joined = 1 AND muted = 0"
    sql += " ORDER BY id"
    conn = _conn()
    rows = _rows(conn.execute(sql, (cid, int(account_id))))
    conn.close()
    return rows


def tabchi_group_joined(customer_id, group_id, guid: str, name: str = "") -> None:
    cid = _require_cid(customer_id)
    conn = _conn()
    conn.execute(
        "UPDATE tabchi_groups SET joined = 1, guid = ?, name = ?, fails = 0 "
        "WHERE id = ? AND customer_id = ?",
        (guid or "", name or "", int(group_id), cid))
    conn.commit()
    conn.close()


def tabchi_group_fail(customer_id, group_id) -> int:
    """Count a failure and auto-mute the group once it keeps failing.

    A group that rejects us (kicked, closed, admin-only) would otherwise be
    retried forever, every interval, generating errors and looking like abuse.
    """
    cid = _require_cid(customer_id)
    conn = _conn()
    c = conn.cursor()
    c.execute("UPDATE tabchi_groups SET fails = fails + 1 "
              "WHERE id = ? AND customer_id = ?", (int(group_id), cid))
    row = _row(c.execute(
        "SELECT fails FROM tabchi_groups WHERE id = ? AND customer_id = ?",
        (int(group_id), cid)))
    fails = int(row["fails"]) if row else 0
    if fails >= config.TABCHI_GROUP_MAX_FAILS:
        c.execute("UPDATE tabchi_groups SET muted = 1 "
                  "WHERE id = ? AND customer_id = ?", (int(group_id), cid))
    conn.commit()
    conn.close()
    return fails


def tabchi_group_ok(customer_id, group_id) -> None:
    cid = _require_cid(customer_id)
    conn = _conn()
    conn.execute("UPDATE tabchi_groups SET fails = 0 WHERE id = ? AND customer_id = ?",
                 (int(group_id), cid))
    conn.commit()
    conn.close()


def tabchi_unmute_all(customer_id, account_id) -> int:
    cid = _require_cid(customer_id)
    conn = _conn()
    c = conn.cursor()
    c.execute("UPDATE tabchi_groups SET muted = 0, fails = 0 "
              "WHERE customer_id = ? AND account_id = ?", (cid, int(account_id)))
    conn.commit()
    n = c.rowcount
    conn.close()
    return int(n or 0)


def tabchi_clear_groups(customer_id, account_id) -> int:
    cid = _require_cid(customer_id)
    conn = _conn()
    c = conn.cursor()
    c.execute("DELETE FROM tabchi_groups WHERE customer_id = ? AND account_id = ?",
              (cid, int(account_id)))
    conn.commit()
    n = c.rowcount
    conn.close()
    return int(n or 0)


def tabchi_apply_to_all(customer_id, source_account_id) -> int:
    """Copy one account's tabchi setup (texts + interval) onto EVERY other
    account of the same customer.

    Without this a customer with twenty accounts has to retype the same text
    twenty times, which is the single most common complaint about panels like
    this. Group links are intentionally NOT copied: a link list is tied to the
    account that is actually a member of those groups.
    """
    cid = _require_cid(customer_id)
    src = int(source_account_id)
    source = tabchi_get(cid, src)
    texts = [t["text"] for t in tabchi_texts(cid, src)]
    interval = int(source.get("interval_sec") or config.TABCHI_DEFAULT_INTERVAL)
    targets = [a["id"] for a in list_accounts(cid) if int(a["id"]) != src]
    for aid in targets:
        tabchi_get(cid, aid)
        tabchi_set(cid, aid, interval_sec=interval)
        tabchi_clear_texts(cid, aid)
        for text in texts:
            tabchi_add_text(cid, aid, text)
    return len(targets)


# =========================================================================== #
# Secretary (auto-reply in private chats) — lives inside the Tabchi section
# =========================================================================== #
def secretary_get(customer_id, account_id) -> dict:
    cid = _require_cid(customer_id)
    aid = int(account_id)
    conn = _conn()
    c = conn.cursor()
    row = _row(c.execute(
        "SELECT * FROM secretary WHERE account_id = ? AND customer_id = ?",
        (aid, cid)))
    if not row:
        c.execute(
            "INSERT OR IGNORE INTO secretary (account_id, customer_id, enabled, "
            "mode, text, interval_sec, updated_at) VALUES (?, ?, 0, 'text', '', ?, ?)",
            (aid, cid, config.SECRETARY_INTERVAL, _now()))
        conn.commit()
        row = _row(c.execute(
            "SELECT * FROM secretary WHERE account_id = ? AND customer_id = ?",
            (aid, cid)))
    conn.close()
    return row or {}


def secretary_set(customer_id, account_id, **fields) -> None:
    cid = _require_cid(customer_id)
    secretary_get(cid, account_id)
    allowed = {"enabled", "mode", "text", "interval_sec"}
    sets, params = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "enabled":
            value = 1 if value else 0
        if key == "interval_sec":
            value = config.clamp_secretary_interval(value)
        sets.append(f"{key} = ?")
        params.append(value)
    if not sets:
        return
    sets.append("updated_at = ?")
    params.append(_now())
    params += [int(account_id), cid]
    conn = _conn()
    conn.execute(
        f"UPDATE secretary SET {', '.join(sets)} WHERE account_id = ? "
        "AND customer_id = ?", params)
    conn.commit()
    conn.close()


def secretary_incr(customer_id, account_id, n: int = 1) -> None:
    cid = _require_cid(customer_id)
    secretary_get(cid, account_id)       # lazily-created row; see tabchi_incr_sent
    conn = _conn()
    conn.execute("UPDATE secretary SET replied_total = replied_total + ? "
                 "WHERE account_id = ? AND customer_id = ?",
                 (int(n), int(account_id), cid))
    conn.commit()
    conn.close()


def secretary_enabled_accounts(customer_id) -> list:
    cid = _require_cid(customer_id)
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT s.* FROM secretary s JOIN accounts a ON a.id = s.account_id "
        "WHERE s.customer_id = ? AND s.enabled = 1 AND a.status = 'active' "
        "ORDER BY s.account_id", (cid,)))
    conn.close()
    return rows


def owner_secretary_enabled() -> list:
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT s.* FROM secretary s JOIN accounts a ON a.id = s.account_id "
        "WHERE s.enabled = 1 AND a.status = 'active'"))
    conn.close()
    return rows


def secretary_was_replied(customer_id, account_id, target) -> bool:
    """Have we already answered this person?

    Without this ledger the secretary answers the same chat on every single
    pass, which reads as spam to the recipient and to the platform.
    """
    cid = _require_cid(customer_id)
    conn = _conn()
    row = _row(conn.execute(
        "SELECT 1 AS x FROM secretary_replied WHERE customer_id = ? "
        "AND account_id = ? AND target = ?", (cid, int(account_id), str(target))))
    conn.close()
    return bool(row)


def secretary_mark_replied(customer_id, account_id, target) -> None:
    cid = _require_cid(customer_id)
    conn = _conn()
    conn.execute(
        "INSERT OR IGNORE INTO secretary_replied (customer_id, account_id, target, "
        "replied_at) VALUES (?, ?, ?, ?)",
        (cid, int(account_id), str(target), _now()))
    conn.commit()
    conn.close()


def secretary_replied_recent(customer_id, account_id, limit: int = 2000) -> list:
    """The most recently answered targets, newest first.

    A remote secretary pass runs on a worker, but this ledger stays on the master
    so it survives a worker being rebuilt. The worker therefore has to be told
    who to skip, and that list is capped because it crosses the tunnel on every
    pass.
    """
    cid = _require_cid(customer_id)
    conn = _conn()
    rows = conn.execute(
        "SELECT target FROM secretary_replied WHERE customer_id = ? "
        "AND account_id = ? ORDER BY replied_at DESC, rowid DESC LIMIT ?",
        (cid, int(account_id), max(1, int(limit)))).fetchall()
    conn.close()
    return [r[0] for r in rows]


def secretary_apply_to_all(customer_id, source_account_id) -> int:
    """Copy one account's secretary setup onto every other account."""
    cid = _require_cid(customer_id)
    src = int(source_account_id)
    source = secretary_get(cid, src)
    targets = [a["id"] for a in list_accounts(cid) if int(a["id"]) != src]
    for aid in targets:
        secretary_set(cid, aid,
                      mode=source.get("mode") or "text",
                      text=source.get("text") or "",
                      interval_sec=source.get("interval_sec")
                      or config.SECRETARY_INTERVAL)
    return len(targets)


# =========================================================================== #
# Diagnostics — powers the owner's "look up a phone number" button
# =========================================================================== #
def owner_locate_phone(phone: str) -> list:
    """Find every account (both platforms) matching a phone, with its owner and
    worker. Turns "my account doesn't work" from a 20-minute dig into one tap."""
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if not digits:
        return []
    like = f"%{digits}%"
    conn = _conn()
    out = []
    for platform, table in (("rubika", "accounts"), ("telegram", "tg_accounts")):
        rows = _rows(conn.execute(
            f"SELECT a.*, c.name AS customer_name, c.username AS customer_username "
            f"FROM {table} a LEFT JOIN customers c ON c.telegram_id = a.customer_id "
            "WHERE a.phone LIKE ?", (like,)))
        for row in rows:
            row["platform"] = platform
            out.append(row)
    conn.close()
    return out



# =========================================================================== #
# Support tickets
# =========================================================================== #
def add_ticket(customer_id, text: str) -> int:
    """File a support ticket. Written by the customer bot."""
    cid = _require_cid(customer_id)
    conn = _conn()
    c = conn.cursor()
    c.execute("INSERT INTO tickets (customer_id, text, created_at) "
              "VALUES (?, ?, ?)", (cid, (text or "").strip(), _now()))
    conn.commit()
    tid = c.lastrowid
    conn.close()
    return int(tid)


def customer_tickets(customer_id, limit: int = 10) -> list:
    cid = _require_cid(customer_id)
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT * FROM tickets WHERE customer_id = ? ORDER BY id DESC LIMIT ?",
        (cid, int(limit))))
    conn.close()
    return rows


def customer_open_tickets(customer_id) -> int:
    """How many unanswered tickets this customer already has.

    The panel uses it to refuse a second one: an open ticket queue of fifty
    duplicates from the same person is not more information, just more noise.
    """
    cid = _require_cid(customer_id)
    conn = _conn()
    row = _row(conn.execute(
        "SELECT COUNT(*) AS n FROM tickets WHERE customer_id = ? AND answered = 0",
        (cid,)))
    conn.close()
    return int(row["n"]) if row else 0


def owner_get_ticket(ticket_id) -> dict | None:
    conn = _conn()
    row = _row(conn.execute("SELECT * FROM tickets WHERE id = ?",
                            (int(ticket_id),)))
    conn.close()
    return row


def owner_list_tickets(only_open: bool = True, limit: int = 30) -> list:
    sql = "SELECT * FROM tickets"
    if only_open:
        sql += " WHERE answered = 0"
    sql += " ORDER BY id DESC LIMIT ?"
    conn = _conn()
    rows = _rows(conn.execute(sql, (int(limit),)))
    conn.close()
    return rows


def owner_count_open_tickets() -> int:
    conn = _conn()
    row = _row(conn.execute("SELECT COUNT(*) AS n FROM tickets WHERE answered = 0"))
    conn.close()
    return int(row["n"]) if row else 0


def owner_answer_ticket(ticket_id, answer: str) -> None:
    conn = _conn()
    conn.execute("UPDATE tickets SET answered = 1, answer = ?, answered_at = ? "
                 "WHERE id = ?", (answer or "", _now(), int(ticket_id)))
    conn.commit()
    conn.close()



# =========================================================================== #
# Telegram multi-account send jobs
# =========================================================================== #
def tgm_create_job(customer_id, content: list, delay: float,
                   target_mode: str = "both") -> str:
    cid = _require_cid(customer_id)
    import uuid
    job_id = uuid.uuid4().hex[:12]
    conn = _conn()
    conn.execute(
        "INSERT INTO tg_multi_jobs (job_id, customer_id, state, content_json, "
        "delay, target_mode, created_at, updated_at) "
        "VALUES (?, ?, 'queued', ?, ?, ?, ?, ?)",
        (job_id, cid, json.dumps(content or [], ensure_ascii=False),
         float(delay), target_mode or "both", _now(), _now()))
    conn.commit()
    conn.close()
    return job_id


def tgm_get_job(customer_id, job_id) -> dict | None:
    cid = _require_cid(customer_id)
    conn = _conn()
    row = _row(conn.execute(
        "SELECT * FROM tg_multi_jobs WHERE job_id = ? AND customer_id = ?",
        (str(job_id), cid)))
    conn.close()
    if row:
        try:
            row["content"] = json.loads(row.get("content_json") or "[]")
        except (TypeError, ValueError):
            row["content"] = []
    return row


def tgm_update_job(customer_id, job_id, **fields) -> None:
    cid = _require_cid(customer_id)
    allowed = {"state", "total", "mutual_total", "sent_count", "failed_count",
               "skipped_count", "current_phone", "stop_requested", "last_error",
               "msg_id", "finished_at"}
    sets, params = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        sets.append(f"{key} = ?")
        params.append(value)
    if not sets:
        return
    sets.append("updated_at = ?")
    params.append(_now())
    params += [str(job_id), cid]
    conn = _conn()
    conn.execute(
        f"UPDATE tg_multi_jobs SET {', '.join(sets)} "
        "WHERE job_id = ? AND customer_id = ?", params)
    conn.commit()
    conn.close()


def tgm_bump_job(customer_id, job_id, *, sent: int = 0, failed: int = 0,
                 skipped: int = 0) -> None:
    cid = _require_cid(customer_id)
    conn = _conn()
    conn.execute(
        "UPDATE tg_multi_jobs SET sent_count = sent_count + ?, "
        "failed_count = failed_count + ?, skipped_count = skipped_count + ?, "
        "updated_at = ? WHERE job_id = ? AND customer_id = ?",
        (int(sent), int(failed), int(skipped), _now(), str(job_id), cid))
    conn.commit()
    conn.close()


def tgm_list_jobs(customer_id, limit: int = 10) -> list:
    cid = _require_cid(customer_id)
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT * FROM tg_multi_jobs WHERE customer_id = ? "
        "ORDER BY created_at DESC LIMIT ?", (cid, int(limit))))
    conn.close()
    return rows


def owner_tgm_unfinished() -> list:
    """Jobs that were still running when the process stopped.

    Read once at boot to resume them and to re-register their accounts in the
    busy registry.
    """
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT * FROM tg_multi_jobs WHERE state IN "
        "('queued', 'running', 'waiting', 'stop_requested') ORDER BY created_at"))
    conn.close()
    for row in rows:
        try:
            row["content"] = json.loads(row.get("content_json") or "[]")
        except (TypeError, ValueError):
            row["content"] = []
    return rows


def tgm_add_account(customer_id, job_id, account_id, phone: str,
                    ordinal: int) -> None:
    cid = _require_cid(customer_id)
    conn = _conn()
    conn.execute(
        "INSERT OR IGNORE INTO tg_multi_accounts (job_id, customer_id, "
        "account_id, phone, ordinal) VALUES (?, ?, ?, ?, ?)",
        (str(job_id), cid, int(account_id), phone, int(ordinal)))
    conn.commit()
    conn.close()


def tgm_job_accounts(customer_id, job_id) -> list:
    cid = _require_cid(customer_id)
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT * FROM tg_multi_accounts WHERE job_id = ? AND customer_id = ? "
        "ORDER BY ordinal", (str(job_id), cid)))
    conn.close()
    return rows


def tgm_update_account(customer_id, job_id, account_id, **fields) -> None:
    cid = _require_cid(customer_id)
    allowed = {"state", "total", "sent_count", "failed_count", "consec_fail",
               "last_error"}
    sets, params = [], []
    for key, value in fields.items():
        if key in allowed:
            sets.append(f"{key} = ?")
            params.append(value)
    if not sets:
        return
    params += [str(job_id), int(account_id), cid]
    conn = _conn()
    conn.execute(
        f"UPDATE tg_multi_accounts SET {', '.join(sets)} "
        "WHERE job_id = ? AND account_id = ? AND customer_id = ?", params)
    conn.commit()
    conn.close()


def tgm_add_recipients(customer_id, job_id, account_id, targets: list,
                       start_idx: int = 0) -> int:
    """Queue one account's own recipients. `targets` is [(key, payload, mutual)]."""
    cid = _require_cid(customer_id)
    if not targets:
        return start_idx
    conn = _conn()
    c = conn.cursor()
    idx = start_idx
    for key, payload, mutual in targets:
        c.execute(
            "INSERT OR IGNORE INTO tg_multi_recipients (job_id, idx, customer_id, "
            "account_id, target_key, target_json, mutual) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(job_id), idx, cid, int(account_id), str(key),
             json.dumps(payload, ensure_ascii=False), 1 if mutual else 0))
        idx += 1
    conn.commit()
    conn.close()
    return idx


def tgm_pending_recipients(customer_id, job_id, account_id,
                           limit: int = 500) -> list:
    """The next pending recipients for one account, mutuals first.

    Mutual contacts are people who added the account back, so they are the least
    likely to report a message — reaching them first makes the account survive
    longer into the run.
    """
    cid = _require_cid(customer_id)
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT * FROM tg_multi_recipients WHERE job_id = ? AND account_id = ? "
        "AND customer_id = ? AND state = 'pending' "
        "ORDER BY mutual DESC, idx LIMIT ?",
        (str(job_id), int(account_id), cid, int(limit))))
    conn.close()
    for row in rows:
        try:
            row["target"] = json.loads(row.get("target_json") or "null")
        except (TypeError, ValueError):
            row["target"] = None
    return rows


def tgm_set_recipient(customer_id, job_id, idx, state: str,
                      error: str = "") -> None:
    cid = _require_cid(customer_id)
    conn = _conn()
    conn.execute(
        "UPDATE tg_multi_recipients SET state = ?, attempts = attempts + 1, "
        "last_error = ? WHERE job_id = ? AND idx = ? AND customer_id = ?",
        (state, error or "", str(job_id), int(idx), cid))
    conn.commit()
    conn.close()


def tgm_mark_uid_sent(customer_id, job_id, uid) -> None:
    _require_cid(customer_id)
    conn = _conn()
    conn.execute("INSERT OR IGNORE INTO tg_multi_sent (job_id, uid) VALUES (?, ?)",
                 (str(job_id), str(uid)))
    conn.commit()
    conn.close()


def tgm_uid_already_sent(customer_id, job_id, uid) -> bool:
    """Has anybody in this job already reached this person?

    This is what stops a customer with five accounts from messaging every shared
    contact five times.
    """
    _require_cid(customer_id)
    conn = _conn()
    row = _row(conn.execute(
        "SELECT 1 AS x FROM tg_multi_sent WHERE job_id = ? AND uid = ?",
        (str(job_id), str(uid))))
    conn.close()
    return bool(row)


def tgm_counts(customer_id, job_id) -> dict:
    cid = _require_cid(customer_id)
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT state, COUNT(*) AS n FROM tg_multi_recipients "
        "WHERE job_id = ? AND customer_id = ? GROUP BY state",
        (str(job_id), cid)))
    conn.close()
    return {r["state"]: int(r["n"]) for r in rows}


def tgm_counts_per_account(customer_id, job_id) -> dict:
    """{account_id: {state: n}} — live, straight from the recipients table.

    The tg_multi_accounts row only gets its sent_count when the account FINISHES,
    so a per-account line built from it reads 0 for the whole run. This is the
    same reason the job-level counters could not drive the progress card.
    """
    cid = _require_cid(customer_id)
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT account_id, state, COUNT(*) AS n FROM tg_multi_recipients "
        "WHERE job_id = ? AND customer_id = ? GROUP BY account_id, state",
        (str(job_id), cid)))
    conn.close()
    out: dict = {}
    for row in rows:
        out.setdefault(int(row["account_id"]), {})[row["state"]] = int(row["n"])
    return out


def tgm_delete_job(customer_id, job_id) -> None:
    cid = _require_cid(customer_id)
    conn = _conn()
    c = conn.cursor()
    for table in ("tg_multi_recipients", "tg_multi_accounts", "tg_multi_jobs"):
        c.execute(f"DELETE FROM {table} WHERE job_id = ? AND customer_id = ?",
                  (str(job_id), cid))
    c.execute("DELETE FROM tg_multi_sent WHERE job_id = ?", (str(job_id),))
    conn.commit()
    conn.close()

"""
central_db.py — the OWNER-ONLY database (data/central.db).
=========================================================

This is the second of the project's two logically separate databases. It holds
data that belongs purely to the owner and that the customer bot must never be
able to read:

  * owner_state  — maintenance flag, last backup time, shield bookkeeping
  * broadcasts   — a record of every broadcast the owner sent
  * audit_log    — every privileged action (grant time, block, broadcast,
                   add/remove worker, backup, freeze sends, shield on/off)
  * tickets      — support messages relayed from customers, with the reply

THE CUSTOMER PROCESS NEVER IMPORTS THIS MODULE. That is the isolation: not a
permission check that can be forgotten, but a file the other process does not
open. The only thing the customer bot needs to know about owner state is
"maintenance on/off", and it learns that from a tiny flag file (see
maintenance_flag_path) instead of reading this database.
"""
from __future__ import annotations

import os
import sqlite3

import config

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "data", "central.db")


def _now() -> str:
    return config.now_str()


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    return conn


def _rows(cur) -> list:
    return [dict(r) for r in cur.fetchall()]


def _row(cur):
    r = cur.fetchone()
    return dict(r) if r else None


def init() -> None:
    conn = _conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS owner_state (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            maintenance INTEGER DEFAULT 0,
            notice      TEXT DEFAULT '',
            last_backup TEXT DEFAULT ''
        )
    """)
    c.execute("INSERT OR IGNORE INTO owner_state (id, maintenance, notice) "
              "VALUES (1, ?, '')",
              (1 if config.MAINTENANCE_DEFAULT else 0,))
    c.execute("""
        CREATE TABLE IF NOT EXISTS broadcasts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            text       TEXT,
            audience   TEXT DEFAULT 'all',
            queued     INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            action     TEXT,
            detail     TEXT,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            text        TEXT,
            answered    INTEGER DEFAULT 0,
            answer      TEXT DEFAULT '',
            created_at  TEXT,
            answered_at TEXT DEFAULT ''
        )
    """)
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- #
# Maintenance mode, mirrored to a flag file
# --------------------------------------------------------------------------- #
def maintenance_flag_path() -> str:
    return os.path.join(os.path.dirname(DB_PATH), "maintenance.flag")


def get_maintenance() -> bool:
    conn = _conn()
    row = _row(conn.execute("SELECT maintenance FROM owner_state WHERE id = 1"))
    conn.close()
    return bool(row["maintenance"]) if row else False


def get_notice() -> str:
    conn = _conn()
    row = _row(conn.execute("SELECT notice FROM owner_state WHERE id = 1"))
    conn.close()
    return (row["notice"] if row else "") or ""


def set_notice(text: str) -> None:
    conn = _conn()
    conn.execute("UPDATE owner_state SET notice = ? WHERE id = 1", (text or "",))
    conn.commit()
    conn.close()


def set_maintenance(on: bool) -> None:
    """Flip maintenance mode and mirror it to a flag file.

    The mirror is what lets the customer bot honour maintenance WITHOUT opening
    this owner-only database: it just checks whether the file exists.
    """
    conn = _conn()
    conn.execute("UPDATE owner_state SET maintenance = ? WHERE id = 1",
                 (1 if on else 0,))
    conn.commit()
    conn.close()
    path = maintenance_flag_path()
    try:
        if on:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(get_notice() or "1")
        elif os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Backup bookkeeping
# --------------------------------------------------------------------------- #
def get_last_backup() -> str:
    conn = _conn()
    row = _row(conn.execute("SELECT last_backup FROM owner_state WHERE id = 1"))
    conn.close()
    return (row["last_backup"] if row else "") or ""


def set_last_backup(ts: str = None) -> None:
    conn = _conn()
    conn.execute("UPDATE owner_state SET last_backup = ? WHERE id = 1",
                 (ts or _now(),))
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- #
# Broadcast history
# --------------------------------------------------------------------------- #
def record_broadcast(text: str, audience: str, queued: int) -> int:
    conn = _conn()
    c = conn.cursor()
    c.execute("INSERT INTO broadcasts (text, audience, queued, created_at) "
              "VALUES (?, ?, ?, ?)",
              (text or "", audience or "all", int(queued), _now()))
    conn.commit()
    bid = c.lastrowid
    conn.close()
    return int(bid)


def list_broadcasts(limit: int = 20) -> list:
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT * FROM broadcasts ORDER BY id DESC LIMIT ?", (int(limit),)))
    conn.close()
    return rows


# --------------------------------------------------------------------------- #
# Audit log — who did what, so a decision six months old is still explainable
# --------------------------------------------------------------------------- #
def audit(action: str, detail: str = "") -> None:
    conn = _conn()
    conn.execute("INSERT INTO audit_log (action, detail, created_at) "
                 "VALUES (?, ?, ?)", (action or "", detail or "", _now()))
    conn.commit()
    conn.close()


def list_audit(limit: int = 50) -> list:
    conn = _conn()
    rows = _rows(conn.execute(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (int(limit),)))
    conn.close()
    return rows


# --------------------------------------------------------------------------- #
# Support tickets (customer -> owner, with the reply relayed back)
# --------------------------------------------------------------------------- #
def add_ticket(customer_id: int, text: str) -> int:
    conn = _conn()
    c = conn.cursor()
    c.execute("INSERT INTO tickets (customer_id, text, created_at) "
              "VALUES (?, ?, ?)", (int(customer_id), text or "", _now()))
    conn.commit()
    tid = c.lastrowid
    conn.close()
    return int(tid)


def get_ticket(ticket_id) -> dict | None:
    conn = _conn()
    row = _row(conn.execute("SELECT * FROM tickets WHERE id = ?", (int(ticket_id),)))
    conn.close()
    return row


def answer_ticket(ticket_id, answer: str) -> None:
    conn = _conn()
    conn.execute("UPDATE tickets SET answered = 1, answer = ?, answered_at = ? "
                 "WHERE id = ?", (answer or "", _now(), int(ticket_id)))
    conn.commit()
    conn.close()


def list_tickets(only_open: bool = True, limit: int = 30) -> list:
    sql = "SELECT * FROM tickets"
    if only_open:
        sql += " WHERE answered = 0"
    sql += " ORDER BY id DESC LIMIT ?"
    conn = _conn()
    rows = _rows(conn.execute(sql, (int(limit),)))
    conn.close()
    return rows


def count_open_tickets() -> int:
    conn = _conn()
    row = _row(conn.execute("SELECT COUNT(*) AS n FROM tickets WHERE answered = 0"))
    conn.close()
    return int(row["n"]) if row else 0

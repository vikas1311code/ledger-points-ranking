"""
database.py
-----------
Thin SQLite wrapper for the Transactions/Ranking service.

Design notes
============
- SQLite is used as a single-file embedded DB to keep the assignment easy to
  run with zero external dependencies (no docker/postgres needed).
- WAL (Write-Ahead Logging) journal mode is enabled. This lets reads happen
  concurrently with a write, and SQLite itself serializes writers, which is
  exactly the guarantee we need for "safe handling of simultaneous requests"
  at this scale.
- `transactions.idempotency_key` has a UNIQUE constraint. This is the actual
  mechanism that prevents duplicate processing: if two identical requests
  (same client-generated key) race each other, only one INSERT can succeed;
  the other hits a UNIQUE constraint violation and we treat it as "already
  processed" and return the original result instead of double-applying it.
- All balance mutations happen inside a single SQLite transaction together
  with the INSERT into `transactions`, so a crash or error can never leave
  the ledger (transactions table) and the materialized balance (users table)
  out of sync. The users table is a denormalized/cached aggregate that can
  always be rebuilt from the transactions table -- it exists purely for
  O(1) reads on /summary and /ranking instead of SUM()-ing on every request.
"""

import sqlite3
import os
import threading

DB_PATH = os.path.join(os.path.dirname(__file__), "app.db")

# A per-user lock map. SQLite already serializes writers at the file level,
# but we additionally take an in-process lock keyed by user_id so that
# "read current balance -> validate business rule -> write new balance"
# read-modify-write sequences for the *same* user are atomic even before
# they reach SQLite. This removes a class of race conditions (e.g. rate
# limit counting) that a single SQL UPDATE statement alone can't fully
# express. Different users never block each other.
_user_locks_guard = threading.Lock()
_user_locks = {}


def get_user_lock(user_id: str) -> threading.Lock:
    with _user_locks_guard:
        if user_id not in _user_locks:
            _user_locks[user_id] = threading.Lock()
        return _user_locks[user_id]


def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.row_factory = sqlite3.Row
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id            TEXT PRIMARY KEY,
    total_points       INTEGER NOT NULL DEFAULT 0,
    transaction_count  INTEGER NOT NULL DEFAULT 0,
    active_days_count  INTEGER NOT NULL DEFAULT 0,
    last_transaction_at TEXT,
    last_active_day    TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transactions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key    TEXT NOT NULL UNIQUE,
    user_id            TEXT NOT NULL,
    type               TEXT NOT NULL CHECK(type IN ('earn','bonus','purchase','adjustment')),
    points             INTEGER NOT NULL,
    status             TEXT NOT NULL DEFAULT 'applied' CHECK(status IN ('applied','rejected')),
    rejection_reason   TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_transactions_created ON transactions(created_at);

-- Distinct active days per user, used as the "consistency" ranking factor.
CREATE TABLE IF NOT EXISTS user_active_days (
    user_id     TEXT NOT NULL,
    day         TEXT NOT NULL,
    PRIMARY KEY (user_id, day)
);
"""


def init_db():
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()

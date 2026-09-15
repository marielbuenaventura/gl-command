"""
GL-Command — persistence layer.

A single SQLite database holds every module's state.  Two design rules matter
for an accounting system and are enforced here rather than in the UI:

1. **Nothing is deleted.**  Rows carry status fields; reversals are new rows.
2. **Every mutation is journalled.**  ``glcmd.audit`` appends a hash-chained
   record for each write, so the audit trail can be proven un-tampered.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from . import config

SCHEMA = """
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- reference
CREATE TABLE IF NOT EXISTS accounts (
    account_no      TEXT PRIMARY KEY,
    description     TEXT NOT NULL,
    account_type    TEXT NOT NULL,           -- Asset/Liability/Equity/Revenue/Expense
    statement       TEXT NOT NULL,           -- BS / P&L
    recon_required  INTEGER NOT NULL DEFAULT 0,
    owner           TEXT,
    ifrs_standard   TEXT
);

CREATE TABLE IF NOT EXISTS fx_rates (
    period          TEXT NOT NULL,           -- YYYY-MM
    currency        TEXT NOT NULL,
    closing_rate    REAL NOT NULL,           -- units of currency per 1 USD
    average_rate    REAL NOT NULL,
    PRIMARY KEY (period, currency)
);

-- ------------------------------------------------------ module 1: roadmap
CREATE TABLE IF NOT EXISTS milestones (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    phase           TEXT NOT NULL,
    workstream      TEXT NOT NULL,
    objective       TEXT NOT NULL,
    success_measure TEXT,
    due_day         INTEGER,                 -- days from start date
    status          TEXT NOT NULL DEFAULT 'Not started',
    progress_pct    INTEGER NOT NULL DEFAULT 0,
    owner           TEXT,
    evidence        TEXT,
    notes           TEXT,
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key             TEXT PRIMARY KEY,
    value           TEXT
);

-- -------------------------------------------------- module 2: close & GL
CREATE TABLE IF NOT EXISTS close_periods (
    period          TEXT PRIMARY KEY,        -- YYYY-MM
    period_type     TEXT NOT NULL,           -- Month / Quarter / Year
    close_start     TEXT NOT NULL,           -- ISO date of WD+1
    target_close    TEXT NOT NULL,           -- ISO date of WD+3
    actual_close    TEXT,
    status          TEXT NOT NULL DEFAULT 'Open'
);

CREATE TABLE IF NOT EXISTS close_tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    period          TEXT NOT NULL REFERENCES close_periods(period),
    workday         TEXT NOT NULL,           -- WD-2 .. WD+3
    stream          TEXT NOT NULL,
    task            TEXT NOT NULL,
    entity          TEXT,
    owner           TEXT,
    reviewer        TEXT,
    control_ref     TEXT,
    status          TEXT NOT NULL DEFAULT 'Not started',
    completed_at    TEXT,
    blocker         TEXT
);

CREATE TABLE IF NOT EXISTS journal_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    je_ref          TEXT UNIQUE NOT NULL,
    period          TEXT NOT NULL,
    entity          TEXT NOT NULL,
    description     TEXT NOT NULL,
    je_type         TEXT NOT NULL,           -- Accrual / Reclass / Correction / Standard / Reversal
    risk_tag        TEXT NOT NULL,
    framework       TEXT NOT NULL DEFAULT 'IFRS',
    currency        TEXT NOT NULL,
    total_debit     REAL NOT NULL DEFAULT 0,
    total_credit    REAL NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'Draft',
    preparer        TEXT NOT NULL,
    reviewer        TEXT,
    controller      TEXT,
    prepared_at     TEXT,
    reviewed_at     TEXT,
    approved_at     TEXT,
    posted_at       TEXT,
    reversal_of     TEXT,
    rejection_note  TEXT,
    support_count   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS journal_lines (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    je_ref          TEXT NOT NULL REFERENCES journal_entries(je_ref),
    line_no         INTEGER NOT NULL,
    account_no      TEXT NOT NULL,
    cost_center     TEXT,
    debit           REAL NOT NULL DEFAULT 0,
    credit          REAL NOT NULL DEFAULT 0,
    memo            TEXT
);

CREATE TABLE IF NOT EXISTS je_documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    je_ref          TEXT NOT NULL REFERENCES journal_entries(je_ref),
    filename        TEXT NOT NULL,
    doc_type        TEXT,
    sha256          TEXT NOT NULL,
    size_bytes      INTEGER,
    stored_path     TEXT NOT NULL,
    uploaded_by     TEXT,
    uploaded_at     TEXT
);

-- --------------------------------------------------- module 3: flux engine
CREATE TABLE IF NOT EXISTS trial_balance (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    period          TEXT NOT NULL,
    entity          TEXT NOT NULL,
    account_no      TEXT NOT NULL,
    currency        TEXT NOT NULL,
    amount_local    REAL NOT NULL,
    amount_group    REAL NOT NULL,
    budget_group    REAL NOT NULL DEFAULT 0,
    source          TEXT NOT NULL DEFAULT 'SAP FAGLL03',
    loaded_at       TEXT,
    UNIQUE (period, entity, account_no)
);

CREATE TABLE IF NOT EXISTS flux_commentary (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    period          TEXT NOT NULL,
    entity          TEXT NOT NULL,
    account_no      TEXT NOT NULL,
    basis           TEXT NOT NULL,           -- PoP / BvA
    variance_amount REAL,
    variance_pct    REAL,
    root_cause      TEXT NOT NULL,
    driver_category TEXT,
    action          TEXT,
    author          TEXT,
    reviewed_by     TEXT,
    created_at      TEXT,
    UNIQUE (period, entity, account_no, basis)
);

-- ----------------------------------------- module 4: reconciliations & FX
CREATE TABLE IF NOT EXISTS reconciliations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    period          TEXT NOT NULL,
    entity          TEXT NOT NULL,
    account_no      TEXT NOT NULL,
    recon_type      TEXT NOT NULL,           -- Balance sheet / Bank / Sub-ledger
    gl_balance      REAL NOT NULL DEFAULT 0,
    subledger_balance REAL NOT NULL DEFAULT 0,
    reconciling_items REAL NOT NULL DEFAULT 0,
    preparer        TEXT,
    reviewer        TEXT,
    status          TEXT NOT NULL DEFAULT 'Not started',
    risk_rating     TEXT NOT NULL DEFAULT 'Low',
    prepared_at     TEXT,
    reviewed_at     TEXT,
    notes           TEXT,
    UNIQUE (period, entity, account_no, recon_type)
);

CREATE TABLE IF NOT EXISTS recon_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recon_id        INTEGER NOT NULL REFERENCES reconciliations(id),
    item_ref        TEXT,
    description     TEXT,
    amount          REAL NOT NULL DEFAULT 0,
    item_date       TEXT,
    category        TEXT,                    -- Timing / Error / Unidentified / In transit
    status          TEXT NOT NULL DEFAULT 'Open',
    resolution      TEXT
);

CREATE TABLE IF NOT EXISTS intercompany (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    period          TEXT NOT NULL,
    doc_ref         TEXT NOT NULL,
    entity          TEXT NOT NULL,
    counterparty    TEXT NOT NULL,
    account_no      TEXT NOT NULL,
    currency        TEXT NOT NULL,
    amount_txn      REAL NOT NULL,           -- transaction currency
    historical_rate REAL NOT NULL,           -- rate at original booking
    booked_group    REAL NOT NULL,           -- as carried in the GL, group ccy
    posting_date    TEXT,
    description     TEXT
);

-- ------------------------------------------- module 5: compliance vault
CREATE TABLE IF NOT EXISTS ppe_register (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_no        TEXT UNIQUE NOT NULL,
    description     TEXT NOT NULL,
    entity          TEXT NOT NULL,
    asset_class     TEXT NOT NULL,
    cgu             TEXT,
    acquisition_date TEXT NOT NULL,
    cost            REAL NOT NULL,
    residual_value  REAL NOT NULL DEFAULT 0,
    useful_life_m   INTEGER,
    method          TEXT NOT NULL DEFAULT 'Straight line',  -- or 'Units of production'
    total_units     REAL,
    units_to_date   REAL,
    accum_dep       REAL NOT NULL DEFAULT 0,
    accum_impairment REAL NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'In use',
    recoverable_amt REAL,
    indicator_notes TEXT,
    last_reviewed   TEXT
);

CREATE TABLE IF NOT EXISTS inventory_valuation (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    period          TEXT NOT NULL,
    entity          TEXT NOT NULL,
    sku             TEXT NOT NULL,
    description     TEXT,
    category        TEXT,
    quantity        REAL NOT NULL,
    unit_cost       REAL NOT NULL,
    selling_price   REAL NOT NULL DEFAULT 0,
    cost_to_complete REAL NOT NULL DEFAULT 0,
    cost_to_sell    REAL NOT NULL DEFAULT 0,
    ageing_days     INTEGER NOT NULL DEFAULT 0,
    existing_provision REAL NOT NULL DEFAULT 0,
    UNIQUE (period, entity, sku)
);

CREATE TABLE IF NOT EXISTS provisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    period          TEXT NOT NULL,
    entity          TEXT NOT NULL,
    provision_ref   TEXT NOT NULL,
    category        TEXT NOT NULL,           -- Rehabilitation / Warranty / Legal / Restructuring
    description     TEXT,
    opening         REAL NOT NULL DEFAULT 0,
    additions       REAL NOT NULL DEFAULT 0,
    utilised        REAL NOT NULL DEFAULT 0,
    unused_reversed REAL NOT NULL DEFAULT 0,
    unwind_discount REAL NOT NULL DEFAULT 0,
    fx_movement     REAL NOT NULL DEFAULT 0,
    discount_rate   REAL,
    expected_settle TEXT,
    recognition_basis TEXT,                  -- Present obligation / Contingent
    evidence        TEXT,
    UNIQUE (period, entity, provision_ref)
);

CREATE TABLE IF NOT EXISTS controls (
    control_ref     TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    standard        TEXT,
    frequency       TEXT,
    assertion       TEXT,
    owner           TEXT,
    last_tested     TEXT,
    test_result     TEXT,
    evidence        TEXT
);

-- --------------------------------------------------------- audit trail
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    actor           TEXT NOT NULL,
    action          TEXT NOT NULL,
    entity_type     TEXT NOT NULL,
    entity_id       TEXT,
    control_ref     TEXT,
    payload         TEXT,
    prev_hash       TEXT NOT NULL,
    row_hash        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_tb_period ON trial_balance(period, entity);
CREATE INDEX IF NOT EXISTS ix_je_period ON journal_entries(period, entity);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_log(ts);
"""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with sane defaults for a Streamlit app."""
    path = Path(db_path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Explicit transaction so a failed multi-table write leaves nothing behind."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ------------------------------------------------------------------ helpers
def query(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> pd.DataFrame:
    """Run a SELECT and return a DataFrame (empty but typed if no rows)."""
    return pd.read_sql_query(sql, conn, params=tuple(params))


def fetch_one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    cur = conn.execute(sql, tuple(params))
    return cur.fetchone()


def scalar(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = (), default: Any = 0) -> Any:
    row = fetch_one(conn, sql, params)
    if row is None or row[0] is None:
        return default
    return row[0]


def execute(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    cur = conn.execute(sql, tuple(params))
    conn.commit()
    return cur


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = fetch_one(conn, "SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    execute(
        conn,
        "INSERT INTO settings(key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def is_seeded(conn: sqlite3.Connection) -> bool:
    return bool(scalar(conn, "SELECT COUNT(*) FROM accounts"))


def reset(db_path: Path | str | None = None) -> None:
    """Drop the database file entirely — used by ``scripts/reset.py``."""
    path = Path(db_path or config.DB_PATH)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()

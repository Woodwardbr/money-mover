from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import settings

# Plaid returns NULL security_id for cash positions. SQLite treats NULLs as
# distinct in PRIMARY KEY comparisons, so ON CONFLICT never fires and cash rows
# duplicate on every re-sync. Store this sentinel instead.
CASH_SECURITY_ID = "__cash__"

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_id        TEXT PRIMARY KEY,
    access_token   TEXT NOT NULL,
    institution    TEXT,
    cursor         TEXT,                       -- Plaid transactions_sync cursor
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS accounts (
    account_id     TEXT PRIMARY KEY,
    item_id        TEXT NOT NULL REFERENCES items(item_id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    mask           TEXT,
    kind           TEXT NOT NULL,          -- depository / credit / investment / loan
    subtype        TEXT,
    iso_currency   TEXT,
    exclude_from_net_worth INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS balances (
    account_id     TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
    snapshot_date  TEXT NOT NULL,
    current        REAL NOT NULL,
    available      REAL,
    PRIMARY KEY (account_id, snapshot_date)
);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_id  TEXT PRIMARY KEY,
    account_id      TEXT NOT NULL REFERENCES accounts(account_id),
    date            TEXT NOT NULL,
    name            TEXT NOT NULL,
    merchant        TEXT,
    amount          REAL NOT NULL,           -- positive = charge
    iso_currency    TEXT,
    category_primary TEXT,
    category_detailed TEXT,
    override_category TEXT
);

CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_tx_account ON transactions(account_id);

CREATE TABLE IF NOT EXISTS budgets (
    category        TEXT PRIMARY KEY,
    monthly_limit   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS merchant_rules (
    merchant_pattern TEXT PRIMARY KEY,   -- lowercase substring matched against merchant/name
    target_category  TEXT NOT NULL       -- friendly category label
);

CREATE TABLE IF NOT EXISTS securities (
    security_id      TEXT PRIMARY KEY,   -- Plaid security_id
    ticker           TEXT,
    name             TEXT,
    type             TEXT,               -- stock / etf / mutual fund / cash / ...
    sector           TEXT,               -- Plaid sector (Technology, Healthcare, ...)
    industry         TEXT,
    class            TEXT,               -- equity / debt / cash
    is_cash_equivalent INTEGER NOT NULL DEFAULT 0,
    manual_override  INTEGER NOT NULL DEFAULT 0  -- 1 = sync skips (Plaid mis-ID fixed)
);

CREATE TABLE IF NOT EXISTS holdings (
    account_id       TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
    security_id      TEXT REFERENCES securities(security_id),
    quantity         REAL,
    institution_price REAL,
    institution_value REAL,
    cost_basis       REAL,
    snapshot_date    TEXT NOT NULL,
    override_sector  TEXT,               -- user-set sector override (persists across syncs)
    PRIMARY KEY (account_id, security_id, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_holdings_account ON holdings(account_id);
CREATE INDEX IF NOT EXISTS idx_holdings_snapshot ON holdings(snapshot_date);

CREATE TABLE IF NOT EXISTS plan_allocations (
    account_id     TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
    label          TEXT NOT NULL,          -- fund name as shown on the plan site
    ticker         TEXT,                   -- best-known ticker (optional)
    allocation_pct REAL NOT NULL,          -- 0-100 share of the plan balance
    sector         TEXT NOT NULL,          -- portfolio sector to roll up into
    PRIMARY KEY (account_id, label)
);

CREATE TABLE IF NOT EXISTS loans (
    loan_id         TEXT PRIMARY KEY,
    name            TEXT NOT NULL,           -- e.g. "1-01 Direct Loan - Subsidized"
    loan_type       TEXT,                    -- e.g. "Direct", "Direct Grad PLUS"
    interest_rate   REAL,                    -- annual rate as a percent (e.g. 6.54)
    min_payment     REAL,                    -- monthly minimum due
    current_balance REAL,                    -- outstanding principal
    next_due_date   TEXT,                    -- ISO date of next scheduled payment
    auto_pay        INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'Scheduled',  -- Scheduled / Paid / Past Due
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS loan_payments (
    payment_id     TEXT PRIMARY KEY,
    loan_id        TEXT REFERENCES loans(loan_id) ON DELETE CASCADE,  -- NULL = combined payment
    payment_date   TEXT NOT NULL,
    amount         REAL NOT NULL,
    status         TEXT NOT NULL DEFAULT 'Received',  -- Received / Processing / Failed
    source         TEXT,                    -- e.g. "Bank Acct *7373"
    notes          TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_loan_payments_date ON loan_payments(payment_date);
CREATE INDEX IF NOT EXISTS idx_loan_payments_loan ON loan_payments(loan_id);

CREATE TABLE IF NOT EXISTS loan_balance_snapshots (
    loan_id        TEXT NOT NULL REFERENCES loans(loan_id) ON DELETE CASCADE,
    snapshot_date  TEXT NOT NULL,
    balance        REAL NOT NULL,
    PRIMARY KEY (loan_id, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_loan_snapshots_date ON loan_balance_snapshots(snapshot_date);
"""


def init_db(path: Path | None = None) -> None:
    path = path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        # Idempotent migration for DBs created before the column existed.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
        if "exclude_from_net_worth" not in cols:
            conn.execute(
                "ALTER TABLE accounts ADD COLUMN exclude_from_net_worth INTEGER NOT NULL DEFAULT 0"
            )
        sec_cols = {r[1] for r in conn.execute("PRAGMA table_info(securities)").fetchall()}
        if "manual_override" not in sec_cols:
            conn.execute(
                "ALTER TABLE securities ADD COLUMN manual_override INTEGER NOT NULL DEFAULT 0"
            )
        item_cols = {r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()}
        if "cursor" not in item_cols:
            conn.execute("ALTER TABLE items ADD COLUMN cursor TEXT")

        # Collapse pre-sentinel duplicate cash holdings, then adopt the sentinel.
        conn.execute(
            """
            DELETE FROM holdings
            WHERE security_id IS NULL
              AND rowid NOT IN (
                  SELECT MIN(rowid) FROM holdings
                  WHERE security_id IS NULL
                  GROUP BY account_id, snapshot_date
              )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO securities (security_id, name, type, is_cash_equivalent) "
            "VALUES (?, ?, 'cash', 1)",
            (CASH_SECURITY_ID, "Cash"),
        )
        conn.execute(
            "UPDATE holdings SET security_id = ? WHERE security_id IS NULL",
            (CASH_SECURITY_ID,),
        )


@contextmanager
def get_conn():
    init_db()
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

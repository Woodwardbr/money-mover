from __future__ import annotations

import sqlite3

from money_mover.db import CASH_SECURITY_ID, get_conn, init_db


def test_cash_holding_dedup(make_account):
    """Inserting the same cash holding twice for one (account_id, snapshot_date)
    leaves exactly one row."""
    make_account("inv1", kind="investment")
    with get_conn() as conn:
        # Simulate what sync.py does with the sentinel
        conn.execute(
            "INSERT INTO holdings (account_id, security_id, quantity, "
            "institution_price, institution_value, cost_basis, snapshot_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(account_id, security_id, snapshot_date) DO UPDATE SET "
            "quantity=excluded.quantity",
            ("inv1", CASH_SECURITY_ID, 1.0, 1.0, 100.0, 100.0, "2026-08-29"),
        )
        conn.execute(
            "INSERT INTO holdings (account_id, security_id, quantity, "
            "institution_price, institution_value, cost_basis, snapshot_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(account_id, security_id, snapshot_date) DO UPDATE SET "
            "quantity=excluded.quantity",
            ("inv1", CASH_SECURITY_ID, 1.0, 1.0, 100.0, 100.0, "2026-08-29"),
        )
    with get_conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM holdings "
            "WHERE account_id = ? AND security_id = ? AND snapshot_date = ?",
            ("inv1", CASH_SECURITY_ID, "2026-08-29"),
        ).fetchone()["n"]
    assert count == 1


def test_init_db_collapses_duplicate_cash():
    """init_db() run against a DB pre-seeded with two NULL-security_id rows
    for the same account and date collapses them to one and rewrites to __cash__."""
    import dataclasses
    import tempfile
    from pathlib import Path

    from money_mover import db as dbmod

    tmp = tempfile.mkdtemp()
    db_path = Path(tmp) / "test.db"
    dbmod.settings = dataclasses.replace(dbmod.settings, db_path=db_path)
    # First init without migration
    conn = sqlite3.connect(str(db_path))
    conn.executescript(dbmod.SCHEMA)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT OR IGNORE INTO items (item_id, access_token, institution) VALUES (?, ?, ?)",
        ("i1", "tok", "Bank"),
    )
    conn.execute(
        "INSERT INTO accounts (account_id, item_id, name, mask, kind, subtype, iso_currency) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("inv1", "i1", "inv1", "0000", "investment", None, "USD"),
    )
    conn.execute(
        "INSERT INTO holdings (account_id, security_id, quantity, institution_price, "
        "institution_value, cost_basis, snapshot_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("inv1", None, 1.0, 1.0, 100.0, 100.0, "2026-08-29"),
    )
    conn.execute(
        "INSERT INTO holdings (account_id, security_id, quantity, institution_price, "
        "institution_value, cost_basis, snapshot_date) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("inv1", None, 1.0, 1.0, 100.0, 100.0, "2026-08-29"),
    )
    conn.commit()
    conn.close()

    # Now run init_db which should collapse the duplicates
    init_db(db_path)
    dbmod.settings = dataclasses.replace(dbmod.settings, db_path=db_path)

    with get_conn() as conn2:
        null_count = conn2.execute(
            "SELECT COUNT(*) AS n FROM holdings WHERE security_id IS NULL"
        ).fetchone()["n"]
        assert null_count == 0

        sentinel_count = conn2.execute(
            "SELECT COUNT(*) AS n FROM holdings WHERE security_id = ?",
            (CASH_SECURITY_ID,),
        ).fetchone()["n"]
        assert sentinel_count == 1
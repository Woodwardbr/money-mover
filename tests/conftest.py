from __future__ import annotations

import dataclasses

import pytest

from money_mover import db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Point every get_conn() call in a test at a fresh throwaway SQLite file."""
    monkeypatch.setattr(
        db, "settings", dataclasses.replace(db.settings, db_path=tmp_path / "test.db")
    )
    db.init_db()
    yield


@pytest.fixture
def conn():
    """A connection to the throwaway DB, for arranging fixture rows."""
    with db.get_conn() as c:
        yield c


@pytest.fixture
def make_account(conn):
    def _make(account_id, *, kind="depository", name=None, item_id="item1",
              institution="Test Bank", exclude=0, subtype=None):
        conn.execute(
            "INSERT OR IGNORE INTO items (item_id, access_token, institution) VALUES (?, ?, ?)",
            (item_id, "tok", institution),
        )
        conn.execute(
            "INSERT INTO accounts (account_id, item_id, name, mask, kind, subtype, "
            "iso_currency, exclude_from_net_worth) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (account_id, item_id, name or account_id, "0000", kind, subtype, "USD", exclude),
        )
        return account_id
    return _make


@pytest.fixture
def make_balance(conn):
    def _make(account_id, snapshot_date, current, available=None):
        conn.execute(
            "INSERT INTO balances (account_id, snapshot_date, current, available) "
            "VALUES (?, ?, ?, ?)",
            (account_id, snapshot_date, current, available),
        )
    return _make


@pytest.fixture
def make_txn(conn):
    def _make(transaction_id, account_id, date, amount, *, name="TXN",
              merchant=None, primary=None, detailed=None, override=None):
        conn.execute(
            "INSERT INTO transactions (transaction_id, account_id, date, name, merchant, "
            "amount, iso_currency, category_primary, category_detailed, override_category) "
            "VALUES (?, ?, ?, ?, ?, ?, 'USD', ?, ?, ?)",
            (transaction_id, account_id, date, name, merchant, amount,
             primary, detailed, override),
        )
    return _make
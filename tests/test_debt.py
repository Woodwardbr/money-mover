from __future__ import annotations

from money_mover.analytics import loan_balance_history
from money_mover.db import get_conn


def make_loan(conn, loan_id, name, current_balance, interest_rate=6.0):
    conn.execute(
        "INSERT INTO loans (loan_id, name, current_balance, interest_rate) "
        "VALUES (?, ?, ?, ?)",
        (loan_id, name, current_balance, interest_rate),
    )


def make_loan_snapshot(conn, loan_id, snapshot_date, balance):
    conn.execute(
        "INSERT INTO loan_balance_snapshots (loan_id, snapshot_date, balance) "
        "VALUES (?, ?, ?)",
        (loan_id, snapshot_date, balance),
    )


def make_loan_payment(conn, payment_id, loan_id, payment_date, amount):
    conn.execute(
        "INSERT INTO loan_payments (payment_id, loan_id, payment_date, amount) "
        "VALUES (?, ?, ?, ?)",
        (payment_id, loan_id, payment_date, amount),
    )


def test_loan_balance_history_post_anchor_payment():
    """A payment after the anchor date should not shift earlier balance points."""
    with get_conn() as conn:
        make_loan(conn, "L1", "Test Loan", 10000.0)
        make_loan_snapshot(conn, "L1", "2026-08-01", 10000.0)
        make_loan_payment(conn, "p1", "L1", "2026-06-01", 500.0)
        make_loan_payment(conn, "p2", "L1", "2026-07-01", 500.0)
        make_loan_payment(conn, "p3", "L1", "2026-08-15", 500.0)

    history = loan_balance_history()
    assert len(history) >= 3

    by_date = {h["date"]: h["total"] for h in history}
    # Pre-anchor payments sum to 1000, so earliest balance = 10000 + 1000 = 11000.
    # After subtracting p1 (500): 10500 on 2026-06-01.
    # After subtracting p2 (500): 10000 on 2026-07-01.
    # Anchor: 10000 on 2026-08-01.
    # The post-anchor payment p3 (2026-08-15, 500) must NOT inflate earlier points.
    assert by_date["2026-06-01"] == 10500.0
    assert by_date["2026-07-01"] == 10000.0
    assert by_date["2026-08-01"] == 10000.0
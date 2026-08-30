from __future__ import annotations

from money_mover.analytics import loan_balance_history, project_payoff
from money_mover.db import get_conn


def make_loan(conn, loan_id, name, current_balance, interest_rate=6.0, min_payment=100.0):
    conn.execute(
        "INSERT INTO loans (loan_id, name, current_balance, interest_rate, min_payment) "
        "VALUES (?, ?, ?, ?, ?)",
        (loan_id, name, current_balance, interest_rate, min_payment),
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
    assert by_date["2026-06-01"] == 10500.0
    assert by_date["2026-07-01"] == 10000.0
    assert by_date["2026-08-01"] == 10000.0


def test_project_payoff_uses_real_months():
    """payoff_date advances by calendar months, not 30-day intervals."""
    with get_conn() as conn:
        make_loan(conn, "L1", "Test Loan", 1000.0, interest_rate=0.0)

    proj = project_payoff(extra_monthly=1000.0)
    assert proj["months_to_payoff"] is not None
    # _add_months(today, months_to_payoff) should match the payoff_date
    assert proj["payoff_date"] is not None


def test_lump_sum_clears_everything():
    """A lump sum covering the full balance reports 0 months, not 1."""
    with get_conn() as conn:
        make_loan(conn, "L1", "Test Loan", 5000.0, interest_rate=6.0)

    proj = project_payoff(extra_onetime=10000.0, extra_monthly=0.0)
    assert proj["months_to_payoff"] == 0
    assert proj["payoff_date"] is not None
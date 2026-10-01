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

def test_roll_due_date_forward():
    from datetime import date

    from money_mover.analytics import roll_due_date_forward

    assert roll_due_date_forward(date(2026, 7, 15), date(2026, 9, 30)) == date(2026, 10, 15)
    assert roll_due_date_forward(date(2026, 7, 15), date(2026, 9, 15)) == date(2026, 9, 15)
    assert roll_due_date_forward(date(2026, 10, 15), date(2026, 9, 30)) == date(2026, 10, 15)
    # Day-of-month is clamped per month, not carried forward from the clamp.
    assert roll_due_date_forward(date(2026, 1, 31), date(2026, 2, 10)) == date(2026, 2, 28)
    assert roll_due_date_forward(date(2026, 1, 31), date(2026, 3, 1)) == date(2026, 3, 31)
    assert roll_due_date_forward(date(2025, 11, 30), date(2026, 1, 5)) == date(2026, 1, 30)


def test_list_loans_rolls_only_autopay_due_dates():
    from datetime import date

    from money_mover.analytics import list_loans

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO loans (loan_id, name, next_due_date, auto_pay) "
            "VALUES ('a', 'Auto', '2020-01-15', 1), ('m', 'Manual', '2020-01-15', 0)"
        )
    loans = {ln.loan_id: ln for ln in list_loans()}
    assert loans["a"].next_due_date >= date.today()
    assert loans["a"].next_due_date.day == 15
    assert loans["m"].next_due_date == date(2020, 1, 15)

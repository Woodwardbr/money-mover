from __future__ import annotations

import pytest
from fastapi import HTTPException

from money_mover import analytics
from money_mover.api import _parse_period
from money_mover.db import get_conn


def test_budget_progress_out_of_range_month():
    """budget_progress('2026-13') should return [] rather than raising."""
    result = analytics.budget_progress("2026-13")
    assert result == []


def test_budget_progress_valid_month(make_account, make_txn):
    """budget_progress for a valid month returns correct spending."""
    make_account("a1", kind="depository")
    make_txn("t1", "a1", "2026-06-15", 42.0, primary="FOOD_AND_DRINK",
             detailed="FOOD_AND_DRINK_RESTAURANT")
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO budgets (category, monthly_limit) VALUES (?, ?)",
            ("Restaurants", 200.0),
        )

    result = analytics.budget_progress("2026-06")
    categories = {r.category: r for r in result}
    assert "Restaurants" in categories
    assert categories["Restaurants"].spent_so_far == 42.0


def test_parse_period_out_of_range_month():
    """_parse_period('2026-13') raises HTTPException with status 400."""
    with pytest.raises(HTTPException) as exc:
        _parse_period("2026-13")
    assert exc.value.status_code == 400


def test_budget_progress_no_longer_contains_raw_int_parsing():
    """Verify the function no longer contains manual int(period[:4]) parsing."""
    import inspect
    source = inspect.getsource(analytics.budget_progress)
    assert "int(period[:4])" not in source
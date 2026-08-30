from __future__ import annotations

from money_mover.analytics import portfolio_by_sector


def test_portfolio_includes_older_synced_account(make_account, make_balance):
    """A depository account last synced a week ago still appears in
    portfolio_by_sector totals."""
    make_account("chk1", kind="depository", institution="Bank A")
    make_account("chk2", kind="depository", institution="Bank B")
    make_balance("chk1", "2026-08-20", 500.0)
    make_balance("chk2", "2026-08-29", 300.0)

    sectors = portfolio_by_sector()
    cash = [s for s in sectors if s.sector == "Cash"]
    assert len(cash) == 1
    # Both accounts should contribute: 500 + 300 = 800
    assert cash[0].total == 800.0
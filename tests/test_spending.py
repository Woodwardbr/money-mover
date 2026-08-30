from __future__ import annotations

from money_mover.analytics import spending_by_friendly_category, spending_for_period


def test_no_transfer_dedup(make_account, make_txn):
    """A debit-card charge and a credit-card charge of the same amount on
    different days should both appear in spending totals."""
    make_account("chk", kind="depository", institution="Bank A")
    make_account("cc", kind="credit", institution="CreditCo", item_id="item2")
    make_txn("t1", "chk", "2026-06-10", 42.0, name="Debit Purchase",
             primary="GENERAL_MERCHANDISE")
    make_txn("t2", "cc", "2026-06-12", 42.0, name="Credit Purchase",
             primary="GENERAL_MERCHANDISE")

    rng = spending_for_period("2026-06")
    result = spending_by_friendly_category(*rng)
    merchandise = [c for c in result if c.category == "Merchandise"]
    assert len(merchandise) == 1
    assert merchandise[0].total == 84.0
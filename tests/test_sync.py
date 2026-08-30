from __future__ import annotations

from unittest.mock import patch

from money_mover import plaid, sync
from money_mover.db import get_conn


def test_link_item_twice_yields_one_row():
    """Linking the same item twice yields one items row."""
    with patch.object(sync, "sync_item", return_value={"accounts": 0, "transactions": 0}):
        with patch.object(plaid, "exchange_public_token") as mock_exchange:
            mock_exchange.return_value = plaid.LinkedItem(
                item_id="real-plaid-item-1",
                access_token="access-sandbox-abc",
            )
            with patch.object(plaid, "get_accounts", return_value=[]):
                with patch.object(plaid, "get_transactions", return_value=([], [], "")):
                    with patch.object(plaid, "get_holdings", return_value=([], [])):
                        sync.link_item("public-token-1", institution="Test Bank")
                        sync.link_item("public-token-1", institution="Test Bank")

    with get_conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM items WHERE item_id = ?",
            ("real-plaid-item-1",),
        ).fetchone()["n"]
    assert count == 1


def test_linked_item_dataclass():
    """LinkedItem holds both item_id and access_token."""
    item = plaid.LinkedItem(item_id="abc", access_token="tok123")
    assert item.item_id == "abc"
    assert item.access_token == "tok123"
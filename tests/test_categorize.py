from __future__ import annotations

from money_mover.categorize import friendly_category


def test_merchant_keyword_beats_plaid_rule():
    """A merchant keyword rule should take precedence over Plaid's category."""
    result = friendly_category(
        primary="GENERAL_SERVICES",
        detailed=None,
        merchant="Tesla Supercharger",
    )
    assert result == "Tesla Charging"


def test_plaid_detailed_mapping():
    """A Plaid detailed category should map to the correct friendly label."""
    result = friendly_category(
        primary="FOOD_AND_DRINK",
        detailed="FOOD_AND_DRINK_RESTAURANT",
    )
    assert result == "Restaurants"


def test_other_fallback():
    """Unrecognized categories should fall back to 'Other'."""
    result = friendly_category(
        primary="SOMETHING_UNKNOWN",
        detailed=None,
    )
    assert result == "Other"
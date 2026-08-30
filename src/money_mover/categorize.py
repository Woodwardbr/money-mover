from __future__ import annotations

# Maps Plaid's personal_finance_category (primary, or primary_detailed) to
# user-facing budget labels. The first matching rule wins; merchant-keyword
# overrides take precedence over the Plaid-derived label so things like
# Tesla Supercharging get bucketed correctly even when Plaid files them
# under GENERAL_SERVICES or OTHER.
FRIENDLY_CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    # Merchant / transaction-name keyword overrides
    (
        "Tesla Charging",
        (
            "tesla us ev charging",
            "tesla supercharger",
            "powerflex",
            "evgo",
            "electrify america",
            "chargepoint",
        ),
    ),
    (
        "Subscriptions",
        (
            "amazon prime",
            "netflix",
            "spotify",
            "hulu",
            "disney",
            "adobe",
            "google storage",
            "icloud",
            "apple one",
            "youtube premium",
            "chatgpt",
            "openai",
        ),
    ),
    (
        "Insurance",
        ("tesla insurance", "allstate", "geico", "progressive", "state farm", "liberty mutual"),
    ),
    ("Rent", ("bilt housing", "bilt rent")),
    # Plaid primary → friendly
    ("Rent", ("RENT_AND_UTILITIES_RENT",)),
    (
        "Utilities",
        (
            "RENT_AND_UTILITIES_MORTGAGE",
            "RENT_AND_UTILITIES_UTILITIES",
            "RENT_AND_UTILITIES_GAS_AND_FUEL",
            "RENT_AND_UTILITIES_WATER",
            "RENT_AND_UTILITIES_ELECTRICITY",
            "RENT_AND_UTILITIES_INTERNET",
            "RENT_AND_UTILITIES_TELEPHONE",
            "RENT_AND_UTILITIES_TRASH",
            "RENT_AND_UTILITIES_HOME_MAINTENANCE",
            "RENT_AND_UTILITIES_LANDSCAPING",
        ),
    ),
    ("Groceries", ("FOOD_AND_DRINK_GROCERIES",)),
    (
        "Restaurants",
        (
            "FOOD_AND_DRINK_RESTAURANT",
            "FOOD_AND_DRINK_FAST_FOOD",
            "FOOD_AND_DRINK_COFFEE",
            "FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR",
            "FOOD_AND_DRINK_FOOD_DELIVERY_SERVICES",
        ),
    ),
    ("Merchandise", ("GENERAL_MERCHANDISE",)),
    ("Transportation", ("TRANSPORTATION",)),
    ("Insurance", ("GENERAL_SERVICES_INSURANCE",)),
    ("Auto & Maintenance", ("GENERAL_SERVICES_AUTOMOTIVE",)),
    ("Health & Fitness", ("PERSONAL_CARE", "HEALTH")),
    ("Bank Fees", ("BANK_FEES",)),
    ("General Services", ("GENERAL_SERVICES",)),
    ("Travel", ("TRAVEL",)),
    ("Loans", ("LOAN_PAYMENTS",)),
    ("Income", ("INCOME",)),
    # Fallbacks keyed on Plaid primary alone (used when detailed is missing)
    ("Rent", ("RENT_AND_UTILITIES",)),
    ("Food & Drink", ("FOOD_AND_DRINK",)),
]


def friendly_category(
    primary: str | None,
    detailed: str | None,
    merchant: str | None = None,
    name: str | None = None,
) -> str:
    """Return a user-facing category label for a transaction.

    Merchant/name keyword rules take precedence over the Plaid-derived label,
    then detailed, then primary, then "Other".
    """
    haystack_parts = [p for p in (merchant, name) if p]
    haystack = " ".join(haystack_parts).lower()

    for label, keys in FRIENDLY_CATEGORY_RULES:
        if all(k.isupper() for k in keys):
            # Plaid-derived rule: match against detailed / primary
            token = (detailed or "") + " " + (primary or "")
            if any(k in token for k in keys):
                return label
        else:
            # Merchant/name keyword rule
            if any(k in haystack for k in keys):
                return label
    return "Other"

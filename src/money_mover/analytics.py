from __future__ import annotations

import calendar
import sqlite3
from collections import defaultdict
from datetime import date, timedelta

from . import categorize
from .db import get_conn
from .models import (
    AssetDetail,
    BudgetProgress,
    CategorySpend,
    DebtLoan,
    LoanPayment,
    NetWorthPoint,
    SectorAllocation,
    Subscription,
    TransactionDetail,
)

ASSET_KINDS = {"depository", "investment"}
LIABILITY_KINDS = {"credit", "loan"}

# Every account's most recent balance row, regardless of whether other accounts
# were synced more recently. Prepend to a query and select FROM latest_balances
# in place of `balances`.
LATEST_BALANCES_CTE = """
WITH latest_balances AS (
    SELECT b.*
    FROM balances b
    JOIN (
        SELECT account_id, MAX(snapshot_date) AS snapshot_date
        FROM balances
        GROUP BY account_id
    ) m ON m.account_id = b.account_id AND m.snapshot_date = b.snapshot_date
)
"""

# Categories that represent balance-sheet shifts (cash → liability reduction,
# transfers between own accounts, ATM withdrawals) rather than consumption.
# Excluded from spending totals to avoid double-counting credit-card payments
# that are already captured on the credit-card side as purchases.
EXCLUDED_SPEND_CATEGORIES = frozenset(
    {
        "LOAN_PAYMENTS",
        "TRANSFER_OUT",
        "TRANSFER_IN",
        "INCOME",
    }
)

_DEPOSITORY_KINDS = {"depository"}
_TRANSFER_WINDOW_DAYS = 3


def net_worth_series() -> list[NetWorthPoint]:
    """Net worth computed from daily balance snapshots across all accounts.

    Accounts flagged ``exclude_from_net_worth`` (e.g. unvested RSUs you don't
    yet own) are kept out of ``assets``/``net`` but still contribute to
    ``potential_net`` so the user can see the would-be figure as those holdings
    vest and move into real accounts.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT b.account_id, b.snapshot_date, b.current,
                   a.kind, a.exclude_from_net_worth
            FROM balances b
            JOIN accounts a USING (account_id)
            ORDER BY b.snapshot_date
            """
        ).fetchall()

    # Walk snapshot dates in order, carrying each account's last known balance
    # forward so a date where only one institution synced doesn't zero out the
    # others.
    latest: dict[str, dict] = {}
    points: list[NetWorthPoint] = []
    for snapshot_date in sorted({r["snapshot_date"] for r in rows}):
        for r in rows:
            if r["snapshot_date"] == snapshot_date:
                latest[r["account_id"]] = {
                    "current": float(r["current"] or 0.0),
                    "kind": r["kind"],
                    "excluded": bool(r["exclude_from_net_worth"]),
                }
        assets = sum(
            v["current"] for v in latest.values()
            if v["kind"] in ASSET_KINDS and not v["excluded"]
        )
        liabilities = sum(
            v["current"] for v in latest.values() if v["kind"] in LIABILITY_KINDS
        )
        excluded = sum(
            v["current"] for v in latest.values()
            if v["kind"] in ASSET_KINDS and v["excluded"]
        )
        net = assets - liabilities
        points.append(
            NetWorthPoint(
                date=date.fromisoformat(snapshot_date),
                assets=assets,
                liabilities=liabilities,
                net=net,
                potential_net=net + excluded,
            )
        )

    if not points:
        # Seed an empty starting point so the chart renders.
        today = date.today()
        points.append(
            NetWorthPoint(date=today, assets=0.0, liabilities=0.0, net=0.0, potential_net=0.0)
        )
    return points


def _effective_category(override: str | None, primary: str | None) -> str:
    return (override or primary or "Uncategorized") or "Uncategorized"


def _spend_rows(conn, start: date, end: date) -> list[dict]:
    """Transactions representing true consumption in [start, end].

    Excludes balance-sheet-shift categories (LOAN_PAYMENTS, TRANSFER_OUT, etc.)
    and drops the depository side of any inter-account transfer pair (same
    absolute amount, within ±3 days, across a depository + credit/loan pair),
    keeping the credit/loan side where the actual purchase was recorded.
    """
    rows = conn.execute(
        """
        SELECT t.transaction_id, t.account_id, t.date, t.amount,
               t.name, t.merchant, t.category_primary, t.category_detailed,
               t.override_category, a.kind AS account_kind, a.name AS account_name
        FROM transactions t
        JOIN accounts a USING (account_id)
        WHERE t.date >= ? AND t.date <= ? AND t.amount > 0
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()

    filtered = [
        r
        for r in rows
        if _effective_category(r["override_category"], r["category_primary"])
        not in EXCLUDED_SPEND_CATEGORIES
    ]

    drop_ids = _transfer_duplicates(filtered)
    return [dict(r) for r in filtered if r["transaction_id"] not in drop_ids]


def _transfer_duplicates(rows: list) -> set[str]:
    """Return transaction_ids to drop as the depository side of a transfer pair."""
    by_amount: dict[float, list] = defaultdict(list)
    for r in rows:
        by_amount[round(abs(r["amount"]), 2)].append(r)

    drop: set[str] = set()
    for group in by_amount.values():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if a["account_id"] == b["account_id"]:
                    continue
                try:
                    da = date.fromisoformat(a["date"])
                    db = date.fromisoformat(b["date"])
                except (TypeError, ValueError):
                    continue
                if abs((da - db).days) > _TRANSFER_WINDOW_DAYS:
                    continue
                ka, kb = a["account_kind"], b["account_kind"]
                if ka in _DEPOSITORY_KINDS and kb in LIABILITY_KINDS:
                    drop.add(a["transaction_id"])
                elif kb in _DEPOSITORY_KINDS and ka in LIABILITY_KINDS:
                    drop.add(b["transaction_id"])
    return drop


def _load_merchant_rules(conn) -> list[tuple[str, str]]:
    """Load all (merchant_pattern, target_category) rules. Patterns are stored
    lowercase; matching is substring against merchant or name (lowercased)."""
    return [
        (r["merchant_pattern"], r["target_category"])
        for r in conn.execute("SELECT merchant_pattern, target_category FROM merchant_rules")
    ]


def _row_friendly_category(r: dict, merchant_rules: list[tuple[str, str]] | None = None) -> str:
    """Friendly category for a spend row.

    Priority (highest first):
      1. per-transaction ``override_category`` (manual one-off reclassify)
      2. merchant rules (persistent vendor → category reclassify)
      3. ``categorize.friendly_category`` (keyword + Plaid mapping)
    """
    if r.get("override_category"):
        return r["override_category"]
    if merchant_rules:
        haystack_parts = [p for p in (r.get("merchant"), r.get("name")) if p]
        haystack = " ".join(haystack_parts).lower()
        for pattern, target in merchant_rules:
            if pattern in haystack:
                return target
    return categorize.friendly_category(
        primary=r.get("category_primary"),
        detailed=r.get("category_detailed"),
        merchant=r.get("merchant"),
        name=r.get("name"),
    )


def spending_by_category(start: date, end: date) -> list[CategorySpend]:
    with get_conn() as conn:
        rows = _spend_rows(conn, start, end)

    totals: dict[str, dict] = defaultdict(lambda: {"total": 0.0, "n": 0})
    for r in rows:
        cat = _effective_category(r.get("override_category"), r.get("category_primary"))
        totals[cat]["total"] += float(r["amount"])
        totals[cat]["n"] += 1

    return [
        CategorySpend(category=cat, total=agg["total"], transaction_count=agg["n"])
        for cat, agg in sorted(totals.items(), key=lambda kv: -kv[1]["total"])
    ]


def spending_by_friendly_category(start: date, end: date) -> list[CategorySpend]:
    """Spending grouped by user-facing friendly categories (Tesla Charging,
    Subscriptions, Restaurants, Groceries, Rent, etc.).

    Uses the same exclusions and transfer-dedup logic as the raw view so
    credit-card payments and inter-account transfers aren't counted twice.
    """
    with get_conn() as conn:
        rows = _spend_rows(conn, start, end)
        rules = _load_merchant_rules(conn)

    totals: dict[str, dict] = defaultdict(lambda: {"total": 0.0, "n": 0})
    for r in rows:
        cat = _row_friendly_category(r, rules)
        totals[cat]["total"] += float(r["amount"])
        totals[cat]["n"] += 1

    return [
        CategorySpend(category=cat, total=agg["total"], transaction_count=agg["n"])
        for cat, agg in sorted(totals.items(), key=lambda kv: -kv[1]["total"])
    ]


def transactions_for_friendly_category(
    start: date, end: date, category: str
) -> list[TransactionDetail]:
    """Return the individual transactions that fell into a friendly category
    in [start, end]. Mirrors the exclusion/dedup logic of `_spend_rows` so the
    list matches the totals shown on the budgets page.
    """
    with get_conn() as conn:
        rows = _spend_rows(conn, start, end)
        rules = _load_merchant_rules(conn)

    out: list[TransactionDetail] = []
    for r in rows:
        if _row_friendly_category(r, rules) != category:
            continue
        out.append(
            TransactionDetail(
                transaction_id=r["transaction_id"],
                date=date.fromisoformat(r["date"]),
                name=r.get("name") or "",
                merchant=r.get("merchant"),
                amount=float(r["amount"]),
                category_detailed=r.get("category_detailed"),
                account_name=r.get("account_name"),
            )
        )
    out.sort(key=lambda t: t.date, reverse=True)
    return out


# --- Budget period picker ---------------------------------------------------

# Months at or after this (year, month) are treated as having complete data.
# Anything in the current calendar month is always excluded from averages.
COMPLETE_MONTHS_START = (2026, 5)

_MONTH_LABELS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _month_end(d: date) -> date:
    """Last calendar day of d's month."""
    last = calendar.monthrange(d.year, d.month)[1]
    return date(d.year, d.month, last)


def _add_months(d: date, n: int) -> date:
    y = d.year + (d.month - 1 + n) // 12
    m = (d.month - 1 + n) % 12 + 1
    return date(y, m, 1)


def _complete_month_range() -> tuple[date, date] | None:
    """(start, end) covering all complete months in the data window.

    Start = the later of COMPLETE_MONTHS_START's first month or the earliest
    transaction month; the current calendar month is never complete. Returns
    None if there are no complete months yet.
    """
    today = date.today()
    first_incomplete = date(today.year, today.month, 1)
    # The last complete month is the month before the current one.
    last_end = first_incomplete - timedelta(days=1)
    if last_end < date(COMPLETE_MONTHS_START[0], COMPLETE_MONTHS_START[1], 1):
        return None

    with get_conn() as conn:
        row = conn.execute("SELECT MIN(date) AS d FROM transactions").fetchone()
    if not row or not row["d"]:
        earliest = date(COMPLETE_MONTHS_START[0], COMPLETE_MONTHS_START[1], 1)
    else:
        earliest = date.fromisoformat(row["d"][:10]).replace(day=1)

    start = max(
        earliest,
        date(COMPLETE_MONTHS_START[0], COMPLETE_MONTHS_START[1], 1),
    )
    if start > last_end:
        return None
    return start, last_end


def _count_complete_months(rng: tuple[date, date]) -> int:
    """Number of calendar months spanned by [start, end] (inclusive)."""
    start, end = rng
    return (end.year - start.year) * 12 + (end.month - start.month) + 1


def spending_months() -> list[dict]:
    """Distinct YYYY-MM periods present in the data (descending).

    Returns ``[{"period": "2026-06", "label": "June 2026"}, ...]``. Used to
    populate the budget period dropdown.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT substr(date, 1, 7) AS ym "
            "FROM transactions ORDER BY ym DESC"
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        ym = r["ym"]
        try:
            y, m = int(ym[:4]), int(ym[5:7])
        except (ValueError, IndexError):
            continue
        out.append({"period": ym, "label": f"{_MONTH_LABELS[m - 1]} {y}"})
    return out


def spending_for_period(period: str) -> tuple[date, date] | None:
    """(start, end) date range for a period token.

    - ``"avg"``: spans every complete month in the data window (current month
      excluded).
    - ``"YYYY-MM"``: that calendar month.
    Returns None for an unparseable/empty period.
    """
    if period == "avg":
        return _complete_month_range()
    try:
        y, m = int(period[:4]), int(period[5:7])
        start = date(y, m, 1)
        end = _month_end(start)
        return start, end
    except (ValueError, IndexError):
        return None


def average_monthly_spending_by_friendly_category() -> list[CategorySpend]:
    """Average monthly spend per friendly category across complete months.

    Divides each category's total over the complete-month window by the number
    of months in that window (including zero-spend months, so a category that
    only appeared once doesn't get inflated).
    """
    rng = _complete_month_range()
    if rng is None:
        return []
    start, end = rng
    months = _count_complete_months(rng)
    if months <= 0:
        return []

    with get_conn() as conn:
        rows = _spend_rows(conn, start, end)
        rules = _load_merchant_rules(conn)

    totals: dict[str, dict] = defaultdict(lambda: {"total": 0.0, "n": 0})
    for r in rows:
        cat = _row_friendly_category(r, rules)
        totals[cat]["total"] += float(r["amount"])
        totals[cat]["n"] += 1

    return [
        CategorySpend(
            category=cat,
            total=agg["total"] / months,
            transaction_count=agg["n"],
        )
        for cat, agg in sorted(totals.items(), key=lambda kv: -kv[1]["total"])
    ]


def transactions_for_period_category(
    period: str, category: str
) -> list[TransactionDetail]:
    """Transactions matching a friendly category for a period token.

    - ``"avg"``: union of matching transactions across all complete months.
    - ``"YYYY-MM"``: that month's transactions.
    """
    rng = spending_for_period(period)
    if rng is None:
        return []
    start, end = rng
    return transactions_for_friendly_category(start, end, category)


def budget_progress(period: str) -> list[BudgetProgress]:
    """Budget progress for a period token (``"avg"`` or ``"YYYY-MM"``).

    For ``"avg"``, ``spent_so_far`` is the average monthly spend over complete
    months, compared against the per-month ``monthly_limit``.
    """
    with get_conn() as conn:
        budgets = conn.execute("SELECT category, monthly_limit FROM budgets").fetchall()

        if period == "avg":
            rng = _complete_month_range()
            if rng is None:
                rows: list = []
                months = 1
            else:
                months = max(_count_complete_months(rng), 1)
                rows = _spend_rows(conn, *rng)
        else:
            rng = spending_for_period(period)
            if rng is None:
                return []
            months = 1
            rows = _spend_rows(conn, *rng)

        rules = _load_merchant_rules(conn)

    spent_map: dict[str, float] = defaultdict(float)
    for r in rows:
        spent_map[_row_friendly_category(r, rules)] += float(r["amount"])

    out: list[BudgetProgress] = []
    for b in budgets:
        spent_raw = spent_map.get(b["category"], 0.0)
        spent = spent_raw / months if period == "avg" else spent_raw
        limit = float(b["monthly_limit"])
        out.append(
            BudgetProgress(
                category=b["category"],
                monthly_limit=limit,
                spent_so_far=spent,
                remaining=limit - spent,
                pct_used=(spent / limit * 100.0) if limit > 0 else 0.0,
            )
        )
    return out


def list_budgets() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM budgets ORDER BY category").fetchall()]


def upsert_budget(category: str, monthly_limit: float) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO budgets (category, monthly_limit) VALUES (?, ?) "
            "ON CONFLICT(category) DO UPDATE SET monthly_limit=excluded.monthly_limit",
            (category, monthly_limit),
        )


def delete_budget(category: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM budgets WHERE category = ?", (category,))


# --- Merchant rules (persistent vendor → category reclassify) ---------------


def list_merchant_rules() -> list[dict]:
    with get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT merchant_pattern, target_category "
                "FROM merchant_rules ORDER BY merchant_pattern"
            ).fetchall()
        ]


def upsert_merchant_rule(merchant_pattern: str, target_category: str) -> None:
    pattern = merchant_pattern.strip().lower()
    if not pattern:
        raise ValueError("merchant_pattern must not be empty")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO merchant_rules (merchant_pattern, target_category) VALUES (?, ?) "
            "ON CONFLICT(merchant_pattern) DO UPDATE SET target_category=excluded.target_category",
            (pattern, target_category),
        )


def delete_merchant_rule(merchant_pattern: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM merchant_rules WHERE merchant_pattern = ?",
            (merchant_pattern.strip().lower(),),
        )


# --- Per-transaction override -----------------------------------------------


def set_transaction_override(transaction_id: str, override_category: str | None) -> None:
    """Set or clear (None) the per-transaction category override."""
    with get_conn() as conn:
        if override_category:
            conn.execute(
                "UPDATE transactions SET override_category = ? WHERE transaction_id = ?",
                (override_category, transaction_id),
            )
        else:
            conn.execute(
                "UPDATE transactions SET override_category = NULL WHERE transaction_id = ?",
                (transaction_id,),
            )


def friendly_category_options() -> list[str]:
    """Distinct friendly category labels currently in play — the static set
    from ``FRIENDLY_CATEGORY_RULES`` plus any in use via budgets/overrides/
    merchant rules. Used to populate the reclassify dropdown."""
    static = sorted({label for label, _ in categorize.FRIENDLY_CATEGORY_RULES})
    with get_conn() as conn:
        used = {
            r["target_category"]
            for r in conn.execute("SELECT DISTINCT target_category FROM merchant_rules")
        }
        used.update(
            r["override_category"]
            for r in conn.execute(
                "SELECT DISTINCT override_category FROM transactions "
                "WHERE override_category IS NOT NULL"
            )
        )
        used.update(r["category"] for r in conn.execute("SELECT DISTINCT category FROM budgets"))
    return sorted(set(static) | used)


# --- Subscription detection ------------------------------------------------

_SUB_MIN_OCCURRENCES = 3
_SUB_INTERVAL_TOL_DAYS = 7       # cadence must be consistent within ±1 week
_SUB_AMOUNT_TOL_PCT = 0.10       # amounts within ±10% of the median
_SUB_MIN_INTERVAL_DAYS = 20     # ignore near-daily repeats (e.g. coffee)
_SUB_MAX_INTERVAL_DAYS = 45


def _subscription_label(r: sqlite3.Row) -> str:
    """Normalized grouping key: prefer merchant, fall back to name."""
    raw = r["merchant"] or r["name"]
    return " ".join(raw.lower().split())


def detect_subscriptions() -> list[Subscription]:
    """Heuristic detection of recurring charges.

    Groups transactions by normalized merchant/name, then flags a group as a
    subscription if it has >= 3 occurrences with consistent monthly cadence
    (20–45 day spacing, ±7 day tolerance) and amounts within ±10% of the
    median.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT transaction_id, date, amount, name, merchant,
                   category_primary, category_detailed, override_category
            FROM transactions
            WHERE amount > 0
            ORDER BY date
            """
        ).fetchall()
        rules = _load_merchant_rules(conn)

    groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        groups[_subscription_label(r)].append(r)

    subs: list[Subscription] = []
    for label, group in groups.items():
        if len(group) < _SUB_MIN_OCCURRENCES:
            continue

        # Sort by date and compute intervals (days) between consecutive occurrences.
        group_sorted = sorted(group, key=lambda r: r["date"])
        dates = [date.fromisoformat(r["date"]) for r in group_sorted]
        intervals = [
            (dates[i] - dates[i - 1]).days for i in range(1, len(dates))
        ]
        if not intervals:
            continue

        # Check cadence: each interval must fall in the monthly band and be
        # consistent with the median interval.
        median_interval = sorted(intervals)[len(intervals) // 2]
        if not (_SUB_MIN_INTERVAL_DAYS <= median_interval <= _SUB_MAX_INTERVAL_DAYS):
            continue
        if any(abs(iv - median_interval) > _SUB_INTERVAL_TOL_DAYS for iv in intervals):
            continue

        # Amount consistency.
        amounts = sorted(float(r["amount"]) for r in group_sorted)
        med_amt = amounts[len(amounts) // 2]
        if med_amt <= 0:
            continue
        if any(abs(a - med_amt) / med_amt > _SUB_AMOUNT_TOL_PCT for a in amounts):
            continue

        # Friendly category for the representative row (most recent).
        rep = group_sorted[-1]
        cat = _row_friendly_category(dict(rep), rules)

        subs.append(
            Subscription(
                label=label,
                amount=med_amt,
                occurrences=len(group_sorted),
                last_date=dates[-1],
                avg_interval_days=sum(intervals) / len(intervals),
                category=cat,
            )
        )

    subs.sort(key=lambda s: s.amount, reverse=True)
    return subs


# --- Portfolio: asset allocation by sector --------------------------------

CASH_SECTOR = "Cash"
_UNKNOWN_SECTOR = "Uncategorized"

# Curated ETF → sector map. Plaid labels every ETF as "Miscellaneous", so we
# derive a meaningful sector from the ticker (which encodes the fund's
# strategy). Add new ETFs here as your portfolio grows.
ETF_SECTOR_MAP: dict[str, str] = {
    # US broad market
    "SPY": "US Broad Market",
    "RSP": "US Broad Market",
    "SCHB": "US Broad Market",
    # International
    "VXUS": "International Equity",
    # Dividend income
    "VYM": "Dividend Income",
    "SCHD": "Dividend Income",
    "HDV": "Dividend Income",
    # Technology thematic
    "FTEC": "Technology",
    "BOTZ": "Technology",
    # Thematic
    "ICLN": "Clean Energy",
    "PPA": "Aerospace & Defense",
    "VIOV": "US Small-Cap Value",
    # Fixed income
    "BND": "Bonds",
}

# Consolidate Plaid's granular equity sectors into cleaner GICS-style
# groupings. Only applied to equities (ETFs use the ticker map above;
# unknown ETFs fall through to "Diversified ETF").
PLAID_SECTOR_REMAP: dict[str, str] = {
    "Electronic Technology": "Technology",
    "Technology Services": "Technology",
    "Retail Trade": "Consumer Discretionary",
    "Non-Energy Minerals": "Materials",
    "Process Industries": "Materials",
    # Plaid dumps investment trusts / specialty finance under "Miscellaneous".
    "Miscellaneous": "Finance",
}


def _latest_snapshot_date(conn) -> str | None:
    row = conn.execute("SELECT MAX(snapshot_date) AS d FROM balances").fetchone()
    return row["d"] if row else None


def _account_balance_sector(name: str, subtype: str | None) -> str:
    """Sector for an investment account surfaced via its balance (no holdings)."""
    n = (name or "").upper()
    s = (subtype or "").lower()
    if "401" in n or "401" in s or "retirement" in s or "IRA" in n:
        return "Retirement"
    if "RESTRICTED" in n or "stock plan" in s or "ESPP" in n:
        return "Equity Compensation"
    return "Other Investments"


def _latest_holdings_snapshot_date(conn) -> str | None:
    row = conn.execute("SELECT MAX(snapshot_date) AS d FROM holdings").fetchone()
    return row["d"] if row else None


# --- 401(k) plan allocations ----------------------------------------------

# Default allocation set for known plans. Keyed by a case-insensitive
# substring of the account name; applied idempotently on init so re-seeding
# after a DB reset is automatic. New plans can be added via the UI.
DEFAULT_PLAN_ALLOCATIONS: dict[str, list[tuple[str, str | None, float, str]]] = {
    "ARCHER AVIATION 401(K)": [
        ("VANG INST TOT STK MK", "VITSX", 29.74, "US Broad Market"),
        ("VANG TOT INTL STK AD", "VTIAX", 29.52, "International Equity"),
        ("FID SM CAP IDX", "FSSNX", 10.58, "US Small-Cap Value"),
        ("FID BLUE CHIP GR K6", "FBGRX", 10.25, "US Broad Market"),
        ("FID MID CAP IDX", "FSMDX", 10.15, "US Mid Cap"),
        ("DFA EMRG MKT CORE EQ", "DFCEX", 5.07, "International Equity"),
        ("FID US BOND IDX", "FXNAX", 4.69, "Bonds"),
    ],
}


def seed_default_plan_allocations() -> int:
    """Idempotently insert the curated default allocations for any matching
    401(k) accounts that currently have none. Returns the number of rows
    inserted."""
    inserted = 0
    with get_conn() as conn:
        accts = conn.execute(
            "SELECT account_id, name FROM accounts WHERE kind = 'investment'"
        ).fetchall()
        for acct in accts:
            name_u = (acct["name"] or "").upper()
            for pattern, rows in DEFAULT_PLAN_ALLOCATIONS.items():
                if pattern.upper() not in name_u:
                    continue
                existing = conn.execute(
                    "SELECT 1 FROM plan_allocations WHERE account_id = ? LIMIT 1",
                    (acct["account_id"],),
                ).fetchone()
                if existing:
                    continue
                for label, ticker, pct, sector in rows:
                    conn.execute(
                        "INSERT OR IGNORE INTO plan_allocations "
                        "(account_id, label, ticker, allocation_pct, sector) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (acct["account_id"], label, ticker, pct, sector),
                    )
                    inserted += 1
    return inserted


def list_plan_allocations() -> list[dict]:
    """All plan allocations with account/institution context, for the UI."""
    with get_conn() as conn:
        rows = conn.execute(
            LATEST_BALANCES_CTE
            + """
            SELECT pa.account_id, pa.label, pa.ticker, pa.allocation_pct, pa.sector,
                   a.name AS account_name, a.subtype, i.institution,
                   b.current AS balance
            FROM plan_allocations pa
            JOIN accounts a USING (account_id)
            JOIN items i USING (item_id)
            LEFT JOIN latest_balances b ON b.account_id = a.account_id
            ORDER BY i.institution, a.name, pa.allocation_pct DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def plan_allocations_for_account(account_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT label, ticker, allocation_pct, sector "
            "FROM plan_allocations WHERE account_id = ? "
            "ORDER BY allocation_pct DESC",
            (account_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def upsert_plan_allocation(
    account_id: str, label: str, ticker: str | None, allocation_pct: float, sector: str
) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO plan_allocations "
            "(account_id, label, ticker, allocation_pct, sector) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(account_id, label) DO UPDATE SET "
            "ticker=excluded.ticker, "
            "allocation_pct=excluded.allocation_pct, "
            "sector=excluded.sector",
            (account_id, label, ticker, allocation_pct, sector),
        )


def delete_plan_allocation(account_id: str, label: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM plan_allocations WHERE account_id = ? AND label = ?",
            (account_id, label),
        )


def _plan_allocation_rows(conn) -> list[sqlite3.Row]:
    """Plan allocations joined with the account's latest balance, for roll-up."""
    return conn.execute(
        LATEST_BALANCES_CTE
        + """
        SELECT pa.account_id, pa.label, pa.ticker, pa.allocation_pct, pa.sector,
               a.name AS account_name, a.subtype, i.institution,
               b.current AS balance
        FROM plan_allocations pa
        JOIN accounts a USING (account_id)
        JOIN items i USING (item_id)
        LEFT JOIN latest_balances b ON b.account_id = a.account_id
        """
    ).fetchall()


def _infer_sector(
    *,
    security_type: str | None,
    ticker: str | None,
    plaid_sector: str | None,
    industry: str | None,
    is_cash_equivalent: bool,
    override: str | None,
) -> str:
    """Effective sector for a holding, late-bound at query time.

    Priority (highest first):
      1. ``override_sector`` (manual one-off reclassify)
      2. ETF ticker → curated sector (``ETF_SECTOR_MAP``)
      3. Security ``type`` signals (cryptocurrency / cash)
      4. Industry hint (Aerospace & Defense)
      5. Plaid sector, consolidated via ``PLAID_SECTOR_REMAP``
      6. 'Uncategorized'
    """
    if override:
        return override

    # ETFs: derive from curated ticker map. Unknown ETFs → "Diversified ETF".
    if security_type == "etf":
        if ticker and ticker in ETF_SECTOR_MAP:
            return ETF_SECTOR_MAP[ticker]
        return "Diversified ETF"

    # Cryptocurrency (Plaid leaves sector blank).
    if security_type == "cryptocurrency":
        return "Cryptocurrency"

    # Cash-type securities (e.g. CUR:USD sweep positions).
    if security_type == "cash" or (is_cash_equivalent and not plaid_sector):
        return CASH_SECTOR

    # Industry hint: aerospace & defense (Plaid sometimes buries these under
    # "Electronic Technology").
    if industry and "Aerospace" in industry:
        return "Aerospace & Defense"

    # Consolidate Plaid's equity sectors.
    if plaid_sector:
        return PLAID_SECTOR_REMAP.get(plaid_sector, plaid_sector)

    return _UNKNOWN_SECTOR


def portfolio_by_sector() -> list[SectorAllocation]:
    """Aggregate portfolio assets by sector.

    - Depository accounts (e.g. Bank of America) contribute their current
      balance to the ``Cash`` sector.
    - Investment accounts (Robinhood, Fidelity) contribute each holding's
      institution_value, grouped by the holding's effective sector.
    """
    with get_conn() as conn:
        snap = _latest_snapshot_date(conn)
        if not snap:
            return []

        # Cash from depository accounts (latest snapshot per account).
        cash_rows = conn.execute(
            LATEST_BALANCES_CTE
            + """
            SELECT a.account_id, b.current
            FROM accounts a
            JOIN latest_balances b USING (account_id)
            WHERE a.kind = 'depository'
              AND a.exclude_from_net_worth = 0
            """,
        ).fetchall()

        # Holdings from investment accounts (latest holdings snapshot).
        # Excludes accounts flagged exclude_from_net_worth (e.g. unvested
        # restricted stock) and 401(k) accounts covered by plan_allocations
        # (those are broken down by fund below to avoid double-counting).
        holding_rows = conn.execute(
            """
            SELECT h.account_id, h.security_id, h.institution_value,
                   h.override_sector,
                   s.sector, s.industry, s.type, s.is_cash_equivalent,
                   s.ticker, s.name,
                   a.name AS account_name, i.institution
            FROM holdings h
            JOIN accounts a USING (account_id)
            JOIN items i USING (item_id)
            LEFT JOIN securities s USING (security_id)
            WHERE h.snapshot_date = (
                SELECT MAX(snapshot_date) FROM holdings
            )
              AND a.exclude_from_net_worth = 0
              AND a.account_id NOT IN (SELECT DISTINCT account_id FROM plan_allocations)
            """
        ).fetchall()

        # Investment accounts that have NO holdings (e.g. 401k, ESPP,
        # restricted stock) — surface their account balance instead.
        # Excludes 401(k) accounts that have plan_allocations rows, since
        # those are broken down by fund below.
        no_holdings_rows = conn.execute(
            LATEST_BALANCES_CTE
            + """
            SELECT a.account_id, a.name, a.subtype, b.current, i.institution
            FROM accounts a
            JOIN latest_balances b USING (account_id)
            JOIN items i USING (item_id)
            WHERE a.kind = 'investment'
              AND a.exclude_from_net_worth = 0
              AND a.account_id NOT IN (
                  SELECT DISTINCT account_id FROM holdings
                  WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM holdings)
              )
              AND a.account_id NOT IN (SELECT DISTINCT account_id FROM plan_allocations)
            """,
        ).fetchall()

        # Manual 401(k) plan allocations — each fund's % of the plan balance.
        plan_rows = _plan_allocation_rows(conn)

    totals: dict[str, dict] = defaultdict(lambda: {"total": 0.0, "n": 0})

    for r in cash_rows:
        val = float(r["current"] or 0.0)
        if val <= 0:
            continue
        totals[CASH_SECTOR]["total"] += val
        totals[CASH_SECTOR]["n"] += 1

    for r in holding_rows:
        val = float(r["institution_value"] or 0.0)
        if val <= 0:
            continue
        sector = _infer_sector(
            security_type=r["type"],
            ticker=r["ticker"],
            plaid_sector=r["sector"],
            industry=r["industry"],
            is_cash_equivalent=bool(r["is_cash_equivalent"]),
            override=r["override_sector"],
        )
        totals[sector]["total"] += val
        totals[sector]["n"] += 1

    for r in no_holdings_rows:
        val = float(r["current"] or 0.0)
        if val <= 0:
            continue
        sector = _account_balance_sector(r["name"], r["subtype"])
        totals[sector]["total"] += val
        totals[sector]["n"] += 1

    # 401(k) plan allocations: each fund's slice = balance * pct / 100.
    plan_balance_by_account: dict[str, float] = {}
    for r in plan_rows:
        aid = r["account_id"]
        if aid not in plan_balance_by_account:
            plan_balance_by_account[aid] = float(r["balance"] or 0.0)
        bal = plan_balance_by_account[aid]
        val = bal * float(r["allocation_pct"]) / 100.0
        if val <= 0:
            continue
        totals[r["sector"]]["total"] += val
        totals[r["sector"]]["n"] += 1

    return [
        SectorAllocation(sector=sec, total=agg["total"], asset_count=agg["n"])
        for sec, agg in sorted(totals.items(), key=lambda kv: -kv[1]["total"])
    ]


def assets_in_sector(sector: str) -> list[AssetDetail]:
    """Individual assets that fall into ``sector``.

    Returns cash accounts (for the Cash sector) plus any holdings whose
    effective sector matches.
    """
    with get_conn() as conn:
        snap = _latest_snapshot_date(conn)
        if not snap:
            return []

        out: list[AssetDetail] = []

        if sector == CASH_SECTOR:
            cash_rows = conn.execute(
                LATEST_BALANCES_CTE
                + """
                SELECT a.account_id, a.name, b.current, i.institution
                FROM accounts a
                JOIN latest_balances b USING (account_id)
                JOIN items i USING (item_id)
                WHERE a.kind = 'depository'
                  AND a.exclude_from_net_worth = 0
                """,
            ).fetchall()
            for r in cash_rows:
                val = float(r["current"] or 0.0)
                if val <= 0:
                    continue
                out.append(
                    AssetDetail(
                        account_id=r["account_id"],
                        account_name=r["name"],
                        institution=r["institution"],
                        security_id=None,
                        ticker=None,
                        name=f"{r['name']} (cash)",
                        sector=CASH_SECTOR,
                        value=val,
                        quantity=None,
                        cost_basis=None,
                    )
                )

        holding_rows = conn.execute(
            """
            SELECT h.account_id, h.security_id, h.institution_value,
                   h.quantity, h.cost_basis, h.override_sector,
                   s.sector, s.industry, s.type, s.is_cash_equivalent,
                   s.ticker, s.name,
                   a.name AS account_name, i.institution
            FROM holdings h
            JOIN accounts a USING (account_id)
            JOIN items i USING (item_id)
            LEFT JOIN securities s USING (security_id)
            WHERE h.snapshot_date = (SELECT MAX(snapshot_date) FROM holdings)
              AND a.exclude_from_net_worth = 0
              AND a.account_id NOT IN (SELECT DISTINCT account_id FROM plan_allocations)
            """
        ).fetchall()

        # Investment accounts without holdings (401k, ESPP, etc.)
        # Excludes 401(k) accounts that have plan_allocations (broken down below).
        no_holdings_rows = conn.execute(
            LATEST_BALANCES_CTE
            + """
            SELECT a.account_id, a.name, a.subtype, b.current, i.institution
            FROM accounts a
            JOIN latest_balances b USING (account_id)
            JOIN items i USING (item_id)
            WHERE a.kind = 'investment'
              AND a.exclude_from_net_worth = 0
              AND a.account_id NOT IN (
                  SELECT DISTINCT account_id FROM holdings
                  WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM holdings)
              )
              AND a.account_id NOT IN (SELECT DISTINCT account_id FROM plan_allocations)
            """,
        ).fetchall()

        plan_rows = _plan_allocation_rows(conn)

        for r in holding_rows:
            val = float(r["institution_value"] or 0.0)
            if val <= 0:
                continue
            eff_sector = _infer_sector(
                security_type=r["type"],
                ticker=r["ticker"],
                plaid_sector=r["sector"],
                industry=r["industry"],
                is_cash_equivalent=bool(r["is_cash_equivalent"]),
                override=r["override_sector"],
            )
            if eff_sector != sector:
                continue
            out.append(
                AssetDetail(
                    account_id=r["account_id"],
                    account_name=r["account_name"],
                    institution=r["institution"],
                    security_id=r["security_id"],
                    ticker=r["ticker"],
                    name=r["name"] or r["ticker"] or "Holding",
                    sector=eff_sector,
                    value=val,
                    quantity=float(r["quantity"]) if r["quantity"] is not None else None,
                    cost_basis=(
                        float(r["cost_basis"]) if r["cost_basis"] is not None else None
                    ),
                )
            )

        for r in no_holdings_rows:
            val = float(r["current"] or 0.0)
            if val <= 0:
                continue
            eff_sector = _account_balance_sector(r["name"], r["subtype"])
            if eff_sector != sector:
                continue
            out.append(
                AssetDetail(
                    account_id=r["account_id"],
                    account_name=r["name"],
                    institution=r["institution"],
                    security_id=None,
                    ticker=None,
                    name=r["name"],
                    sector=eff_sector,
                    value=val,
                    quantity=None,
                    cost_basis=None,
                )
            )

        # 401(k) plan allocations: each fund's slice of the plan balance.
        plan_balance_by_account: dict[str, float] = {}
        for r in plan_rows:
            aid = r["account_id"]
            if aid not in plan_balance_by_account:
                plan_balance_by_account[aid] = float(r["balance"] or 0.0)
            bal = plan_balance_by_account[aid]
            val = bal * float(r["allocation_pct"]) / 100.0
            if val <= 0:
                continue
            if r["sector"] != sector:
                continue
            out.append(
                AssetDetail(
                    account_id=r["account_id"],
                    account_name=r["account_name"],
                    institution=r["institution"],
                    security_id=None,
                    ticker=r["ticker"],
                    name=r["label"],
                    sector=r["sector"],
                    value=val,
                    quantity=None,
                    cost_basis=None,
                )
            )

    out.sort(key=lambda a: -a.value)
    return out


def set_holding_sector_override(
    account_id: str, security_id: str | None, sector: str | None
) -> None:
    """Set or clear (None) the per-holding sector override.

    Applies to the latest snapshot's row for this account+security pair; the
    override persists across future syncs because sync_holdings omits
    ``override_sector`` from its UPDATE clause.
    """
    with get_conn() as conn:
        if security_id is None:
            # Cash positions have no security_id and can't be reclassified.
            return
        conn.execute(
            """
            UPDATE holdings SET override_sector = ?
            WHERE account_id = ? AND security_id = ?
              AND snapshot_date = (SELECT MAX(snapshot_date) FROM holdings)
            """,
            (sector, account_id, security_id),
        )


def sector_options() -> list[str]:
    """Distinct sector labels in play — the curated ETF/sector set plus any
    currently in use via Plaid metadata or overrides. Used to populate the
    reclassify dropdown on the portfolio page."""
    static = sorted(set(ETF_SECTOR_MAP.values()) | {
        CASH_SECTOR,
        "Cryptocurrency",
        "Technology",
        "Finance",
        "Healthcare",
        "Consumer Discretionary",
        "Consumer Staples",
        "Communication Services",
        "Industrials",
        "Energy",
        "Materials",
        "Real Estate",
        "Utilities",
        "Aerospace & Defense",
        "Diversified ETF",
        "Retirement",
        "Equity Compensation",
        "Other Investments",
        "US Mid Cap",
        _UNKNOWN_SECTOR,
    })
    with get_conn() as conn:
        used = {
            r["sector"]
            for r in conn.execute(
                "SELECT DISTINCT sector FROM securities WHERE sector IS NOT NULL"
            )
        }
        used.update(
            r["override_sector"]
            for r in conn.execute(
                "SELECT DISTINCT override_sector FROM holdings "
                "WHERE override_sector IS NOT NULL"
            )
        )
    # Drop raw Plaid sector labels that are remapped at query time — they
    # never surface as final sectors, so offering them in the reclassify
    # dropdown would be misleading and reintroduce the messy taxonomy.
    used -= set(PLAID_SECTOR_REMAP)
    return sorted(set(static) | used)


# --- Debt Tracker: manually-tracked loans -----------------------------------


def _row_to_loan(r: sqlite3.Row) -> DebtLoan:
    return DebtLoan(
        loan_id=r["loan_id"],
        name=r["name"],
        loan_type=r["loan_type"],
        interest_rate=r["interest_rate"],
        min_payment=r["min_payment"],
        current_balance=r["current_balance"],
        next_due_date=date.fromisoformat(r["next_due_date"]) if r["next_due_date"] else None,
        auto_pay=bool(r["auto_pay"]),
        status=r["status"],
        notes=r["notes"],
    )


def list_loans() -> list[DebtLoan]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM loans ORDER BY name").fetchall()
    return [_row_to_loan(r) for r in rows]


def upsert_loan(
    loan_id: str | None,
    name: str,
    loan_type: str | None,
    interest_rate: float | None,
    min_payment: float | None,
    current_balance: float | None,
    next_due_date: date | None,
    auto_pay: bool,
    status: str,
    notes: str | None,
) -> str:
    """Insert or update a loan. On a balance change, record a snapshot row.

    Returns the loan_id (newly generated if None was passed).
    """
    import uuid

    with get_conn() as conn:
        is_new = not loan_id
        if is_new:
            loan_id = uuid.uuid4().hex[:12]

        if not is_new:
            prev = conn.execute(
                "SELECT current_balance FROM loans WHERE loan_id = ?", (loan_id,)
            ).fetchone()
            prev_balance = prev["current_balance"] if prev else None
        else:
            prev_balance = None

        conn.execute(
            """
            INSERT INTO loans
              (loan_id, name, loan_type, interest_rate, min_payment,
               current_balance, next_due_date, auto_pay, status, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(loan_id) DO UPDATE SET
                name=excluded.name, loan_type=excluded.loan_type,
                interest_rate=excluded.interest_rate, min_payment=excluded.min_payment,
                current_balance=excluded.current_balance,
                next_due_date=excluded.next_due_date, auto_pay=excluded.auto_pay,
                status=excluded.status, notes=excluded.notes,
                updated_at=datetime('now')
            """,
            (
                loan_id, name, loan_type, interest_rate, min_payment,
                current_balance, next_due_date.isoformat() if next_due_date else None,
                int(auto_pay), status, notes,
            ),
        )

        # Record a balance snapshot whenever the balance actually moves.
        # Skip when current_balance is NULL (user hasn't entered it yet).
        if current_balance is not None and current_balance != prev_balance:
            today = date.today().isoformat()
            conn.execute(
                """
                INSERT INTO loan_balance_snapshots (loan_id, snapshot_date, balance)
                VALUES (?, ?, ?)
                ON CONFLICT(loan_id, snapshot_date) DO UPDATE SET balance=excluded.balance
                """,
                (loan_id, today, current_balance),
            )
    return loan_id


def delete_loan(loan_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM loans WHERE loan_id = ?", (loan_id,))


def _row_to_payment(r: sqlite3.Row) -> LoanPayment:
    return LoanPayment(
        payment_id=r["payment_id"],
        loan_id=r["loan_id"],
        payment_date=date.fromisoformat(r["payment_date"]),
        amount=r["amount"],
        status=r["status"],
        source=r["source"],
        notes=r["notes"],
    )


def list_loan_payments() -> list[LoanPayment]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM loan_payments ORDER BY payment_date DESC, created_at DESC"
        ).fetchall()
    return [_row_to_payment(r) for r in rows]


def record_loan_payment(
    payment_date: date,
    amount: float,
    loan_id: str | None = None,
    status: str = "Received",
    source: str | None = None,
    notes: str | None = None,
    new_balance: float | None = None,
) -> str:
    """Log a payment. When loan_id + new_balance are provided, also update that
    loan's current_balance and snapshot it. A NULL loan_id means a combined
    payment across all loans (logged for history but not auto-split)."""
    import uuid

    payment_id = uuid.uuid4().hex[:12]
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO loan_payments
              (payment_id, loan_id, payment_date, amount, status, source, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (payment_id, loan_id, payment_date.isoformat(), amount, status, source, notes),
        )
        if loan_id and new_balance is not None:
            conn.execute(
                "UPDATE loans SET current_balance=?, updated_at=datetime('now') WHERE loan_id=?",
                (new_balance, loan_id),
            )
            conn.execute(
                """
                INSERT INTO loan_balance_snapshots (loan_id, snapshot_date, balance)
                VALUES (?, ?, ?)
                ON CONFLICT(loan_id, snapshot_date) DO UPDATE SET balance=excluded.balance
                """,
                (loan_id, payment_date.isoformat(), new_balance),
            )
    return payment_id


def delete_loan_payment(payment_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM loan_payments WHERE payment_id = ?", (payment_id,))


def loan_balance_history() -> list[dict]:
    """Total loan balance over time, ascending.

    Uses the latest balance snapshot as an anchor and reconstructs prior
    points from the payment log: each past payment is "added back" to derive
    the balance before it was applied. This gives a declining line even when
    only one snapshot exists. Interest accrual between payments is not modeled,
    so early points are a lower bound — but the line still shows the payoff
    trend the user wants to see.
    """
    with get_conn() as conn:
        snap = conn.execute(
            "SELECT snapshot_date, SUM(balance) AS total "
            "FROM loan_balance_snapshots GROUP BY snapshot_date "
            "ORDER BY snapshot_date DESC LIMIT 1"
        ).fetchone()
        if not snap:
            return []
        anchor_date = snap["snapshot_date"]
        anchor_total = float(snap["total"] or 0.0)
        payments = conn.execute(
            "SELECT payment_date, amount FROM loan_payments ORDER BY payment_date ASC"
        ).fetchall()

    total_paid = sum(float(p["amount"] or 0.0) for p in payments)
    running = anchor_total + total_paid  # balance before the earliest payment
    points: list[dict] = []
    for p in payments:
        if p["payment_date"] < anchor_date:
            running -= float(p["amount"] or 0.0)
            points.append({"date": p["payment_date"], "total": round(running, 2)})
    points.append({"date": anchor_date, "total": round(anchor_total, 2)})
    return points


PAYOFF_STRATEGIES = ("distributed", "highest_interest", "lowest_balance")


def project_payoff(
    extra_monthly: float = 0.0,
    extra_onetime: float = 0.0,
    strategy: str = "distributed",
) -> dict:
    """Forward projection of total loan balance under a payoff strategy.

    Strategies:
      - ``distributed`` (proportional): split the extra payment across
        projectable loans proportional to each loan's balance share. This is
        the historical default.
      - ``highest_interest`` (avalanche): direct the full extra payment at
        the highest-interest-rate loan each month.
      - ``lowest_balance`` (snowball): direct the full extra payment at the
        smallest-balance loan each month.

    When a loan pays off, its minimum payment rolls into the extra-payment
    pool for the remaining loans (all strategies), accelerating payoff.

    Returns a dict with:
      - schedule: list of {month_offset, date, total_balance} points
      - months_to_payoff: month offset when total hits 0 (or None)
      - payoff_date: ISO date string (or None if not within max_months)
      - total_interest: sum of interest paid across all projectable loans
      - unprojectable: list of loan names missing rate/min/balance

    Loans missing interest_rate, min_payment, or current_balance are skipped
    (reported in `unprojectable`) since we can't amortize them.
    """
    if strategy not in PAYOFF_STRATEGIES:
        raise ValueError(
            f"unknown strategy {strategy!r}; expected one of {PAYOFF_STRATEGIES}"
        )

    loans = list_loans()
    projectable = [
        ln for ln in loans
        if ln.current_balance is not None
        and ln.interest_rate is not None
        and ln.min_payment is not None
        and ln.current_balance > 0
    ]
    unprojectable = [ln.name for ln in loans if ln not in projectable]

    if not projectable:
        return {
            "schedule": [],
            "months_to_payoff": None,
            "payoff_date": None,
            "total_interest": 0.0,
            "unprojectable": unprojectable,
        }

    # Per-loan mutable state for the simulation.
    state = [
        {
            "name": ln.name,
            "balance": float(ln.current_balance or 0.0),
            "rate": float(ln.interest_rate or 0.0),
            "monthly_rate": (float(ln.interest_rate or 0.0) / 100.0) / 12.0,
            "min_payment": float(ln.min_payment or 0.0),
            "interest_paid": 0.0,
            "schedule": [0.0],  # ending balance per month; index 0 = start
        }
        for ln in projectable
    ]
    # Apply one-time lump payments up front (proportional to balance, matching
    # the historical "distributed" behaviour for the lump sum across all
    # strategies — the strategy only governs the recurring extra).
    total_balance = sum(s["balance"] for s in state)
    if extra_onetime > 0 and total_balance > 0:
        for s in state:
            share = s["balance"] / total_balance
            s["balance"] = max(s["balance"] - extra_onetime * share, 0.0)
            s["schedule"][0] = round(s["balance"], 2)

    max_months = 600
    today = date.today()
    total_schedule: list[dict] = [{
        "month_offset": 0,
        "date": today.isoformat(),
        "total_balance": round(sum(s["balance"] for s in state), 2),
    }]
    months_to_payoff: int | None = None

    for m in range(1, max_months + 1):
        # Pool of extra payment available this month: the user's extra_monthly
        # plus the freed-up minimums from any loans paid off in prior months.
        freed_min = sum(
            s["min_payment"] for s in state if s["balance"] <= 0.005
        )
        extra_pool = extra_monthly + freed_min

        # Order the still-active loans for this month's extra allocation.
        active = [s for s in state if s["balance"] > 0.005]
        if strategy == "highest_interest":
            active.sort(key=lambda s: (-s["rate"], s["balance"]))
        elif strategy == "lowest_balance":
            active.sort(key=lambda s: (s["balance"], -s["rate"]))
        else:  # distributed — order doesn't matter, split is proportional
            active.sort(key=lambda s: -s["balance"])

        total_active = sum(s["balance"] for s in active)

        # Assign each active loan its slice of the extra payment.
        extra_by_loan: dict[int, float] = {}
        if extra_pool > 0 and active:
            if strategy == "distributed" and total_active > 0:
                for s in active:
                    extra_by_loan[id(s)] = extra_pool * (
                        s["balance"] / total_active
                    )
            else:
                # Avalanche / snowball: stack the whole extra onto the
                # first loan in the ordering; any remainder (when the top
                # loan's balance + interest is less than its payment) spills
                # to the next, and so on.
                remaining_extra = extra_pool
                for s in active:
                    if remaining_extra <= 0:
                        break
                    # Cap at what would actually apply (balance + interest
                    # this month minus the min payment's principal portion).
                    interest_this_month = s["balance"] * s["monthly_rate"]
                    cap = max(s["balance"] + interest_this_month - s["min_payment"], 0.0)
                    take = min(remaining_extra, cap)
                    if take > 0:
                        extra_by_loan[id(s)] = extra_by_loan.get(id(s), 0.0) + take
                        remaining_extra -= take

        # Step each active loan forward one month.
        for s in active:
            interest = s["balance"] * s["monthly_rate"]
            s["interest_paid"] += interest
            payment = min(
                s["min_payment"] + extra_by_loan.get(id(s), 0.0),
                s["balance"] + interest,
            )
            principal = payment - interest
            s["balance"] = max(s["balance"] - principal, 0.0)
            s["schedule"].append(round(s["balance"], 2))

        # Paid-off loans this month append a 0.0 point so their schedule
        # length tracks the others.
        for s in state:
            if s["balance"] <= 0.005:
                if len(s["schedule"]) <= m:
                    s["schedule"].append(0.0)

        total = sum(s["balance"] for s in state)
        d = today + timedelta(days=30 * m)
        total_schedule.append({
            "month_offset": m,
            "date": d.isoformat(),
            "total_balance": round(total, 2),
        })
        if total < 0.01 and months_to_payoff is None:
            months_to_payoff = m
            break

    payoff_date = None
    if months_to_payoff is not None:
        payoff_date = (today + timedelta(days=30 * months_to_payoff)).isoformat()

    total_interest = round(sum(s["interest_paid"] for s in state), 2)

    return {
        "schedule": total_schedule,
        "months_to_payoff": months_to_payoff,
        "payoff_date": payoff_date,
        "total_interest": total_interest,
        "unprojectable": unprojectable,
    }


def debt_summary() -> dict:
    """Headline numbers for the Debt Tracker page."""
    loans = list_loans()
    total_balance = sum((ln.current_balance or 0.0) for ln in loans)
    total_min = sum((ln.min_payment or 0.0) for ln in loans)
    proj = project_payoff(extra_monthly=0.0)
    return {
        "total_balance": round(total_balance, 2),
        "total_min_payment": round(total_min, 2),
        "num_loans": len(loans),
        "projected_payoff_months": proj["months_to_payoff"],
        "projected_payoff_date": proj["payoff_date"],
        "projected_total_interest": proj["total_interest"],
        "unprojectable": proj["unprojectable"],
    }

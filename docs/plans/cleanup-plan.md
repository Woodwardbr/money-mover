# Money Mover — Cleanup Plan

**Created:** 2026-08-29
**Status:** not started
**Audience:** an implementing agent working task-by-task

Derived from two passes over `src/money_mover/` at commit `a9d6468` plus uncommitted
working-tree changes: a full architectural read, and a `/code-review` pass over the
working-tree diff (budget period picker, debt payoff strategies, incremental
`transactions_sync`). Every "verified" claim below was reproduced by running the code.

---

## How to work this plan

1. Work tasks **in order**. Phases 1 and 2 are prerequisites for everything after.
2. **One task per commit.** Commit message: `cleanup: <task id> <short title>`.
3. After every task run the full check suite:
   ```bash
   uv run ruff check .
   uv run pytest                       # available after Task 2.1
   uv run python -c "import money_mover.app"   # import + startup smoke
   ```
4. Each task has a **Done when** section. Do not move on until every box is satisfiable.
5. If a task's premise turns out to be wrong when you open the file, **stop and report**
   rather than improvising a different change.

### Guardrails

- Do **not** reformat files you are not otherwise editing. Ruff is already clean
  (`All checks passed!`); keep it that way.
- Do **not** change the SQLite schema except where a task explicitly says to. Schema
  changes must go through the idempotent-migration block in `db.py:init_db()`.
- Do **not** delete or regenerate anything under `data/` — it holds the user's real
  financial data and is gitignored.
- Do **not** commit `.env`, `data/`, or `__pycache__`.
- Preserve existing docstrings when refactoring; they carry real domain reasoning.

### Repo orientation

| File | Lines | Role |
|---|---|---|
| `src/money_mover/analytics.py` | 1635 | spending, budgets, subscriptions, portfolio, debt |
| `src/money_mover/static/app.js` | 1086 | all frontend logic |
| `src/money_mover/api.py` | 485 | JSON API |
| `src/money_mover/sync.py` | 253 | Plaid → SQLite |
| `src/money_mover/plaid.py` | 246 | Plaid SDK wrapper |
| `src/money_mover/db.py` | 170 | schema + connection |
| `src/money_mover/app.py` | 106 | FastAPI app + page routes |

---

# Phase 1 — Unblock (do first)

## Task 1.1 — Fix the fresh-clone startup crash

**Severity: blocking.** The app does not boot from a clean checkout.

`analytics.py:729 seed_default_plan_allocations()` bypasses `get_conn()` and calls
`sqlite3.connect(settings.db_path)` directly, so `init_db()` never runs — the `data/`
directory is not created and no tables exist. It is wired to startup via
`app.py:24 @app.on_event("startup")`.

Reproduced:
```
SEED CRASH: OperationalError unable to open database file
```

**Change** — in `src/money_mover/analytics.py`, replace the body of
`seed_default_plan_allocations` so it uses the shared connection helper:

```python
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
```

Note what this removes: the local `import sqlite3` and `from .config import settings`
(both are already available at module scope — `sqlite3` at line 4), the explicit
`conn.commit()` and `conn.close()` (`get_conn` does both), and the `try/finally`.

**Done when**
- [ ] `seed_default_plan_allocations` contains no `sqlite3.connect` and no local imports.
- [ ] From a directory where `data/` does not exist:
      `DB_PATH=./scratch/mm.db uv run python -c "from money_mover import analytics; print(analytics.seed_default_plan_allocations())"`
      prints `0` instead of raising.
- [ ] `uv run python -c "import money_mover.app"` succeeds.

---

## Task 1.2 — Make the Plaid secret env vars match the documentation

`config.py:29 _plaid_secret()` reads `PLAID_SECRET_SANDBOX` / `PLAID_SECRET_PRODUCTION`,
but `.env.example` and `README.md` both instruct the user to set `PLAID_SECRET`. Following
the documentation yields an empty secret and a confusing runtime error from
`plaid.py:142 _client()`.

Second defect in the same expression: `_plaid_secret(_env("PLAID_ENV", "sandbox"))` passes
the **raw** env string, not the normalized one, so `PLAID_ENV=Production` silently selects
the sandbox secret while `plaid_env_value` reports `production`.

**Change** — in `src/money_mover/config.py`, replace `_plaid_secret` and the `settings`
construction:

```python
def _plaid_secret(env: str) -> str:
    """Env-specific secret if set, else the generic PLAID_SECRET.

    ``env`` must already be normalized (lowercased/stripped).
    """
    specific = (
        _env("PLAID_SECRET_PRODUCTION")
        if env == "production"
        else _env("PLAID_SECRET_SANDBOX")
    )
    return specific or _env("PLAID_SECRET")


_PLAID_ENV = _env("PLAID_ENV", "sandbox").strip().lower()

settings = Settings(
    plaid_client_id=_env("PLAID_CLIENT_ID"),
    plaid_secret=_plaid_secret(_PLAID_ENV),
    plaid_env=_PLAID_ENV,
    app_host=_env("APP_HOST", "127.0.0.1"),
    app_port=int(_env("APP_PORT", "8000")),
    db_path=Path(_env("DB_PATH", "data/money-mover.db")),
)
```

Then update `.env.example` to document all three forms:

```
# Plaid API credentials — get these from https://dashboard.plaid.com
PLAID_CLIENT_ID=your_client_id_here
PLAID_ENV=sandbox

# Either set the generic secret...
PLAID_SECRET=your_secret_here
# ...or override per environment (these win when set):
# PLAID_SECRET_SANDBOX=
# PLAID_SECRET_PRODUCTION=
```

Also drop the dead `development` branch in `plaid.py:133 _env_to_plaid_environment` —
Plaid retired that environment and the hardcoded `https://development.plaid.com` no
longer resolves. Keep only `sandbox` and `production`, with `sandbox` as the fallback.

**Done when**
- [ ] `PLAID_SECRET=abc uv run python -c "from money_mover.config import settings; print(settings.plaid_secret)"` prints `abc`.
- [ ] `PLAID_ENV=Production` results in `settings.plaid_env == "production"` and selects `PLAID_SECRET_PRODUCTION`.
- [ ] `.env.example` and `README.md` agree with the code.
- [ ] `_env_to_plaid_environment` no longer mentions `development`.

---

# Phase 2 — Safety net

## Task 2.1 — Add a test harness

There are currently **no tests** and no test dependency. Everything in Phase 3 is a
behavior change to money math; it needs regression cover first.

**Change 1** — add pytest to `pyproject.toml`:

```toml
[project.optional-dependencies]
dev = [
    "ruff>=0.6",
    "pytest>=8.0",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

**Change 2** — create `tests/conftest.py`. The key problem it solves: `settings` is a
frozen module-level singleton, and `db.py` binds it into its own namespace with
`from .config import settings`. Patch the object on `money_mover.db` using
`dataclasses.replace` (works on frozen dataclasses).

```python
from __future__ import annotations

import dataclasses

import pytest

from money_mover import db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Point every get_conn() call in a test at a fresh throwaway SQLite file."""
    monkeypatch.setattr(
        db, "settings", dataclasses.replace(db.settings, db_path=tmp_path / "test.db")
    )
    db.init_db()
    yield


@pytest.fixture
def conn():
    """A connection to the throwaway DB, for arranging fixture rows."""
    with db.get_conn() as c:
        yield c


@pytest.fixture
def make_account(conn):
    def _make(account_id, *, kind="depository", name=None, item_id="item1",
              institution="Test Bank", exclude=0, subtype=None):
        conn.execute(
            "INSERT OR IGNORE INTO items (item_id, access_token, institution) VALUES (?, ?, ?)",
            (item_id, "tok", institution),
        )
        conn.execute(
            "INSERT INTO accounts (account_id, item_id, name, mask, kind, subtype, "
            "iso_currency, exclude_from_net_worth) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (account_id, item_id, name or account_id, "0000", kind, subtype, "USD", exclude),
        )
        return account_id
    return _make


@pytest.fixture
def make_balance(conn):
    def _make(account_id, snapshot_date, current, available=None):
        conn.execute(
            "INSERT INTO balances (account_id, snapshot_date, current, available) "
            "VALUES (?, ?, ?, ?)",
            (account_id, snapshot_date, current, available),
        )
    return _make


@pytest.fixture
def make_txn(conn):
    def _make(transaction_id, account_id, date, amount, *, name="TXN",
              merchant=None, primary=None, detailed=None, override=None):
        conn.execute(
            "INSERT INTO transactions (transaction_id, account_id, date, name, merchant, "
            "amount, iso_currency, category_primary, category_detailed, override_category) "
            "VALUES (?, ?, ?, ?, ?, ?, 'USD', ?, ?, ?)",
            (transaction_id, account_id, date, name, merchant, amount,
             primary, detailed, override),
        )
    return _make
```

**Change 3** — create `tests/test_categorize.py` as a first smoke test over pure logic
(no DB needed), covering at least: a merchant keyword rule beating a Plaid rule
(`friendly_category(primary="GENERAL_SERVICES", detailed=None, merchant="Tesla Supercharger")`
→ `"Tesla Charging"`), a Plaid detailed mapping, and the `"Other"` fallback.

**Done when**
- [ ] `uv sync --extra dev` succeeds.
- [ ] `uv run pytest` collects and passes at least 3 tests.
- [ ] The `temp_db` fixture is autouse, so no test can touch `data/money-mover.db`.
      Verify by running the suite and confirming `data/` mtime is unchanged.

---

# Phase 3 — Data-correctness bugs

> For each task in this phase: **write the failing test first**, confirm it fails,
> then apply the fix, then confirm it passes.

## Task 3.1 — Use per-account latest balances, not a global MAX

**Severity: high — silently wrong numbers.**

Every "latest balance" query filters on a single **global** `MAX(snapshot_date)` across
the whole `balances` table. Any account whose most recent snapshot is older than that
global max — because its institution errored, or it was linked on a different day —
disappears entirely rather than falling back to its last known balance.

Reproduced (`a1` synced 2026-08-29, `a2` synced 2026-08-20, using the exact dashboard
query from `app.py:33`):
```
dashboard shows: [{'name': 'Chase', 'current': 100.0, 'snapshot_date': '2026-08-29'}]
```
The Fidelity account is gone. The `OR b.snapshot_date IS NULL` clause looks like it
preserves the LEFT JOIN, but an account that *has* balances on an older date produces a
non-NULL row failing both conditions.

**Call sites to fix:**

| File:line | Function |
|---|---|
| `app.py:29` | `dashboard()` account table |
| `analytics.py:690` | `_latest_snapshot_date()` |
| `analytics.py:768` | `list_plan_allocations()` |
| `analytics.py:822` | `_plan_allocation_rows()` |
| `analytics.py:886` | `portfolio_by_sector()` — `cash_rows` and `no_holdings_rows` |
| `analytics.py:1010` | `assets_in_sector()` — `cash_rows` and `no_holdings_rows` |
| `analytics.py:42` | `net_worth_series()` — different fix, see below |

**Change A** — add a shared SQL fragment near the top of `analytics.py` (after the
`LIABILITY_KINDS` constants):

```python
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
```

Then rewrite each call site to `LATEST_BALANCES_CTE + "SELECT ... FROM latest_balances b ..."`
and drop the `WHERE b.snapshot_date = ?` / `= (SELECT MAX(...))` predicate and its
parameter. `_latest_snapshot_date()` becomes unnecessary at those call sites — delete it
once nothing references it.

For `app.py:29`, the same CTE applies; keep the `LEFT JOIN` so an account with *no*
balance row at all still renders. Import the constant from `analytics` or duplicate the
fragment locally — prefer importing.

**Change B** — `net_worth_series()` (`analytics.py:42`) needs carry-forward, not just a
latest-row filter: it currently does `GROUP BY b.snapshot_date`, so a date on which only
one account synced renders as a cliff in the chart. Replace the SQL aggregation with a
Python carry-forward, which is clearer and directly testable:

```python
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
```

Keep the existing "seed an empty starting point" block that follows.

Note this now uses the `ASSET_KINDS` / `LIABILITY_KINDS` constants (lines 22–23), which
the old inline SQL duplicated as string literals.

**Tests to add** (`tests/test_net_worth.py`, `tests/test_portfolio.py`):
- Two accounts with snapshots on different dates → both appear in the dashboard query,
  and the later date's net worth includes the older account's carried-forward balance.
- An account with no balance row at all still appears in the dashboard query.
- `portfolio_by_sector()` total includes a depository account last synced a week ago.

**Done when**
- [ ] No query outside `LATEST_BALANCES_CTE` contains `MAX(snapshot_date) FROM balances`.
- [ ] `_latest_snapshot_date` is deleted or has no remaining callers.
- [ ] New tests pass and fail against the pre-fix code.

---

## Task 3.2 — Fix the 500 on an out-of-range month

Verified:
```
period accepted: 2026-13
BUDGET CRASH: ValueError month must be in 1..12, not 13
```

`api.py:21 _PERIOD_RE = r"^\d{4}-\d{2}$"` accepts month 13. `spending_for_period`
(`analytics.py:344`) catches the resulting `ValueError`, but `budget_progress`
(`analytics.py:413`) re-implements the same parsing with the `try` wrapping only the
`int()` calls, not `date(y, m, 1)`. So `GET /api/budgets?period=2026-13` returns a 500.

**Change A** — tighten the regex in `api.py:21`:

```python
_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
```

**Change B** — in `analytics.py:413 budget_progress`, delete the duplicated parsing and
delegate to `spending_for_period`. Preserve the existing behavior that an `"avg"` period
with no complete months still lists budgets at zero spend:

```python
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
```

**Tests** (`tests/test_budgets.py`):
- `budget_progress("2026-13")` returns `[]` rather than raising.
- `budget_progress("2026-06")` matches `spending_by_friendly_category` over that month.
- `_parse_period("2026-13")` raises `HTTPException` with status 400.

**Done when**
- [ ] `uv run python -c "from money_mover import analytics; analytics.budget_progress('2026-13')"` does not raise.
- [ ] `budget_progress` no longer contains `int(period[:4])`.

---

## Task 3.3 — Remove the transfer-dedup heuristic

**Severity: high — silently deletes real spending.** Read the reasoning carefully before
implementing; this is a deliberate behavior change.

`analytics.py:130 _transfer_duplicates` drops a depository-account charge whenever *any*
credit/loan charge of the same rounded amount lands within ±3 days. Its docstring says
this removes credit-card payments already counted on the card side.

It cannot do that, for two compounding reasons:

1. A credit-card payment appears on the **card** side as a *negative* amount, and
   `_spend_rows` (`analytics.py:110`) already filters to `t.amount > 0`. The card side is
   never in the candidate set.
2. `_spend_rows` filters out `EXCLUDED_SPEND_CATEGORIES` (`TRANSFER_OUT`, `TRANSFER_IN`,
   `LOAN_PAYMENTS`, `INCOME`) *before* calling `_transfer_duplicates` (line 122). So by
   the time the heuristic runs, every remaining row is consumption-categorized.

What it therefore actually matches is two unrelated same-amount purchases — one on a
debit card, one on a credit card, within three days — and it silently drops the debit one
from every spending total, budget, and category drill-down.

The exclusion list at `analytics.py:29` already handles the case this was written for.

**Change** — in `src/money_mover/analytics.py`:
1. Delete the `_transfer_duplicates` function (lines 130–158).
2. In `_spend_rows`, delete the `drop_ids = _transfer_duplicates(filtered)` line and
   return `[dict(r) for r in filtered]`.
3. Delete the now-unused `_DEPOSITORY_KINDS` and `_TRANSFER_WINDOW_DAYS` constants
   (lines 37–38).
4. Update the `_spend_rows` docstring to drop the transfer-pair paragraph, keeping the
   balance-sheet-exclusion explanation.

**Test** (`tests/test_spending.py`): a $42.00 debit-card charge and a $42.00 credit-card
charge two days apart both appear in `spending_by_friendly_category`, and the total is
$84.00.

**Owner check before merging:** this will *increase* reported historical spending totals
by whatever the heuristic was wrongly removing. Run `/api/spending-categories?period=avg`
before and after and note the delta in the commit message so the change is traceable. If
the user wants deduplication retained instead, the correct shape is to match
opposite-signed pairs across the full unfiltered row set — but do not build that without
asking.

**Done when**
- [ ] `grep -rn "_transfer_duplicates\|_TRANSFER_WINDOW_DAYS\|_DEPOSITORY_KINDS" src/` returns nothing.
- [ ] The new test passes.
- [ ] The commit message records the before/after spending delta.

---

## Task 3.4 — Stop duplicating cash holdings on every sync

`db.py:78` declares `holdings` with `PRIMARY KEY (account_id, security_id, snapshot_date)`.
Plaid cash positions have `security_id = NULL`, and SQLite treats NULLs as distinct in
uniqueness checks — so the `ON CONFLICT` clause in `sync.py:236` never fires for them and
every re-sync inserts another row. Same-day re-syncs accumulate duplicate cash rows that
`portfolio_by_sector` sums, inflating the Cash sector.

**Change A** — add a sentinel constant to `src/money_mover/db.py`, above `SCHEMA`:

```python
# Plaid returns NULL security_id for cash positions. SQLite treats NULLs as
# distinct in PRIMARY KEY comparisons, so ON CONFLICT never fires and cash rows
# duplicate on every re-sync. Store this sentinel instead.
CASH_SECURITY_ID = "__cash__"
```

**Change B** — extend the idempotent migration block in `db.py:init_db()` (after the
existing `cursor` migration):

```python
        # Collapse pre-sentinel duplicate cash holdings, then adopt the sentinel.
        conn.execute(
            """
            DELETE FROM holdings
            WHERE security_id IS NULL
              AND rowid NOT IN (
                  SELECT MIN(rowid) FROM holdings
                  WHERE security_id IS NULL
                  GROUP BY account_id, snapshot_date
              )
            """
        )
        conn.execute(
            "UPDATE holdings SET security_id = ? WHERE security_id IS NULL",
            (CASH_SECURITY_ID,),
        )
```

**Change C** — in `sync.py:216 sync_holdings`, substitute the sentinel before the insert.
Add at the top of the `for h in holdings:` loop:

```python
            security_id = h.security_id or CASH_SECURITY_ID
```

and replace every `h.security_id` in that statement's parameter tuple with `security_id`.
Import `CASH_SECURITY_ID` from `.db`.

**Change D** — `analytics._infer_sector` (`analytics.py:~1120`) must classify the sentinel
as cash. It currently receives no `security_id`. Add a keyword parameter and check it
immediately after the `override` check:

```python
def _infer_sector(
    *,
    security_type: str | None,
    ticker: str | None,
    plaid_sector: str | None,
    industry: str | None,
    is_cash_equivalent: bool,
    override: str | None,
    security_id: str | None = None,
) -> str:
    if override:
        return override

    # Cash sentinel rows have no securities join partner.
    if security_id == CASH_SECURITY_ID:
        return CASH_SECTOR
    ...
```

Pass `security_id=r["security_id"]` at both call sites (`portfolio_by_sector` and
`assets_in_sector`). Import `CASH_SECURITY_ID` into `analytics.py`.

**Tests** (`tests/test_holdings_sync.py`):
- Inserting the same cash holding twice for one `(account_id, snapshot_date)` leaves
  exactly one row.
- `init_db()` run against a DB pre-seeded with two NULL-`security_id` rows for the same
  account and date collapses them to one and rewrites it to `__cash__`.

**Done when**
- [ ] `SELECT COUNT(*) FROM holdings WHERE security_id IS NULL` is 0 after `init_db()`.
- [ ] Re-running a holdings sync twice for one date does not change the row count.
- [ ] Portfolio Cash total is unchanged by a repeated same-day sync.

---

## Task 3.5 — Use Plaid's real item_id

`sync.py:13` invents `item_id = secrets.token_hex(8)` instead of using the `item_id`
returned by the token exchange. Consequences:

- Re-linking the same institution creates a **second** `items` row holding a duplicate
  access token.
- The account upsert at `sync.py:56` does not update `item_id`, so accounts stay bound to
  the *old* item row. `unlink_item` on the new item deletes nothing; on the old one it
  deletes accounts the user believes they relinked.

**Change A** — in `src/money_mover/plaid.py`, return both fields from the exchange:

```python
@dataclass(frozen=True)
class LinkedItem:
    item_id: str
    access_token: str


def exchange_public_token(public_token: str) -> LinkedItem:
    """Exchange a Link public token for the item's id and reusable access token."""
    client = _client()
    request = ItemPublicTokenExchangeRequest(public_token=public_token)
    response = client.item_public_token_exchange(request)
    return LinkedItem(
        item_id=response["item_id"],
        access_token=response["access_token"],
    )
```

**Change B** — in `sync.py:10 link_item`, use it and make the insert idempotent:

```python
def link_item(public_token: str, institution: str | None = None) -> str:
    """Exchange a public token from Plaid Link and persist the item."""
    linked = plaid.exchange_public_token(public_token)
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO items (item_id, access_token, institution) VALUES (?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                access_token=excluded.access_token,
                institution=COALESCE(excluded.institution, items.institution)
            """,
            (linked.item_id, linked.access_token, institution),
        )
    sync_item(linked.item_id)
    return linked.item_id
```

**Change C** — in the accounts upsert (`sync.py:56`), add `item_id=excluded.item_id` to
the `DO UPDATE SET` list so a re-linked account follows its new item.

**Change D** — remove `import secrets` from `sync.py` if nothing else uses it.

**Not retroactive.** Existing rows keep their synthetic hex ids and keep working; only
newly linked items get real Plaid ids. Do not write a migration for this — mapping old
ids to real ones requires live Plaid calls. Note the limitation in the commit message.

**Test** (`tests/test_sync.py`): monkeypatch `plaid.exchange_public_token` to return a
fixed `LinkedItem` and `plaid.get_accounts` / `get_transactions` / `get_holdings` to
return empty results; call `link_item` twice with the same item_id and assert
`SELECT COUNT(*) FROM items` is 1.

**Done when**
- [ ] `grep -n "secrets" src/money_mover/sync.py` returns nothing.
- [ ] Linking the same item twice yields one `items` row.
- [ ] `api.py:102 exchange_public_token` route still returns `{"item_id": ..., "status": "linked"}`.

---

## Task 3.6 — Fix the inflated loan balance history

`analytics.py:1408 loan_balance_history` computes
`running = anchor_total + total_paid` over **all** payments, but the loop below only
subtracts payments with `payment_date < anchor_date`. Any payment dated on or after the
latest snapshot leaves every historical point high by that amount, with a discontinuous
drop at the anchor.

This is reachable in normal use: `record_loan_payment` only writes a balance snapshot
when the optional `new_balance` is supplied, so logging a payment without entering a new
balance produces exactly this state.

**Change** — one line:

```python
    total_paid = sum(
        float(p["amount"] or 0.0)
        for p in payments
        if p["payment_date"] < anchor_date
    )
```

**Test** (`tests/test_debt.py`): one loan, snapshot of 10000 on `2026-08-01`, payments of
500 on `2026-06-01`, `2026-07-01`, and `2026-08-15`. Assert the series is
`[11000, 10500, 10000]` at dates `2026-06-01`, `2026-07-01`, `2026-08-01` — i.e. the
post-anchor payment does not shift the earlier points.

**Done when**
- [ ] The test passes and fails against the pre-fix code.

---

## Task 3.7 — Use real month arithmetic in the payoff projection

`analytics.py:1446 project_payoff` advances the schedule with
`today + timedelta(days=30 * m)`, drifting roughly six months over a 30-year horizon.
`_add_months` already exists at `analytics.py:280`.

**Change** — replace both sites:
- inside the month loop: `d = _add_months(today, m)`
- the final `payoff_date`: `_add_months(today, months_to_payoff).isoformat()`

Note that `_add_months` returns the first of the target month; that is fine for a
projection and is the intended simplification.

**Also in the same function**, two small cleanups:
- `unprojectable = [ln.name for ln in loans if ln not in projectable]` is an O(n²) scan
  using dataclass value equality. Replace with an id set:
  ```python
  projectable_ids = {ln.loan_id for ln in projectable}
  unprojectable = [ln.name for ln in loans if ln.loan_id not in projectable_ids]
  ```
- `extra_by_loan` is keyed by `id(s)`. Key it by list index instead
  (`for idx, s in enumerate(active)`), which is stable and readable.

**Also fix two payoff edge cases in the same function:**

*Off-by-one when a lump sum clears everything.* When `extra_onetime` zeroes every loan
before the loop, the loop still runs once, finds `total < 0.01`, and reports one month of
remaining payments on an already-cleared portfolio. Verified:

```
months_to_payoff: 1  payoff_date: 2026-09-28
schedule[0]: {'month_offset': 0, 'date': '2026-08-29', 'total_balance': 0.0}
```

Add an early return after `total_schedule` is seeded and before the `for m in range(...)`
loop:

```python
    if total_schedule[0]["total_balance"] < 0.01:
        return {
            "schedule": total_schedule,
            "months_to_payoff": 0,
            "payoff_date": today.isoformat(),
            "total_interest": 0.0,
            "unprojectable": unprojectable,
        }
```

*Per-loan `schedule` seeded wrong.* `analytics.py:~1508` initializes each loan's
`"schedule": [0.0]` with the comment "index 0 = start", but seeds `0.0` rather than the
starting balance; it is only corrected inside the `extra_onetime > 0` branch. Nothing
reads this field today (the returned `schedule` is built from `total_schedule`), so it is
latent — but seed it correctly with `round(float(ln.current_balance or 0.0), 2)`, or drop
the field entirely if you confirm no reader.

**Tests** (`tests/test_debt.py`):
- A loan with a known amortization reaches `months_to_payoff` at the expected month, and
  `payoff_date` is exactly that many calendar months after today's first-of-month.
- `project_payoff(extra_onetime=<more than the total balance>)` returns
  `months_to_payoff == 0` and `payoff_date == today`.

**Done when**
- [ ] `grep -n "timedelta(days=30" src/money_mover/analytics.py` returns nothing.
- [ ] `grep -n "id(s)" src/money_mover/analytics.py` returns nothing.
- [ ] A lump sum covering the full balance reports 0 months, not 1.

---

## Task 3.8 — Fix the budgets period picker

Two defects in the period selector, both in `static/app.js`.

**A. It always opens on "Average (all months)".** `app.js:153`:

```js
sel.value = currentPeriod || sel.value || "avg";
```

By this point `sel.innerHTML` has already been assigned with `<option value="avg">` first,
so `sel.value` is `"avg"` and the `|| "avg"` fallback is unreachable. On first load
`currentPeriod` is `""`, so the page always lands on the average view. That contradicts
three other places: the `loadBudgets` comment ("default = current month"), the static
`<h3>This Month's Spending</h3>` in `budgets.html`, and `_parse_period(None)` in
`api.py:29`, which defaults to the current `YYYY-MM`.

Fix: compute the current `YYYY-MM`, use it when it appears in the fetched month list, and
fall back to `"avg"` only when it does not:

```js
  const now = new Date();
  const thisMonth = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
  const hasThisMonth = months.some((m) => m.period === thisMonth);
  sel.value = currentPeriod || (hasThisMonth ? thisMonth : "avg");
```

**B. Every specific month is labelled "This Month's Spending".** `updateBudgetHeading`
(`app.js:162`) only branches on `currentPeriod === "avg"`, so selecting "March 2026"
displays March's numbers under a heading claiming they are the current month, with a
`Total:` label. Use the selected option's own label:

```js
function updateBudgetHeading() {
  const sel = document.getElementById("budget-period-select");
  const label = sel?.selectedOptions[0]?.textContent || "";
  const heading = document.getElementById("spending-heading");
  const totalLine = document.getElementById("spending-total-line");
  const isAvg = currentPeriod === "avg";
  if (heading) {
    heading.textContent = isAvg ? "Average Monthly Spending" : `${label} Spending`;
  }
  if (totalLine) {
    const total = currentSpendingCategories.reduce((s, c) => s + c.total, 0);
    totalLine.textContent = isAvg ? `Avg/month: ${fmt(total)}` : `Total: ${fmt(total)}`;
  }
}
```

Also update the static `<h3>` in `templates/budgets.html` so the pre-JS render is not
misleading — make it neutral (`Spending`) since JS replaces it on load.

**Done when**
- [ ] Loading `/budgets` with transactions in the current month selects that month, not "avg".
- [ ] Selecting an older month updates the heading to that month's name.
- [ ] Selecting "Average (all months)" still reads "Average Monthly Spending" / "Avg/month:".

---

## Task 3.9 — Guard the month label lookup in `spending_months`

`analytics.py:340`: `_MONTH_LABELS[m - 1]` sits **outside** the `try` that guards the
`int()` conversions. A stored date whose `substr(date, 1, 7)` yields month 13+ raises an
uncaught `IndexError`, and month `00` silently wraps to `_MONTH_LABELS[-1]` = "December".

This one is load-bearing: `/api/spending-months` is awaited by `loadPeriodSelect`, so a
500 here takes down the entire budgets page, not just the dropdown.

**Change:**

```python
    for r in rows:
        ym = r["ym"]
        try:
            y, m = int(ym[:4]), int(ym[5:7])
        except (ValueError, IndexError):
            continue
        if not 1 <= m <= 12:
            continue
        out.append({"period": ym, "label": f"{_MONTH_LABELS[m - 1]} {y}"})
```

**Test** (`tests/test_budgets.py`): insert a transaction with date `2026-13-01` and assert
`spending_months()` skips it rather than raising.

**Done when**
- [ ] `spending_months()` never raises regardless of stored date values.

---

## Task 3.10 — Make the "avg" period's counts and drill-down honest

> **Changes what the UI displays. Confirm the choice with the owner before implementing.**

Two related presentation defects when the period is `"avg"`:

**A. Averaged dollars beside an un-averaged count.**
`average_monthly_spending_by_friendly_category` (`analytics.py:363`) divides `total` by the
month count but leaves `transaction_count` as the raw multi-month total
(`analytics.py:387-393`). The budgets table renders `fmt(c.total)` next to `c.count`, so a
row reads "$120 / 45 txns" — a one-month figure beside a three-month count.

There is a second-order effect: the "Set budget" suggestion is
`Math.ceil(c.total / 50) * 50` (`app.js:202`), computed from whichever scale is active. The
suggested limit silently changes meaning with the dropdown.

*Recommended fix:* divide the count too, so both columns are per-month:
`transaction_count=round(agg["n"] / months)`. The alternative — relabelling the column
header per mode — leaves the budget suggestion ambiguous, so prefer dividing.

**B. The drill-down shows a multi-month union under an averaged total.**
`transactions_for_period_category("avg", ...)` (`analytics.py:398`) returns every matching
transaction across all complete months. Clicking a category showing "$200" opens a list
totalling ~$600, with no period context in the panel title (`Transactions in "X"`).

*Recommended fix:* keep the union (it is the useful view) but label it. Pass the month span
to the frontend and render the panel title as
`Transactions in "X" — 3 months (Jun–Aug 2026)`, so the sum not matching the row is
self-explanatory. `_complete_month_range()` already returns the span.

**Done when**
- [ ] In "avg" mode, the dollar and count columns are on the same time basis.
- [ ] The drill-down panel states its period span whenever it is not a single month.
- [ ] The owner has signed off on which of the two options in (A) was taken.

---

# Phase 4 — Security and hygiene

## Task 4.1 — Apply `escapeHtml` consistently in `app.js`

`static/app.js:796` defines `escapeHtml`, and the debt tables use it — but the older
sections interpolate raw values into `innerHTML`, including into attribute positions.
Merchant names come from Plaid; category, sector, and loan labels are user-entered. A
value containing a quote visibly breaks the reclassify UI.

**Unescaped sinks to fix** (all in `src/money_mover/static/app.js`):

| Line | Value |
|---|---|
| 254 | `data-merchant="${...}"` — hand-rolled `.replace(/"/g, "&quot;")`, replace with `escapeHtml` |
| 255 | `<td>${t.merchant || t.name}</td>` |
| 288 | `<option value="${o}" ...>${o}</option>` in the reclassify select |
| 297 | `All from "${merchant}"` button label |
| 338 | `<option value="${o}">` datalist |
| 655 | `<option value="${s}">` sector datalist |
| 669 | plan-allocation labels in the `Object.entries(byAccount)` block |

Also audit lines 196, 349, 382, 438, 489, 535, 586 for interpolated server values and
wrap each in `escapeHtml`.

**Change** — move `escapeHtml` (line 796) to the top of the file, next to `fmt`, so it is
defined before every use. Wrap every interpolation of a server-supplied string. Leave
numeric interpolations (`fmt(...)`, `l.interest_rate`) alone.

**Done when**
- [ ] Every `${...}` inside a template literal assigned to `innerHTML` is either a number,
      a literal, or wrapped in `escapeHtml`.
- [ ] Manually: set a merchant rule with pattern `a"b<i>` and confirm the vendor-rules
      table renders the text literally rather than breaking the markup.

---

## Task 4.2 — Vendor Chart.js and pin the Plaid script

`templates/base.html:8-9` loads two third-party scripts with no `integrity` attribute,
into the page holding the user's entire financial picture.

**Change:**
1. Download Chart.js 4.4.1 UMD build to `src/money_mover/static/vendor/chart.umd.min.js`
   and change line 8 to `<script src="/static/vendor/chart.umd.min.js"></script>`.
2. The Plaid Link script must stay remote (it is versioned and served from Plaid). Add
   `crossorigin="anonymous"` and leave a comment noting SRI is not possible because Plaid
   ships a mutable `stable` URL.
3. Add `src/money_mover/static/vendor/` to the wheel package data if needed — verify
   `uv build` still includes it.

**Done when**
- [ ] The dashboard renders charts with the network disconnected from jsdelivr.
- [ ] `grep -n "cdn.jsdelivr" src/money_mover/templates/` returns nothing.

---

## Task 4.3 — Let Pydantic parse dates

`api.py` has three unguarded `date.fromisoformat` calls that turn a malformed date into a
500 instead of a 422: lines 132–133 (`spending`), 398 (`save_loan`), 438
(`save_loan_payment`).

**Change:**
- `LoanReq.next_due_date: date | None = None` and `LoanPaymentReq.payment_date: date`
  (import `date` from `datetime`, already imported at `api.py:4`). Delete the manual
  `date.fromisoformat` calls in both handlers and pass `req.next_due_date` /
  `req.payment_date` straight through.
- For the `spending` query params, change the signature to
  `def spending(start: date | None = None, end: date | None = None)` — FastAPI parses and
  validates query params by type. Delete the manual parsing.

**Done when**
- [ ] `POST /api/loan-payments` with `payment_date: "not-a-date"` returns 422, not 500.
- [ ] `GET /api/spending?start=nope` returns 422, not 500.
- [ ] `grep -n "date.fromisoformat" src/money_mover/api.py` returns nothing.

---

## Task 4.4 — Move personal data out of source

Three constants in `analytics.py` are the user's private financial details baked into
tracked code:

- `DEFAULT_PLAN_ALLOCATIONS` (line 716) — employer 401(k) plan name and exact fund
  percentages.
- `ETF_SECTOR_MAP` (line ~648) — tuned to this specific portfolio.
- `COMPLETE_MONTHS_START = (2026, 5)` (line 266) — this user's data start date.

`.env` and `data/` are correctly gitignored; this is the same category of data sitting in
git history.

**Change:**
1. `COMPLETE_MONTHS_START` — derive it instead of hardcoding. `_complete_month_range`
   already queries `MIN(date) FROM transactions`; make that the sole lower bound and
   delete the constant. This also removes a value the user would otherwise have to
   remember to update.
2. `DEFAULT_PLAN_ALLOCATIONS` — move to a gitignored `data/plan-allocations.json` read by
   `seed_default_plan_allocations`, with the file absent meaning "seed nothing". Add
   `data/plan-allocations.example.json` with obviously-fake values, and add a README line.
3. `ETF_SECTOR_MAP` — leave in source for now (ticker→sector is public reference data, not
   personal), but add a comment saying so, so the distinction is deliberate.

**Done when**
- [ ] `grep -n "ARCHER" src/` returns nothing.
- [ ] `COMPLETE_MONTHS_START` no longer exists.
- [ ] `seed_default_plan_allocations()` returns 0 and does not raise when the JSON file is absent.
- [ ] `data/plan-allocations.example.json` is tracked; the real file is not.

---

## Task 4.5 — Note the plaintext access tokens

`items.access_token` stores Plaid access tokens in plaintext. That is a defensible
tradeoff for a local-only app, but it should be stated rather than implied.

**Change** — add to the README `## Notes` section:

```markdown
- Plaid access tokens are stored unencrypted in `data/money-mover.db`. Anyone with read
  access to that file can pull your account data from Plaid. Keep it off shared drives
  and out of backups you don't control.
```

**Done when**
- [ ] The note is present in `README.md`.

---

# Phase 5 — Refactors

> These are behavior-preserving. The Phase 3 tests are the safety net; do not start
> Phase 5 until `uv run pytest` is green.

## Task 5.1 — Unify `portfolio_by_sector` and `assets_in_sector`

**Highest-value cleanup in the repo: ~200 duplicated lines.**

`analytics.py:886 portfolio_by_sector` and `analytics.py:1010 assets_in_sector` run the
same four queries (cash, holdings, no-holdings investment accounts, plan allocations) and
the same plan-allocation roll-up, differing only in aggregate-vs-detail output.

**Change** — extract one private builder and derive both public functions from it:

```python
def _portfolio_assets(conn) -> list[AssetDetail]:
    """Every portfolio position as an AssetDetail, sector already resolved.

    Sources, in order: depository cash, investment holdings, investment accounts
    with no holdings, and manual 401(k) plan allocations. Accounts flagged
    exclude_from_net_worth are omitted; accounts with plan_allocations rows are
    represented only by their fund breakdown, never twice.
    """
```

Then:
- `assets_in_sector(sector)` → `[a for a in _portfolio_assets(conn) if a.sector == sector]`,
  sorted by `-value`. Note the current version only emits cash rows when
  `sector == CASH_SECTOR`; the unified version filters on the resolved sector, which is
  equivalent.
- `portfolio_by_sector()` → group `_portfolio_assets(conn)` by `.sector`, summing `.value`
  and counting, sorted by `-total`.

Preserve the `if val <= 0: continue` skip in the builder — both current versions do it.

**Done when**
- [ ] Both functions are under 20 lines.
- [ ] For every sector returned by `portfolio_by_sector()`, the sum of
      `assets_in_sector(sector)` values equals that sector's total (add this as a test).
- [ ] Portfolio totals are byte-identical to pre-refactor output on the real DB. Capture
      `curl -s localhost:8000/api/portfolio` before and after and diff.

---

## Task 5.2 — Split `analytics.py`

At 1635 lines it covers four independent domains sharing almost nothing.

**Change** — split into a package, keeping import paths working:

```
src/money_mover/analytics/
    __init__.py     # re-export the public API so `from . import analytics` keeps working
    spending.py     # _spend_rows, spending_by_*, transactions_for_*, subscriptions
    budgets.py      # period helpers, budget_progress, budget/merchant-rule CRUD
    portfolio.py    # sectors, holdings, plan allocations, net_worth_series
    debt.py         # loans, payments, history, project_payoff
```

`__init__.py` should re-export every name currently imported by `api.py` and `app.py` —
grep for `analytics\.` across those two files to build the list. Do not change any
function bodies in this task; it is a pure move.

**Done when**
- [ ] `grep -rn "analytics\." src/money_mover/api.py src/money_mover/app.py` resolves — the app imports and all routes respond.
- [ ] No module exceeds 600 lines.
- [ ] `uv run pytest` is green with no test file changes beyond import paths.

---

## Task 5.3 — Collapse the hand-written API serialization

`api.py` hand-writes a dict comprehension for every response — roughly ten of them
(lines 134, 156, 174, 191, 249, 274, 286, 379, 422). The returns are already frozen
dataclasses.

**Change** — add one helper and use it throughout:

```python
def _serialize(obj) -> dict:
    """dataclasses.asdict with dates rendered as ISO strings."""
    return {
        k: v.isoformat() if isinstance(v, date) else v
        for k, v in dataclasses.asdict(obj).items()
    }
```

Two call sites rename fields (`transaction_count` → `count` at lines 135 and 157). Keep
those renames — the frontend depends on them (`app.js` reads `c.count`). Either keep those
two comprehensions or apply the rename after `_serialize`; do not silently change the
wire format.

**Done when**
- [ ] Every route's JSON output is unchanged — capture each `/api/*` response before and
      after and diff.
- [ ] `api.py` is under 400 lines.

---

## Task 5.4 — Fix Plaid client lifecycle and move `create_link_token`

Two problems:
- `plaid.py:142 _client()` constructs a new `ApiClient` on every call and never closes it,
  leaking a urllib3 connection pool per sync. `AGENTS.md` states "use context managers for
  resource cleanup" as a project convention.
- `api.py:82-90` duplicates the entire client construction inline and imports the private
  `_env_to_plaid_environment` (`api.py:17`).

**Change:**
1. In `plaid.py`, cache the client with `functools.lru_cache(maxsize=1)` on a
   `_build_client()` helper; keep `_client()` as the public-ish accessor that raises the
   credentials error when config is missing (do not cache the error path).
2. Move link-token creation into `plaid.py` as `create_link_token() -> str`, moving the
   `LinkTokenCreateRequest` imports with it.
3. In `api.py`, reduce the route to a credentials check plus
   `return LinkTokenResp(link_token=plaid_wrapper.create_link_token())`. Delete the
   `from .plaid import _env_to_plaid_environment` import and the direct `import plaid`.

Watch the name collision: `api.py` currently does `import plaid` (the SDK) while the local
wrapper is also `money_mover.plaid`. After this task `api.py` should import only the local
wrapper.

**Done when**
- [ ] `grep -n "^import plaid" src/money_mover/api.py` returns nothing.
- [ ] `grep -n "_env_to_plaid_environment" src/money_mover/api.py` returns nothing.
- [ ] `plaid.Configuration(` appears exactly once in the codebase.
- [ ] Two consecutive syncs create one `ApiClient`, not two (assert via a monkeypatched counter).

---

## Task 5.5 — Delete dead code and inline imports

All small and independent; do them in one commit.

- **`categorize.py:3-27`** — `KEYWORD_RULES` and `infer_category` have no callers
  (`friendly_category` superseded them). Delete both.
- **`main.py:6 run()`** duplicates `app.py:100 run()` verbatim. `pyproject.toml` points the
  console script at `money_mover.app:run`; the README documents `python main.py`. Keep
  `app.py:run` and make `main.py` a two-line shim that imports and calls it.
- **Inline imports** — `import uuid` inside `upsert_loan` (line 1287) and
  `record_loan_payment` (line 1375). Move to module scope.
- **`analytics.py:1177 set_holding_sector_override`** returns silently for cash positions
  while `api.py:309` still responds `{"status": "saved"}`. Raise `ValueError` and let the
  route translate it to a 400.

**Done when**
- [ ] `grep -rn "infer_category\|KEYWORD_RULES" src/` returns nothing.
- [ ] `grep -rn "^\s\+import " src/money_mover/analytics.py` returns nothing (no function-level imports).
- [ ] `PATCH /api/portfolio/assets/sector` with a null `security_id` returns 400.

---

## Task 5.6 — Replace the deprecated startup hook

`app.py:24 @app.on_event("startup")` is deprecated in current FastAPI.

**Change:**

```python
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Idempotently seed curated 401(k) default allocations at startup."""
    analytics.seed_default_plan_allocations()
    yield


app = FastAPI(title="Money Mover", lifespan=lifespan)
```

Note `lifespan` must be defined before `app`, so it moves above the `app = FastAPI(...)`
line and the `app.mount` / `include_router` calls stay where they are.

**Done when**
- [ ] `grep -n "on_event" src/money_mover/app.py` returns nothing.
- [ ] Starting the server emits no `DeprecationWarning`.

---

## Task 5.7 — Avoid nested connections in `budget_progress`

`analytics.py:413 budget_progress` holds a `get_conn()` context while calling
`_complete_month_range()`, which opens its own connection. It works today (SQLite allows
concurrent readers) but is a latent lock hazard once anything writes concurrently.

**Change** — give `_complete_month_range` an optional `conn` parameter, defaulting to
opening its own, and pass the caller's connection from `budget_progress`.

**Done when**
- [ ] `budget_progress` opens exactly one connection.

---

# Phase 6 — Documentation

## Task 6.1 — Mark the OpenRouter section in `AGENTS.md` as unbuilt

Roughly a third of `AGENTS.md` specifies an OpenRouter LLM-categorization integration —
config keys, an `llm.py` module, schema migrations — none of which exists in the code. It
is written in the present tense and reads as implemented, which will mislead both the user
and any agent reading the file.

**Change:**
- Retitle `## OpenRouter Integration: LLM-Based Categorization` to
  `## PROPOSED (not implemented): OpenRouter LLM categorization`.
- Add a first line: `> Nothing in this section exists in the codebase today. It is a design sketch, not documentation.`
- Fix `### API Integration Pattern (Plaid → LLM)` — the "→ LLM" half is aspirational.
  Retitle to `### API Integration Pattern`.
- Verify the rest of the file against the code and correct anything else that drifted
  (in particular the "Conventions" section's resource-management claim, which Task 5.4
  makes true).

**Done when**
- [ ] Every present-tense claim in `AGENTS.md` is verifiable against the code.

---

## Task 6.2 — Refresh the README

**Change:**
- Update the setup step to match the env vars from Task 1.2.
- Add `docs/plans/` to the `## Layout` tree.
- Add a `## Development` section: `uv sync --extra dev`, `uv run pytest`, `uv run ruff check .`.
- Add the access-token note from Task 4.5 if not already done.

**Done when**
- [ ] Following the README from a clean clone produces a running app.

---

# Optional follow-ups (not cleanup — confirm with the owner first)

These are real gaps, but they add functionality rather than clean up what exists. Do not
start them as part of this plan.

- **`exclude_from_net_worth` has no write path.** It is read in eight places and rendered
  in `dashboard.html:61`, but there is no endpoint and no UI — it can only be set with
  manual SQL. Would need `PATCH /api/accounts/{id}` plus a dashboard checkbox.
- **No authentication on any endpoint,** including `DELETE /api/items/{item_id}`. The
  README's "do not expose this" warning mostly covers it, but a page the user visits can
  issue a cross-origin `POST /api/sync` as a simple request without triggering preflight.
  A shared-secret header or an `Origin` check would close it cheaply.
- **`record_loan_payment` with a back-dated payment plus `new_balance`** writes a snapshot
  at that past date, which can become an out-of-order anchor for `loan_balance_history`.
  Task 3.6 fixes the arithmetic but not this ordering case.
- **`db.get_conn()` calls `init_db()` on every connection,** re-running the full schema
  script and the migration `PRAGMA` checks per request. Harmless but wasteful; a
  module-level "already initialized" flag would remove it.

---

# Completion checklist

- [ ] Phase 1 — 1.1, 1.2
- [ ] Phase 2 — 2.1
- [ ] Phase 3 — 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10
- [ ] Phase 4 — 4.1, 4.2, 4.3, 4.4, 4.5
- [ ] Phase 5 — 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7
- [ ] Phase 6 — 6.1, 6.2
- [ ] `uv run ruff check .` clean
- [ ] `uv run pytest` green
- [ ] App boots from a clean clone and the dashboard, budgets, portfolio, and debt pages render

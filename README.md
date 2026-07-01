# Money Mover

A local personal finance dashboard inspired by Rocket Money. Syncs credit cards,
brokerages, and student loans via Plaid and renders net worth, spending, and
budgets in a browser UI.

## Features (MVP)

- **Net worth over time** — assets (depository + investment) minus liabilities (credit + loan) across balance snapshots.
- **Spending by category** — categorized transaction breakdown (doughnut chart).
- **Budgets vs actuals** — set monthly per-category limits and track progress.

## Setup

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```bash
# 1. Install deps
uv sync

# 2. Configure Plaid credentials
cp .env.example .env
#   fill in PLAID_CLIENT_ID, PLAID_SECRET from https://dashboard.plaid.com
#   set PLAID_ENV to sandbox | development | production

# 3. Run
uv run python main.py
# open http://127.0.0.1:8000
```

## Usage

1. Click **Link Account** to connect a card, brokerage, or loan through Plaid Link.
2. Click **Sync** to pull the latest balances and transactions.
3. Visit **Budgets** to set per-category monthly limits.

## Layout

```
src/money_mover/
  app.py          FastAPI app + page routes
  api.py          JSON API + Plaid Link endpoints
  plaid.py        Plaid SDK wrapper
  sync.py         Plaid -> SQLite sync
  analytics.py    net worth, spending, budget queries
  categorize.py   keyword-based category fallback
  db.py           SQLite schema
  templates/      Jinja2 pages
  static/         CSS + JS (Chart.js)
```

## Notes

- All data is stored locally in `data/money-mover.db` (gitignored).
- In sandbox mode use Plaid's [test credentials](https://plaid.com/docs/sandbox/test-credentials/).
- This is a personal local app — do not expose it to the public internet as-is.

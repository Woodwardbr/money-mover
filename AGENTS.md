# Money Mover — AI Agent Guide

A local personal finance dashboard syncing credit cards, brokerages, and loans via Plaid with local SQLite storage.

## Quick Start for Agents

**Project root commands:**
```bash
uv sync              # Install dependencies (Python 3.13+)
uv run python main.py   # Start dev server (http://127.0.0.1:8000)
```

**Key files to understand:**
- [src/money_mover/app.py](src/money_mover/app.py) — FastAPI routes & page rendering
- [src/money_mover/api.py](src/money_mover/api.py) — JSON API endpoints (Plaid Link, budget CRUD)
- [src/money_mover/sync.py](src/money_mover/sync.py) — Plaid transaction sync pipeline
- [src/money_mover/analytics.py](src/money_mover/analytics.py) — Spending queries & aggregations
- [src/money_mover/categorize.py](src/money_mover/categorize.py) — Keyword-based category fallback
- [src/money_mover/db.py](src/money_mover/db.py) — SQLite schema & connection management

## Architecture

### Data Flow: Plaid → SQLite → Analytics → UI

1. **Sync** ([sync.py](src/money_mover/sync.py)): Exchanges Plaid public tokens → access tokens, streams transactions with pagination
2. **Store** ([db.py](src/money_mover/db.py)): Writes accounts, balances, transactions to SQLite with Plaid's category metadata
3. **Query** ([analytics.py](src/money_mover/analytics.py)): Aggregates spending/budgets with category priority: `override_category > category_primary > 'Uncategorized'`
4. **Render** ([app.py](src/money_mover/app.py), [templates/](src/money_mover/templates/)): Jinja2 pages with Chart.js visualizations

### Configuration Pattern

Settings are frozen dataclasses loaded once at startup from `.env`:
- **Required**: `PLAID_CLIENT_ID`, `PLAID_SECRET`, `PLAID_ENV` (sandbox/development/production)
- **Optional**: `APP_HOST`, `APP_PORT`, `DB_PATH`

See [config.py](src/money_mover/config.py) — the `_env()` helper handles defaults.

### API Integration Pattern (Plaid → LLM)

Wrapper modules decouple external APIs:
- [plaid.py](src/money_mover/plaid.py): SDK normalization via dataclasses (`AccountSnapshot`, `TransactionRow`)
- Transactions extracted via cursor-based pagination (efficient, resumable)
- Category priority: Plaid's `personal_finance_category` > legacy `category` array

**For new integrations (e.g., OpenRouter):** Create `llm.py` following the same pattern — dataclass responses, error handling, resource management.

## OpenRouter Integration: LLM-Based Categorization

The project is designed to integrate OpenRouter API for improved transaction categorization. Plaid provides initial categories, but keyword fallback and manual correction are the only enrichment today.

### Opportunity: Transaction Enrichment

**Current limitation:** Plaid categories are generic; merchants are often truncated in transaction names.

**Goal:** Use OpenRouter LLM to:
1. Re-categorize transactions with weak Plaid confidence
2. Enrich merchant names ("AMZN" → "Amazon")
3. Detect spending patterns (e.g., recurring subscriptions)

### Implementation Pattern

1. **Add config** ([config.py](src/money_mover/config.py)):
   - `OPENROUTER_API_KEY` (env var)
   - Optional: `OPENROUTER_MODEL` (default: `openrouter/auto`), `OPENROUTER_BATCH_SIZE` (default: 10)

2. **Create wrapper** (`src/money_mover/llm.py`):
   ```python
   # Mirror plaid.py structure
   @dataclass
   class CategorizedTransaction:
       transaction_id: str
       inferred_category: str
       merchant_enriched: str | None
       confidence: float
   
   def categorize_transaction(merchant_name, transaction_name, plaid_category: str) -> CategorizedTransaction:
       # Call OpenRouter with context, cache results
       ...
   ```

3. **Integration point** ([sync.py](src/money_mover/sync.py)):
   - After storing Plaid data, batch-call LLM for weak categories
   - Store in new `llm_category` column OR set `override_category` directly
   - Update analytics query priority: `override_category > llm_category > category_primary`

4. **Database schema extension** ([db.py](src/money_mover/db.py)):
   ```sql
   ALTER TABLE transactions ADD COLUMN llm_category TEXT;
   ALTER TABLE transactions ADD COLUMN llm_confidence REAL;
   ALTER TABLE transactions ADD COLUMN merchant_enriched TEXT;
   ```

### Design Constraints

- **Local-first**: All data stays on user's machine; API calls to Plaid & OpenRouter are essential only
- **Fallback behavior**: If OpenRouter fails/unavailable, system continues with Plaid categories
- **Cost-conscious**: Batch calls; avoid recategorizing manually corrected transactions (`override_category IS NOT NULL`)
- **Late-binding aggregation**: Keep categorization logic at query-time (analytics.py), not during sync, to allow retroactive rule changes

## Conventions

- **Error handling**: Use context managers for resource cleanup (DB connections, API rate limits)
- **Testing**: Plaid sandbox mode enabled via `PLAID_ENV=sandbox` with test credentials
- **Code style**: Ruff (linter/formatter) configured in `pyproject.toml` — line length 100, Python 3.13+
- **Type hints**: Required for all functions; use `from __future__ import annotations` for forward refs
- **No ORM**: Direct SQL; connection context manager at [db.py](src/money_mover/db.py)

## Common Tasks

| Task | How To |
|------|--------|
| Add a new category to keyword fallback | Edit [categorize.py](src/money_mover/categorize.py) `KEYWORD_RULES` |
| Add a new API endpoint | Add route to [api.py](src/money_mover/api.py), follow Pydantic BaseModel pattern |
| Query a new spending insight | Add function to [analytics.py](src/money_mover/analytics.py), use `spending_by_category()` as template |
| Modify dashboard UI | Edit [templates/dashboard.html](src/money_mover/templates/dashboard.html), use Chart.js (already imported) |
| Fix Plaid sync issues | Debug [sync.py](src/money_mover/sync.py) and [plaid.py](src/money_mover/plaid.py) — pagination logic is in `transactions_sync()` cursor handling |

## Resources

- [Plaid API Docs](https://plaid.com/docs/) — focus on `transactions_sync`, account linking flow
- [FastAPI Docs](https://fastapi.tiangolo.com/) — request/response models, dependency injection
- [OpenRouter Docs](https://openrouter.ai/docs) — if implementing LLM categorization
- [SQLite](https://www.sqlite.org/lang.html) — connection at `db.get_conn()`, schema defined in [db.py](src/money_mover/db.py)

# Muse API via Tailscale

This exposes a minimal, token-authenticated API for Muse's monthly payoff workflow.

The Muse API runs as a **separate listener** that serves only the four
`/api/muse/*` routes below. The dashboard and the rest of `/api` stay on
`127.0.0.1`, so Muse cannot reach them even with a valid token. To give Muse a
new capability, add a route to `muse_api.py`; nothing else is exposed.

## What it does

- `GET /api/muse/health` — health check
- `GET /api/muse/payoff-balances` — statement balances for credit cards + monthly payment due for student loans (via Plaid Liabilities)
- `GET /api/muse/spending-summary?period=YYYY-MM` — budgets vs actuals + average monthly spending
- `POST /api/muse/record-payment` — record a payoff payment: `{"account_id": "...", "amount": 123.45, "payment_date": "2026-10-01"}`, optionally with `"loan_id"` to target one tracked loan

All endpoints require header: `X-Muse-Token: <MUSE_API_TOKEN>`

## Setup

1. Generate a token and add to `.env`:
   ```bash
   openssl rand -hex 32
   # add to .env:
   MUSE_API_TOKEN=<output>
   ```

2. Install Tailscale on your local machine (where money-mover runs):
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```

3. Run money-mover with the Muse listener on your Tailscale IP:
   ```bash
   MUSE_HOST=$(tailscale ip -4) uv run python main.py
   ```
   (or set `MUSE_HOST` / `MUSE_PORT` in `.env`; `MUSE_PORT` defaults to 8001).
   The dashboard stays at `http://127.0.0.1:8000`. Startup refuses to run if
   `MUSE_HOST` is set while `MUSE_API_TOKEN` is empty or `APP_HOST` is not a
   loopback address. Leave `MUSE_HOST` empty to disable the Muse API entirely.

4. On Muse's VM, install Tailscale and join the same tailnet:
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```

5. Share your Tailscale IP + token with Muse (via chat, not in git):
   - Your Tailscale IP: `tailscale ip -4` (e.g. `100.x.y.z`)
   - Muse queries: `http://100.x.y.z:8001/api/muse/payoff-balances` with `X-Muse-Token` header

## Security notes

- Never commit `.env` with real tokens (it's gitignored).
- The Muse token is separate from Plaid credentials — rotate it with `openssl rand -hex 32` if exposed.
- Muse's listener serves only `/api/muse/*` (no dashboard, no other `/api` routes,
  no `/docs` or OpenAPI schema). Everything else answers 404.
- Tailscale ACLs: optionally restrict port 8001 on this machine to Muse's device only.
- The only write is `record-payment`, which adds a local `loan_payments` row; nothing moves money.
- `record-payment` stores a `loan_payments` row when `loan_id` is given, or as a combined payment across all loans when the account is a Plaid loan account (e.g. Aidvantage). Otherwise it returns `"stored": false` (card payments arrive via Plaid sync).
- `payoff-balances` only queries institutions with credit or loan accounts, and reports per-institution Plaid failures under `errors` instead of omitting them. Liabilities must be enabled on your Plaid account. New links request it automatically; items linked before this change need re-linking.

## For Muse's payoff workflow

Muse will:
1. `GET /api/muse/payoff-balances` → build payment plan (full statement balances for cards, monthly payment due for Aidvantage)
2. `GET /api/muse/spending-summary` → flag significant/unusual expenses vs budget/average
3. Present plan for manual approval in chat
4. After approval, guide payments via browser (or you pay manually)
5. `POST /api/muse/record-payment` for each payment + remind you to Sync in money-mover UI

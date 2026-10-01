"""Plaid client wrapper.

Wraps the plaid-python SDK so the rest of the app stays decoupled from the
SDK's request/response shapes. All calls return plain dicts/dataclasses.
"""
from __future__ import annotations

import functools
from dataclasses import dataclass
from datetime import date
from typing import Any

import plaid
from plaid.api import plaid_api
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.country_code import CountryCode
from plaid.model.investments_holdings_get_request import InvestmentsHoldingsGetRequest
from plaid.model.item_public_token_exchange_request import (
    ItemPublicTokenExchangeRequest,
)
from plaid.model.liabilities_get_request import LiabilitiesGetRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products
from plaid.model.transactions_sync_request import TransactionsSyncRequest

from .config import settings


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: str
    name: str
    mask: str | None
    kind: str  # depository / credit / investment / loan
    subtype: str | None
    current_balance: float
    available_balance: float | None
    iso_currency: str | None


@dataclass(frozen=True)
class TransactionRow:
    transaction_id: str
    account_id: str
    date: date
    name: str
    merchant: str | None
    amount: float  # positive = charge, negative = credit/income
    iso_currency: str | None
    category_primary: str | None
    category_detailed: str | None


@dataclass(frozen=True)
class TransactionSyncPage:
    """One full (paginated) transactions_sync result.

    - ``rows``: added + modified transactions to upsert.
    - ``removed_ids``: transaction IDs Plaid says no longer exist (deleted
      pending transactions, etc.) — the caller should DELETE these.
    - ``next_cursor``: cursor to persist for the next incremental sync.
    """

    rows: list[TransactionRow]
    removed_ids: list[str]
    next_cursor: str


@dataclass(frozen=True)
class SecurityInfo:
    security_id: str
    ticker: str | None
    name: str | None
    type: str | None          # stock / etf / mutual fund / cash / ...
    sector: str | None        # Technology, Healthcare, ...
    industry: str | None
    cls: str | None           # equity / debt / cash  ("class" is a Python builtin-ish)
    is_cash_equivalent: bool


@dataclass(frozen=True)
class LinkedItem:
    item_id: str
    access_token: str


@dataclass(frozen=True)
class HoldingSnapshot:
    account_id: str
    security_id: str | None
    quantity: float | None
    institution_price: float | None
    institution_value: float
    cost_basis: float | None


def get_holdings(access_token: str) -> tuple[list[HoldingSnapshot], list[SecurityInfo]]:
    """Fetch investment holdings + securities metadata for an item.

    Returns (holdings, securities). Holdings without a security_id are treated
    as cash positions in the calling code.
    """
    client = _client()
    request = InvestmentsHoldingsGetRequest(access_token=access_token)
    response = client.investments_holdings_get(request)

    securities: list[SecurityInfo] = []
    for s in response["securities"]:
        securities.append(
            SecurityInfo(
                security_id=s["security_id"],
                ticker=s.get("ticker_symbol"),
                name=s.get("name"),
                type=str(s["type"]) if s.get("type") is not None else None,
                sector=s.get("sector"),
                industry=s.get("industry"),
                cls=str(s["class"]) if s.get("class") is not None else None,
                is_cash_equivalent=bool(s.get("is_cash_equivalent", False)),
            )
        )

    holdings: list[HoldingSnapshot] = []
    for h in response["holdings"]:
        holdings.append(
            HoldingSnapshot(
                account_id=h["account_id"],
                security_id=h.get("security_id"),
                quantity=float(h["quantity"]) if h.get("quantity") is not None else None,
                institution_price=(
                    float(h["institution_price"])
                    if h.get("institution_price") is not None
                    else None
                ),
                institution_value=float(h["institution_value"]),
                cost_basis=(
                    float(h["cost_basis"]) if h.get("cost_basis") is not None else None
                ),
            )
        )

    return holdings, securities


def _env_to_plaid_environment(env: str) -> str:
    mapping = {
        "sandbox": plaid.Environment.Sandbox,
        "production": plaid.Environment.Production,
    }
    return mapping.get(env, plaid.Environment.Sandbox)


@functools.lru_cache(maxsize=1)
def _build_client() -> plaid_api.PlaidApi:
    """Build a PlaidApi client (cached across calls)."""
    configuration = plaid.Configuration(
        host=_env_to_plaid_environment(settings.plaid_env_value),
        api_key={
            "clientId": settings.plaid_client_id,
            "secret": settings.plaid_secret,
        },
    )
    api_client = plaid.ApiClient(configuration)
    return plaid_api.PlaidApi(api_client)


def _client() -> plaid_api.PlaidApi:
    if not settings.plaid_client_id or not settings.plaid_secret:
        raise RuntimeError(
            "Plaid credentials missing. Copy .env.example to .env and fill in "
            "PLAID_CLIENT_ID / PLAID_SECRET."
        )
    return _build_client()


def exchange_public_token(public_token: str) -> LinkedItem:
    """Exchange a Link public token for the item's id and reusable access token."""
    client = _client()
    request = ItemPublicTokenExchangeRequest(public_token=public_token)
    response = client.item_public_token_exchange(request)
    return LinkedItem(
        item_id=response["item_id"],
        access_token=response["access_token"],
    )


def create_link_token() -> str:
    """Create a Plaid Link token for the browser Link flow."""
    client = _client()
    request = LinkTokenCreateRequest(
        user=LinkTokenCreateRequestUser(client_user_id="money-mover-user"),
        client_name="Money Mover",
        products=[Products("transactions"), Products("investments")],
        country_codes=[CountryCode("US")],
        language="en",
    )
    response = client.link_token_create(request)
    return response["link_token"]


def get_accounts(access_token: str) -> list[AccountSnapshot]:
    client = _client()
    response = client.accounts_get(AccountsGetRequest(access_token=access_token))
    out: list[AccountSnapshot] = []
    for acct in response["accounts"]:
        balances = acct["balances"]
        out.append(
            AccountSnapshot(
                account_id=acct["account_id"],
                name=acct["name"],
                mask=acct.get("mask"),
                kind=str(acct["type"]),
                subtype=str(acct["subtype"]) if acct.get("subtype") is not None else None,
                current_balance=float(balances.get("current") or 0.0),
                available_balance=(
                    float(balances["available"]) if balances.get("available") is not None else None
                ),
                iso_currency=balances.get("iso_currency_code"),
            )
        )
    return out


def get_transactions(
    access_token: str, cursor: str | None = None
) -> TransactionSyncPage:
    """Pull transactions via the sync endpoint, paginating to completion.

    ``cursor`` enables incremental syncs: pass the ``next_cursor`` returned by
    a prior call to fetch only added/modified/removed deltas. Pass ``None`` (or
    an empty string) for the very first sync, which returns the full history.

    Returns a :class:`TransactionSyncPage` with the upsertable rows (added +
    modified), the removed transaction IDs Plaid flagged this sync, and the
    next cursor to persist.
    """
    client = _client()
    cur: str = cursor or ""
    rows: list[TransactionRow] = []
    removed: list[str] = []
    next_cursor = cur
    while True:
        request = TransactionsSyncRequest(
            access_token=access_token,
            cursor=cur,
        )
        response = client.transactions_sync(request)
        for tx in response["added"]:
            rows.append(_tx_to_row(tx))
        for tx in response["modified"]:
            rows.append(_tx_to_row(tx))
        removed.extend(tx["transaction_id"] for tx in response["removed"])
        next_cursor = response["next_cursor"]
        if response["has_more"]:
            cur = next_cursor
        else:
            break
    return TransactionSyncPage(
        rows=rows, removed_ids=removed, next_cursor=next_cursor
    )


def _tx_to_row(tx: Any) -> TransactionRow:
    category = tx.get("category") or []
    personal = tx.get("personal_finance_category")
    primary = personal.get("primary") if personal else (category[0] if category else None)
    detailed = personal.get("detailed") if personal else (
        category[-1] if category else None
    )
    return TransactionRow(
        transaction_id=tx["transaction_id"],
        account_id=tx["account_id"],
        date=tx["date"],
        name=tx.get("name") or "",
        merchant=tx.get("merchant_name"),
        amount=float(tx["amount"]),
        iso_currency=tx.get("iso_currency_code"),
        category_primary=primary,
        category_detailed=detailed,
    )


@dataclass(frozen=True)
class CreditLiability:
    account_id: str
    last_statement_balance: float | None
    last_statement_issue_date: str | None
    minimum_payment_amount: float | None
    next_payment_due_date: str | None
    last_payment_amount: float | None
    last_payment_date: str | None


@dataclass(frozen=True)
class StudentLoanLiability:
    account_id: str
    loan_name: str | None
    next_monthly_payment: float | None
    next_payment_due_date: str | None
    minimum_payment_amount: float | None
    last_payment_amount: float | None
    last_payment_date: str | None
    origination_date: str | None
    expected_payoff_date: str | None


def _opt_float(d: Any, key: str) -> float | None:
    v = d.get(key)
    return float(v) if v is not None else None


def _opt_str(d: Any, key: str) -> str | None:
    v = d.get(key)
    return str(v) if v else None


def get_liabilities(access_token: str) -> tuple[list[CreditLiability], list[StudentLoanLiability]]:
    """Fetch credit and student loan liabilities for payoff workflow.

    Returns (credit_liabilities, student_loan_liabilities).
    """
    client = _client()
    response = client.liabilities_get(LiabilitiesGetRequest(access_token=access_token))
    liabilities = response.get("liabilities", {}) or {}

    credit_out = [
        CreditLiability(
            account_id=c["account_id"],
            last_statement_balance=_opt_float(c, "last_statement_balance"),
            last_statement_issue_date=_opt_str(c, "last_statement_issue_date"),
            minimum_payment_amount=_opt_float(c, "minimum_payment_amount"),
            next_payment_due_date=_opt_str(c, "next_payment_due_date"),
            last_payment_amount=_opt_float(c, "last_payment_amount"),
            last_payment_date=_opt_str(c, "last_payment_date"),
        )
        for c in liabilities.get("credit", []) or []
    ]
    student_out = [
        StudentLoanLiability(
            account_id=s["account_id"],
            loan_name=s.get("loan_name"),
            next_monthly_payment=_opt_float(s, "next_monthly_payment"),
            next_payment_due_date=_opt_str(s, "next_payment_due_date"),
            minimum_payment_amount=_opt_float(s, "minimum_payment_amount"),
            last_payment_amount=_opt_float(s, "last_payment_amount"),
            last_payment_date=_opt_str(s, "last_payment_date"),
            origination_date=_opt_str(s, "origination_date"),
            expected_payoff_date=_opt_str(s, "expected_payoff_date"),
        )
        for s in liabilities.get("student", []) or []
    ]
    return credit_out, student_out

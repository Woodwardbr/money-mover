from __future__ import annotations

import re
from datetime import date

import plaid
from fastapi import APIRouter, HTTPException, Query
from plaid.api import plaid_api
from plaid.model.country_code import CountryCode
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products
from pydantic import BaseModel

from . import analytics, sync
from .config import settings
from .plaid import _env_to_plaid_environment

router = APIRouter()

_PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")


def _parse_period(period: str | None) -> str:
    """Validate a period query param. Defaults to the current YYYY-MM.

    Accepts ``"avg"`` or a ``YYYY-MM`` string; anything else raises 400.
    """
    if period is None or period == "":
        today = date.today()
        return f"{today.year:04d}-{today.month:02d}"
    if period == "avg":
        return period
    if _PERIOD_RE.match(period):
        return period
    raise HTTPException(
        status_code=400,
        detail="period must be 'avg' or 'YYYY-MM'",
    )


class LinkTokenResp(BaseModel):
    link_token: str


class PublicTokenReq(BaseModel):
    public_token: str
    institution: str | None = None


class BudgetReq(BaseModel):
    category: str
    monthly_limit: float


class MerchantRuleReq(BaseModel):
    merchant_pattern: str
    target_category: str


class TransactionOverrideReq(BaseModel):
    override_category: str | None = None


@router.get("/api/health")
def health() -> dict:
    return {"status": "ok", "plaid_env": settings.plaid_env_value}


@router.get("/api/link-token", response_model=LinkTokenResp)
def create_link_token() -> LinkTokenResp:
    """Create a Plaid Link token for the browser Link flow."""
    if not settings.plaid_client_id or not settings.plaid_secret:
        raise HTTPException(
            status_code=400,
            detail=(
                "Plaid credentials not configured. Copy .env.example to .env and "
                "fill in PLAID_CLIENT_ID / PLAID_SECRET."
            ),
        )

    configuration = plaid.Configuration(
        host=_env_to_plaid_environment(settings.plaid_env_value),
        api_key={
            "clientId": settings.plaid_client_id,
            "secret": settings.plaid_secret,
        },
    )
    api_client = plaid.ApiClient(configuration)
    client = plaid_api.PlaidApi(api_client)
    request = LinkTokenCreateRequest(
        user=LinkTokenCreateRequestUser(client_user_id="money-mover-user"),
        client_name="Money Mover",
        products=[Products("transactions"), Products("investments")],
        country_codes=[CountryCode("US")],
        language="en",
    )
    response = client.link_token_create(request)
    return LinkTokenResp(link_token=response["link_token"])


@router.post("/api/exchange-public-token")
def exchange_public_token(req: PublicTokenReq) -> dict:
    item_id = sync.link_item(req.public_token, institution=req.institution)
    return {"item_id": item_id, "status": "linked"}


@router.post("/api/sync")
def trigger_sync() -> dict:
    return sync.sync_all()


@router.get("/api/items")
def items() -> list[dict]:
    return sync.list_items()


@router.delete("/api/items/{item_id}")
def remove_item(item_id: str) -> dict:
    sync.unlink_item(item_id)
    return {"status": "removed"}


@router.get("/api/net-worth")
def net_worth() -> list[dict]:
    return [p.__dict__ | {"date": p.date.isoformat()} for p in analytics.net_worth_series()]


@router.get("/api/spending")
def spending(start: str | None = None, end: str | None = None) -> list[dict]:
    today = date.today()
    start_d = date.fromisoformat(start) if start else today.replace(day=1)
    end_d = date.fromisoformat(end) if end else today
    return [
        {"category": c.category, "total": c.total, "count": c.transaction_count}
        for c in analytics.spending_by_category(start_d, end_d)
    ]


@router.get("/api/spending-categories")
def spending_categories(period: str | None = None) -> list[dict]:
    """Friendly-category spending breakdown for a period token.

    ``period`` is ``"avg"`` (average monthly spend over complete months) or a
    ``YYYY-MM`` string. Defaults to the current month.
    """
    p = _parse_period(period)
    if p == "avg":
        cats = analytics.average_monthly_spending_by_friendly_category()
    else:
        rng = analytics.spending_for_period(p)
        if rng is None:
            return []
        start, end = rng
        cats = analytics.spending_by_friendly_category(start, end)
    return [
        {"category": c.category, "total": c.total, "count": c.transaction_count}
        for c in cats
    ]


@router.get("/api/spending-months")
def spending_months() -> list[dict]:
    """Distinct YYYY-MM periods present in the data (descending)."""
    return analytics.spending_months()


@router.get("/api/spending-categories/{category}/transactions")
def spending_category_transactions(
    category: str, period: str | None = None
) -> list[dict]:
    """Individual transactions for a friendly category in a period."""
    p = _parse_period(period)
    return [
        {
            "transaction_id": t.transaction_id,
            "date": t.date.isoformat(),
            "name": t.name,
            "merchant": t.merchant,
            "amount": t.amount,
            "category_detailed": t.category_detailed,
            "account_name": t.account_name,
        }
        for t in analytics.transactions_for_period_category(p, category)
    ]


@router.get("/api/budgets")
def budgets(period: str | None = None) -> list[dict]:
    p = _parse_period(period)
    return [
        {
            "category": b.category,
            "monthly_limit": b.monthly_limit,
            "spent_so_far": b.spent_so_far,
            "remaining": b.remaining,
            "pct_used": b.pct_used,
        }
        for b in analytics.budget_progress(p)
    ]


@router.post("/api/budgets")
def set_budget(req: BudgetReq) -> dict:
    analytics.upsert_budget(req.category, req.monthly_limit)
    return {"status": "saved"}


@router.delete("/api/budgets/{category}")
def remove_budget(category: str) -> dict:
    analytics.delete_budget(category)
    return {"status": "removed"}


# --- Reclassify: merchant rules + per-transaction override -----------------


@router.get("/api/category-options")
def category_options() -> list[str]:
    return analytics.friendly_category_options()


@router.get("/api/merchant-rules")
def list_merchant_rules() -> list[dict]:
    return analytics.list_merchant_rules()


@router.post("/api/merchant-rules")
def add_merchant_rule(req: MerchantRuleReq) -> dict:
    analytics.upsert_merchant_rule(req.merchant_pattern, req.target_category)
    return {"status": "saved"}


@router.delete("/api/merchant-rules/{merchant_pattern}")
def remove_merchant_rule(merchant_pattern: str) -> dict:
    analytics.delete_merchant_rule(merchant_pattern)
    return {"status": "removed"}


@router.patch("/api/transactions/{transaction_id}/category")
def reclassify_transaction(transaction_id: str, req: TransactionOverrideReq) -> dict:
    analytics.set_transaction_override(transaction_id, req.override_category)
    return {"status": "saved"}


@router.get("/api/subscriptions")
def subscriptions() -> list[dict]:
    """Detected recurring transactions (subscriptions)."""
    return [
        {
            "label": s.label,
            "amount": s.amount,
            "occurrences": s.occurrences,
            "last_date": s.last_date.isoformat(),
            "avg_interval_days": s.avg_interval_days,
            "category": s.category,
        }
        for s in analytics.detect_subscriptions()
    ]


# --- Portfolio: asset allocation by sector ---------------------------------


class HoldingSectorOverrideReq(BaseModel):
    account_id: str
    security_id: str | None = None
    sector: str | None = None


@router.get("/api/portfolio")
def portfolio() -> list[dict]:
    """Asset allocation grouped by sector (cash + investment holdings)."""
    return [
        {
            "sector": s.sector,
            "total": s.total,
            "asset_count": s.asset_count,
        }
        for s in analytics.portfolio_by_sector()
    ]


@router.get("/api/portfolio/{sector}/assets")
def portfolio_assets(sector: str) -> list[dict]:
    """Individual assets in a given portfolio sector."""
    return [
        {
            "account_id": a.account_id,
            "account_name": a.account_name,
            "institution": a.institution,
            "security_id": a.security_id,
            "ticker": a.ticker,
            "name": a.name,
            "sector": a.sector,
            "value": a.value,
            "quantity": a.quantity,
            "cost_basis": a.cost_basis,
        }
        for a in analytics.assets_in_sector(sector)
    ]


@router.get("/api/sector-options")
def sector_options() -> list[str]:
    return analytics.sector_options()


@router.patch("/api/portfolio/assets/sector")
def reclassify_holding_sector(req: HoldingSectorOverrideReq) -> dict:
    analytics.set_holding_sector_override(req.account_id, req.security_id, req.sector)
    return {"status": "saved"}


# --- 401(k) plan allocations -----------------------------------------------


class PlanAllocationReq(BaseModel):
    account_id: str
    label: str
    ticker: str | None = None
    allocation_pct: float
    sector: str


@router.get("/api/plan-allocations")
def plan_allocations() -> list[dict]:
    """All 401(k) plan allocations with account context + current balance."""
    return analytics.list_plan_allocations()


@router.get("/api/plan-allocations/{account_id}")
def plan_allocations_for_account(account_id: str) -> list[dict]:
    return analytics.plan_allocations_for_account(account_id)


@router.post("/api/plan-allocations")
def upsert_plan_allocation(req: PlanAllocationReq) -> dict:
    analytics.upsert_plan_allocation(
        req.account_id, req.label, req.ticker, req.allocation_pct, req.sector
    )
    return {"status": "saved"}


@router.delete("/api/plan-allocations/{account_id}/{label}")
def delete_plan_allocation(account_id: str, label: str) -> dict:
    analytics.delete_plan_allocation(account_id, label)
    return {"status": "removed"}


# --- Debt Tracker: manually-tracked loans -----------------------------------


class LoanReq(BaseModel):
    loan_id: str | None = None
    name: str
    loan_type: str | None = None
    interest_rate: float | None = None
    min_payment: float | None = None
    current_balance: float | None = None
    next_due_date: str | None = None
    auto_pay: bool = False
    status: str = "Scheduled"
    notes: str | None = None


class LoanPaymentReq(BaseModel):
    loan_id: str | None = None
    payment_date: str
    amount: float
    status: str = "Received"
    source: str | None = None
    notes: str | None = None
    new_balance: float | None = None


@router.get("/api/loans")
def loans() -> list[dict]:
    return [
        {
            "loan_id": ln.loan_id,
            "name": ln.name,
            "loan_type": ln.loan_type,
            "interest_rate": ln.interest_rate,
            "min_payment": ln.min_payment,
            "current_balance": ln.current_balance,
            "next_due_date": ln.next_due_date.isoformat() if ln.next_due_date else None,
            "auto_pay": ln.auto_pay,
            "status": ln.status,
            "notes": ln.notes,
        }
        for ln in analytics.list_loans()
    ]


@router.post("/api/loans")
def save_loan(req: LoanReq) -> dict:
    due = date.fromisoformat(req.next_due_date) if req.next_due_date else None
    loan_id = analytics.upsert_loan(
        loan_id=req.loan_id,
        name=req.name,
        loan_type=req.loan_type,
        interest_rate=req.interest_rate,
        min_payment=req.min_payment,
        current_balance=req.current_balance,
        next_due_date=due,
        auto_pay=req.auto_pay,
        status=req.status,
        notes=req.notes,
    )
    return {"status": "saved", "loan_id": loan_id}


@router.delete("/api/loans/{loan_id}")
def remove_loan(loan_id: str) -> dict:
    analytics.delete_loan(loan_id)
    return {"status": "removed"}


@router.get("/api/loan-payments")
def loan_payments() -> list[dict]:
    return [
        {
            "payment_id": p.payment_id,
            "loan_id": p.loan_id,
            "payment_date": p.payment_date.isoformat(),
            "amount": p.amount,
            "status": p.status,
            "source": p.source,
            "notes": p.notes,
        }
        for p in analytics.list_loan_payments()
    ]


@router.post("/api/loan-payments")
def save_loan_payment(req: LoanPaymentReq) -> dict:
    pdate = date.fromisoformat(req.payment_date)
    payment_id = analytics.record_loan_payment(
        payment_date=pdate,
        amount=req.amount,
        loan_id=req.loan_id,
        status=req.status,
        source=req.source,
        notes=req.notes,
        new_balance=req.new_balance,
    )
    return {"status": "saved", "payment_id": payment_id}


@router.delete("/api/loan-payments/{payment_id}")
def remove_loan_payment(payment_id: str) -> dict:
    analytics.delete_loan_payment(payment_id)
    return {"status": "removed"}


@router.get("/api/debt/summary")
def debt_summary() -> dict:
    return analytics.debt_summary()


@router.get("/api/debt/balance-history")
def debt_balance_history() -> list[dict]:
    return analytics.loan_balance_history()


@router.get("/api/debt/projection")
def debt_projection(
    extra_monthly: float = 0.0,
    extra_onetime: float = 0.0,
    strategy: str = Query(
        "distributed",
        description="Payoff strategy: distributed, highest_interest, or lowest_balance",
    ),
) -> dict:
    if strategy not in analytics.PAYOFF_STRATEGIES:
        raise HTTPException(
            status_code=400,
            detail=f"strategy must be one of {analytics.PAYOFF_STRATEGIES}",
        )
    return analytics.project_payoff(
        extra_monthly=extra_monthly,
        extra_onetime=extra_onetime,
        strategy=strategy,
    )

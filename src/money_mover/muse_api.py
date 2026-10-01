"""Muse API for monthly payoff workflow.

Exposes payoff-relevant balances over Tailscale with token auth.
All routes live under /api/muse and require an ``X-Muse-Token`` header.

``muse_app`` is a standalone app served on its own listener (MUSE_HOST) and
contains only these routes, so Muse cannot reach the dashboard or the rest of
``/api`` regardless of the token.
"""

from __future__ import annotations

import logging
import re
import secrets
from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import analytics, plaid
from .config import settings
from .db import get_conn

log = logging.getLogger(__name__)

_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def require_token(x_muse_token: str | None = Header(default=None)) -> None:
    expected = settings.muse_api_token
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="MUSE_API_TOKEN not configured. Set it in .env.",
        )
    if not x_muse_token or not secrets.compare_digest(x_muse_token.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Invalid Muse token")


router = APIRouter(prefix="/api/muse", dependencies=[Depends(require_token)])


class RecordPaymentReq(BaseModel):
    account_id: str = Field(min_length=1)
    amount: float = Field(gt=0)
    payment_date: date | None = None
    loan_id: str | None = None
    notes: str | None = None


class LoanUpdate(BaseModel):
    """Partial update to a tracked loan. Only fields that are sent change."""

    model_config = ConfigDict(extra="forbid")

    loan_id: str = Field(min_length=1)
    current_balance: float | None = Field(default=None, ge=0)
    min_payment: float | None = Field(default=None, ge=0)
    next_due_date: date | None = None
    interest_rate: float | None = Field(default=None, ge=0, le=100)
    status: Literal["Scheduled", "Paid", "Past Due"] | None = None

    @model_validator(mode="after")
    def _sent_fields_not_null(self) -> LoanUpdate:
        fields = self.model_fields_set - {"loan_id"}
        if not fields:
            raise ValueError("at least one field to update is required")
        nulls = sorted(f for f in fields if getattr(self, f) is None)
        if nulls:
            raise ValueError(f"fields cannot be null: {nulls}")
        return self

    def changes(self) -> dict:
        return {f: getattr(self, f) for f in self.model_fields_set - {"loan_id"}}


class UpdateLoansReq(BaseModel):
    model_config = ConfigDict(extra="forbid")

    updates: list[LoanUpdate] = Field(min_length=1, max_length=50)


@router.get("/health")
def muse_health() -> dict:
    return {"status": "ok", "plaid_env": settings.plaid_env_value}


# Plaid errors meaning "this item has no liabilities data" rather than "something
# broke". Cards on these items fall back to their synced current balance.
_NO_LIABILITIES_CODES = {"ADDITIONAL_CONSENT_REQUIRED", "PRODUCTS_NOT_SUPPORTED"}


@router.get("/payoff-balances")
def payoff_balances() -> dict:
    """Return what to pay for the payoff workflow.

    Credit cards: one entry per card. ``payoff_amount`` is the last statement
    balance when Plaid Liabilities is available for the card
    (``balance_source: "statement"``), otherwise the current balance from the
    last sync (``balance_source: "current"``). Due date and minimum payment
    are only known with Liabilities.

    Student loans: Plaid Liabilities data, when any linked item has it.
    Tracked loans: the Debt Tracker's loans, with monthly payment, due date
    and ``auto_pay``. Their ``loan_id`` can be passed to ``record-payment``.

    Unexpected Plaid failures are listed under ``errors``; the affected cards
    still appear with their current balance, so a card is never silently
    dropped or mistaken for a zero balance.
    """
    with get_conn() as conn:
        cards = conn.execute(
            analytics.LATEST_BALANCES_CTE
            + """
            SELECT a.account_id, a.name, a.mask, a.item_id, i.institution,
                   lb.current AS current_balance, lb.snapshot_date AS balance_as_of
            FROM accounts a
            JOIN items i ON i.item_id = a.item_id
            LEFT JOIN latest_balances lb ON lb.account_id = a.account_id
            WHERE a.kind = 'credit'
            ORDER BY i.institution, a.name
            """
        ).fetchall()
        # Only items holding credit/loan accounts can have liabilities; querying
        # brokerage-only items would fail every time and bury real errors.
        items = conn.execute(
            """
            SELECT DISTINCT i.item_id, i.access_token, i.institution
            FROM items i JOIN accounts a ON a.item_id = i.item_id
            WHERE a.kind IN ('credit', 'loan')
            """
        ).fetchall()
        account_names = {
            r["account_id"]: r for r in conn.execute("SELECT account_id, name, mask FROM accounts")
        }

    credit_liabs: dict[str, plaid.CreditLiability] = {}
    student_loans: list[dict] = []
    errors: list[dict] = []

    for item in items:
        try:
            credit, student = plaid.get_liabilities(item["access_token"])
        except Exception as exc:
            code = plaid.error_code(exc)
            if code not in _NO_LIABILITIES_CODES:
                log.warning("Liabilities fetch failed for %s: %s", item["institution"], exc)
                errors.append(
                    {"institution": item["institution"], "error_code": code or type(exc).__name__}
                )
            continue

        credit_liabs.update((c.account_id, c) for c in credit)
        for s in student:
            acct = account_names.get(s.account_id)
            student_loans.append(
                {
                    "account_id": s.account_id,
                    "name": acct["name"] if acct else None,
                    "mask": acct["mask"] if acct else None,
                    "institution": item["institution"],
                    "loan_name": s.loan_name,
                    "monthly_payment_due": s.next_monthly_payment,
                    "due_date": s.next_payment_due_date,
                    "minimum_payment": s.minimum_payment_amount,
                    "last_payment_amount": s.last_payment_amount,
                    "last_payment_date": s.last_payment_date,
                }
            )

    credit_cards = []
    for card in cards:
        liab = credit_liabs.get(card["account_id"])
        statement = liab.last_statement_balance if liab else None
        if statement is not None:
            payoff, source = statement, "statement"
        elif card["current_balance"] is not None:
            payoff, source = card["current_balance"], "current"
        else:
            payoff, source = None, None
        credit_cards.append(
            {
                "account_id": card["account_id"],
                "name": card["name"],
                "mask": card["mask"],
                "institution": card["institution"],
                "payoff_amount": payoff,
                "balance_source": source,
                "statement_balance": statement,
                "current_balance": card["current_balance"],
                "balance_as_of": card["balance_as_of"],
                "statement_issue_date": liab.last_statement_issue_date if liab else None,
                "minimum_payment": liab.minimum_payment_amount if liab else None,
                "due_date": liab.next_payment_due_date if liab else None,
                "last_payment_amount": liab.last_payment_amount if liab else None,
                "last_payment_date": liab.last_payment_date if liab else None,
            }
        )

    tracked_loans = [_loan_dict(ln) for ln in analytics.list_loans()]

    return {
        "credit_cards": credit_cards,
        "student_loans": student_loans,
        "tracked_loans": tracked_loans,
        "errors": errors,
    }


def _loan_dict(ln) -> dict:
    return {
        "loan_id": ln.loan_id,
        "name": ln.name,
        "monthly_payment": ln.min_payment,
        "next_due_date": ln.next_due_date.isoformat() if ln.next_due_date else None,
        "current_balance": ln.current_balance,
        "interest_rate": ln.interest_rate,
        "auto_pay": ln.auto_pay,
        "status": ln.status,
    }


@router.get("/spending-summary")
def spending_summary(period: str | None = None) -> dict:
    """A month's spending next to the usual, for the unusual-spending check.

    ``period`` is ``YYYY-MM`` and defaults to the last complete month.

    - ``categories``: every category's spend that month vs its average over the
      other complete months (``difference``, ``pct_of_avg``), biggest overspend first.
    - ``largest_transactions``: the month's 10 largest purchases.
    - ``budgets``: budget vs actual, for categories that have a budget.
    - ``average_monthly``: averages over all complete months (kept for compatibility).
    """
    if period is None:
        prev = date.today().replace(day=1) - timedelta(days=1)
        period = f"{prev.year:04d}-{prev.month:02d}"
    elif not _PERIOD_RE.match(period):
        raise HTTPException(status_code=400, detail="period must be 'YYYY-MM'")

    budgets = [
        {
            "category": b.category,
            "limit": b.monthly_limit,
            "spent": b.spent_so_far,
            "remaining": b.remaining,
        }
        for b in analytics.budget_progress(period)
    ]
    average_monthly = [
        {"category": c.category, "avg_monthly": c.total, "count": c.transaction_count}
        for c in analytics.average_monthly_spending_by_friendly_category()
    ]
    comparison = analytics.month_vs_average(period)
    return {
        "period": period,
        "month_complete": comparison.month_complete,
        "baseline_months": comparison.baseline_months,
        "categories": [
            {
                "category": c.category,
                "spent": round(c.spent, 2),
                "count": c.transaction_count,
                "avg_monthly": round(c.avg_monthly, 2),
                "difference": round(c.difference, 2),
                "pct_of_avg": round(c.pct_of_avg, 1) if c.pct_of_avg is not None else None,
            }
            for c in comparison.categories
        ],
        "largest_transactions": comparison.largest_transactions,
        "budgets": budgets,
        "average_monthly": average_monthly,
    }


_AUTOPAY_DETAIL = (
    "Loan is on autopay; its payments are logged by the user. Not recording, to avoid duplicates."
)


@router.post("/record-payment")
def record_payment(payload: RecordPaymentReq) -> dict:
    """Record a manual payoff payment so it shows in money-mover.

    With ``loan_id``, the payment is logged against that tracked loan. Without
    it, a payment to a Plaid loan account (e.g. Aidvantage) is logged as a
    combined payment across all loans (``loan_id`` NULL). Payments to other
    accounts are not stored (card payments arrive via Plaid sync) and the
    response says so with ``"stored": false``. The loan's balance is not
    updated; that happens when the user edits it in the Debt Tracker.

    Loans on autopay are refused with 409: the user logs those payments
    themselves, so a Muse entry would double-count. A combined payment is
    refused when every tracked loan is on autopay.
    """
    with get_conn() as conn:
        acct = conn.execute(
            "SELECT name, kind FROM accounts WHERE account_id = ?", (payload.account_id,)
        ).fetchone()
        if acct is None:
            raise HTTPException(status_code=404, detail="Unknown account_id")
        loan_name: str | None = None
        if payload.loan_id is not None:
            loan = conn.execute(
                "SELECT name, auto_pay FROM loans WHERE loan_id = ?", (payload.loan_id,)
            ).fetchone()
            if loan is None:
                raise HTTPException(status_code=404, detail="Unknown loan_id")
            if loan["auto_pay"]:
                raise HTTPException(status_code=409, detail=_AUTOPAY_DETAIL)
            loan_name = loan["name"]
        elif acct["kind"] == "loan":
            manual = conn.execute(
                "SELECT COUNT(*) AS n FROM loans WHERE auto_pay = 0"
            ).fetchone()["n"]
            tracked = conn.execute("SELECT COUNT(*) AS n FROM loans").fetchone()["n"]
            if tracked and not manual:
                raise HTTPException(status_code=409, detail=_AUTOPAY_DETAIL)

    if payload.loan_id is None and acct["kind"] != "loan":
        return {"status": "noted", "stored": False, "account": acct["name"]}

    payment_id = analytics.record_loan_payment(
        payment_date=payload.payment_date or date.today(),
        amount=payload.amount,
        loan_id=payload.loan_id,
        status="Received",
        source=f"Muse payoff ({acct['name']})",
        notes=payload.notes or "Muse monthly payoff",
    )
    return {"status": "recorded", "stored": True, "payment_id": payment_id, "loan": loan_name}


# No interactive docs/OpenAPI schema: Muse gets exactly the routes above.
muse_app = FastAPI(title="Money Mover — Muse", docs_url=None, redoc_url=None, openapi_url=None)
muse_app.include_router(router)


@router.post("/update-loans")
def update_loans(payload: UpdateLoansReq) -> dict:
    """Update balance, payment, due date, rate or status on existing tracked loans.

    Only fields that are sent change. All updates apply or none do: an unknown
    ``loan_id`` returns 404 and nothing is written. Loans cannot be created,
    deleted or renamed, and ``auto_pay`` cannot be changed, through this API.
    Returns the updated loans in request order.
    """
    ids = [u.loan_id for u in payload.updates]
    if len(set(ids)) != len(ids):
        raise HTTPException(status_code=400, detail="Each loan_id may appear only once")
    try:
        loans = analytics.update_loans([(u.loan_id, u.changes()) for u in payload.updates])
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=f"Unknown loan_id: {exc.args[0]}") from exc
    log.info(
        "Muse updated loans: %s",
        ", ".join(f"{u.loan_id}={sorted(u.changes())}" for u in payload.updates),
    )
    return {"loans": [_loan_dict(ln) for ln in loans]}

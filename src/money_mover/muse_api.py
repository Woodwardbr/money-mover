"""Muse API for monthly payoff workflow.

Exposes payoff-relevant balances over Tailscale with token auth.
All routes live under /api/muse and require an ``X-Muse-Token`` header.
"""

from __future__ import annotations

import logging
import re
import secrets
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

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


@router.get("/health")
def muse_health() -> dict:
    return {"status": "ok", "plaid_env": settings.plaid_env_value}


@router.get("/payoff-balances")
def payoff_balances() -> dict:
    """Return statement balances for the payoff workflow.

    Credit cards: last statement balance, minimum payment, due date.
    Student loans: next monthly payment + due date.

    Items whose liabilities lookup fails are listed under ``errors`` rather than
    dropped, so a missing card is never mistaken for a zero balance.
    """
    with get_conn() as conn:
        # Only items holding credit/loan accounts can have liabilities; querying
        # brokerage-only items would fail every time and bury real errors.
        items = conn.execute(
            """
            SELECT DISTINCT i.item_id, i.access_token, i.institution
            FROM items i JOIN accounts a ON a.item_id = i.item_id
            WHERE a.kind IN ('credit', 'loan')
            """
        ).fetchall()
        accounts = {
            r["account_id"]: dict(r) for r in conn.execute("SELECT * FROM accounts").fetchall()
        }

    credit_cards: list[dict] = []
    student_loans: list[dict] = []
    errors: list[dict] = []

    for item in items:
        try:
            credit_liabs, student_liabs = plaid.get_liabilities(item["access_token"])
        except Exception as exc:
            log.warning("Liabilities fetch failed for %s: %s", item["institution"], exc)
            errors.append({"institution": item["institution"], "error": str(exc)})
            continue

        for c in credit_liabs:
            acct = accounts.get(c.account_id, {})
            credit_cards.append(
                {
                    "account_id": c.account_id,
                    "name": acct.get("name"),
                    "mask": acct.get("mask"),
                    "institution": item["institution"],
                    "statement_balance": c.last_statement_balance,
                    "statement_issue_date": c.last_statement_issue_date,
                    "minimum_payment": c.minimum_payment_amount,
                    "due_date": c.next_payment_due_date,
                    "last_payment_amount": c.last_payment_amount,
                    "last_payment_date": c.last_payment_date,
                }
            )

        for s in student_liabs:
            acct = accounts.get(s.account_id, {})
            student_loans.append(
                {
                    "account_id": s.account_id,
                    "name": acct.get("name"),
                    "mask": acct.get("mask"),
                    "institution": item["institution"],
                    "loan_name": s.loan_name,
                    "monthly_payment_due": s.next_monthly_payment,
                    "due_date": s.next_payment_due_date,
                    "minimum_payment": s.minimum_payment_amount,
                    "last_payment_amount": s.last_payment_amount,
                    "last_payment_date": s.last_payment_date,
                }
            )

    return {"credit_cards": credit_cards, "student_loans": student_loans, "errors": errors}


@router.get("/spending-summary")
def spending_summary(period: str | None = None) -> dict:
    """Budgets vs actuals for a month, plus average monthly spending by category.

    ``period`` is ``YYYY-MM`` and defaults to the last complete month.
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
    return {"period": period, "budgets": budgets, "average_monthly": average_monthly}


@router.post("/record-payment")
def record_payment(payload: RecordPaymentReq) -> dict:
    """Record a manual payoff payment so it shows in money-mover.

    With ``loan_id``, the payment is logged against that tracked loan. Without
    it, a payment to a Plaid loan account (e.g. Aidvantage) is logged as a
    combined payment across all loans (``loan_id`` NULL). Payments to other
    accounts are not stored (card payments arrive via Plaid sync) and the
    response says so with ``"stored": false``. The loan's balance is not
    updated; that happens when the user edits it in the Debt Tracker.
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
                "SELECT name FROM loans WHERE loan_id = ?", (payload.loan_id,)
            ).fetchone()
            if loan is None:
                raise HTTPException(status_code=404, detail="Unknown loan_id")
            loan_name = loan["name"]

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

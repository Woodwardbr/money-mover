from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import analytics
from .api import router as api_router
from .config import settings
from .db import get_conn

BASE_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=BASE_DIR / "templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Idempotently seed curated 401(k) default allocations at startup."""
    analytics.seed_default_plan_allocations()
    yield


app = FastAPI(title="Money Mover", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.include_router(api_router)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    with get_conn() as conn:
        acct_rows = conn.execute(
            analytics.LATEST_BALANCES_CTE
            + """
            SELECT a.name, a.mask, a.kind, a.subtype,
                   a.exclude_from_net_worth,
                   b.current,
                   b.snapshot_date
            FROM accounts a
            LEFT JOIN latest_balances b ON a.account_id = b.account_id
            ORDER BY a.kind, a.name
            """
        ).fetchall()
    return TEMPLATES.TemplateResponse(
        request,
        "dashboard.html",
        {
            "accounts": [dict(r) for r in acct_rows],
            "plaid_env": settings.plaid_env_value,
        },
    )


@app.get("/budgets", response_class=HTMLResponse)
def budgets_page(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request, "budgets.html", {"budgets": analytics.list_budgets()}
    )


@app.get("/portfolio", response_class=HTMLResponse)
def portfolio_page(request: Request) -> HTMLResponse:
    with get_conn() as conn:
        items = [
            dict(r)
            for r in conn.execute(
                "SELECT item_id, institution FROM items ORDER BY institution"
            ).fetchall()
        ]
        invest_accts = [
            dict(r)
            for r in conn.execute(
                """
                SELECT a.account_id, a.name, a.subtype, i.institution
                FROM accounts a
                JOIN items i USING (item_id)
                WHERE a.kind = 'investment'
                ORDER BY i.institution, a.name
                """
            ).fetchall()
        ]
    return TEMPLATES.TemplateResponse(
        request,
        "portfolio.html",
        {"items": items, "investment_accounts": invest_accts},
    )


@app.get("/debt", response_class=HTMLResponse)
def debt_page(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "debt.html",
        {"plaid_env": settings.plaid_env_value},
    )


def run() -> None:
    uvicorn.run(
        "money_mover.app:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
    )

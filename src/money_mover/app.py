from __future__ import annotations

import asyncio
import ipaddress
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


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def run() -> None:
    """Serve the dashboard, plus the Muse API on its own listener if MUSE_HOST is set.

    The two are separate apps so that the address Muse can reach only serves
    ``/api/muse/*``; the dashboard and the rest of ``/api`` stay on APP_HOST.
    """
    configs = [
        uvicorn.Config("money_mover.app:app", host=settings.app_host, port=settings.app_port)
    ]
    if settings.muse_host:
        if not settings.muse_api_token:
            raise SystemExit("MUSE_HOST is set but MUSE_API_TOKEN is empty; refusing to start.")
        if not _is_loopback(settings.app_host):
            raise SystemExit(
                "MUSE_HOST is set, so APP_HOST must be a loopback address (e.g. 127.0.0.1); "
                "otherwise Muse could reach the dashboard and the rest of /api."
            )
        configs.append(
            uvicorn.Config(
                "money_mover.muse_api:muse_app", host=settings.muse_host, port=settings.muse_port
            )
        )

    async def serve_all() -> None:
        await asyncio.gather(*(uvicorn.Server(c).serve() for c in configs))

    asyncio.run(serve_all())

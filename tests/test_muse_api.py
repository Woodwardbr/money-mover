from __future__ import annotations

import dataclasses
import json

import pytest
from fastapi.testclient import TestClient

from money_mover import muse_api, plaid
from money_mover.app import app
from money_mover.db import get_conn

TOKEN = "test-token"
AUTH = {"X-Muse-Token": TOKEN}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(
        muse_api, "settings", dataclasses.replace(muse_api.settings, muse_api_token=TOKEN)
    )
    return TestClient(muse_api.muse_app)


def make_loan(loan_id: str, name: str) -> None:
    with get_conn() as conn:
        conn.execute("INSERT INTO loans (loan_id, name) VALUES (?, ?)", (loan_id, name))


def loan_payments() -> list[tuple]:
    with get_conn() as conn:
        rows = conn.execute("SELECT loan_id, amount FROM loan_payments").fetchall()
    return [tuple(r) for r in rows]


def test_rejects_missing_and_wrong_token(client):
    assert client.get("/api/muse/health").status_code == 401
    assert client.get("/api/muse/health", headers={"X-Muse-Token": "nope"}).status_code == 401
    assert client.get("/api/muse/health", headers=AUTH).status_code == 200


def test_unconfigured_token_returns_503(monkeypatch):
    monkeypatch.setattr(
        muse_api, "settings", dataclasses.replace(muse_api.settings, muse_api_token="")
    )
    assert TestClient(muse_api.muse_app).get("/api/muse/health", headers=AUTH).status_code == 503


def test_muse_app_serves_only_muse_routes(client):
    for path in ("/", "/api/budgets", "/docs", "/openapi.json"):
        assert client.get(path, headers=AUTH).status_code == 404, path
    assert client.delete("/api/items/item1", headers=AUTH).status_code == 404


def test_dashboard_app_does_not_serve_muse_routes():
    assert TestClient(app).get("/api/muse/health", headers=AUTH).status_code == 404


def test_spending_summary_rejects_bad_period(client):
    resp = client.get("/api/muse/spending-summary", params={"period": "2026-13"}, headers=AUTH)
    assert resp.status_code == 400


def test_payoff_balances_skips_items_without_credit_or_loan(client, make_account, monkeypatch):
    make_account("card", kind="credit", item_id="bank", institution="Bank")
    make_account("brk", kind="investment", item_id="broker", institution="Broker")
    queried: list[str] = []

    def fake_liabilities(token: str):
        queried.append(token)
        return [
            plaid.CreditLiability(
                account_id="card",
                last_statement_balance=500.0,
                last_statement_issue_date="2026-09-01",
                minimum_payment_amount=25.0,
                next_payment_due_date="2026-09-25",
                last_payment_amount=None,
                last_payment_date=None,
            )
        ], []

    monkeypatch.setattr(plaid, "get_liabilities", fake_liabilities)
    body = client.get("/api/muse/payoff-balances", headers=AUTH).json()

    assert len(queried) == 1
    assert body["errors"] == []
    assert [c["account_id"] for c in body["credit_cards"]] == ["card"]
    assert body["credit_cards"][0]["statement_balance"] == 500.0
    assert body["credit_cards"][0]["payoff_amount"] == 500.0
    assert body["credit_cards"][0]["balance_source"] == "statement"


def plaid_error(code: str) -> Exception:
    exc = plaid.plaid.ApiException(status=400, reason="Bad Request")
    exc.body = json.dumps({"error_code": code, "error_message": "nope"})
    return exc


def test_payoff_balances_falls_back_to_current_without_consent(
    client, make_account, make_balance, monkeypatch
):
    make_account("card", kind="credit", item_id="bank", institution="Bank")
    make_balance("card", "2026-09-01", 900.0)
    make_balance("card", "2026-09-29", 1234.5)

    def no_consent(token: str):
        raise plaid_error("ADDITIONAL_CONSENT_REQUIRED")

    monkeypatch.setattr(plaid, "get_liabilities", no_consent)
    body = client.get("/api/muse/payoff-balances", headers=AUTH).json()

    assert body["errors"] == []
    card = body["credit_cards"][0]
    assert card["payoff_amount"] == 1234.5
    assert card["balance_source"] == "current"
    assert card["balance_as_of"] == "2026-09-29"
    assert card["statement_balance"] is None
    assert card["due_date"] is None


def test_payoff_balances_reports_unexpected_errors_but_keeps_card(
    client, make_account, make_balance, monkeypatch
):
    make_account("card", kind="credit", item_id="bank", institution="Bank")
    make_balance("card", "2026-09-29", 80.0)

    def broken(token: str):
        raise plaid_error("ITEM_LOGIN_REQUIRED")

    monkeypatch.setattr(plaid, "get_liabilities", broken)
    body = client.get("/api/muse/payoff-balances", headers=AUTH).json()

    assert body["errors"] == [{"institution": "Bank", "error_code": "ITEM_LOGIN_REQUIRED"}]
    assert body["credit_cards"][0]["payoff_amount"] == 80.0


def test_payoff_balances_includes_tracked_loans(client):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO loans (loan_id, name, min_payment, next_due_date, auto_pay) "
            "VALUES ('l1', 'Direct Loan', 150.0, '2026-10-15', 1)"
        )
    body = client.get("/api/muse/payoff-balances", headers=AUTH).json()

    assert body["tracked_loans"] == [
        {
            "loan_id": "l1",
            "name": "Direct Loan",
            "monthly_payment": 150.0,
            "next_due_date": "2026-10-15",
            "current_balance": None,
            "interest_rate": None,
            "auto_pay": True,
            "status": "Scheduled",
        }
    ]


def test_record_payment_unknown_account_404(client):
    resp = client.post(
        "/api/muse/record-payment", json={"account_id": "missing", "amount": 10}, headers=AUTH
    )
    assert resp.status_code == 404


def test_record_payment_rejects_non_positive_amount(client, make_account):
    make_account("loan", kind="loan")
    resp = client.post(
        "/api/muse/record-payment", json={"account_id": "loan", "amount": -5}, headers=AUTH
    )
    assert resp.status_code == 422


def test_record_payment_to_loan_account_is_combined(client, make_account):
    make_account("aid", kind="loan", name="Aidvantage")
    make_loan("l1", "1-01 Direct Loan - Subsidized")
    resp = client.post(
        "/api/muse/record-payment", json={"account_id": "aid", "amount": 300}, headers=AUTH
    )
    assert resp.json()["stored"] is True
    assert loan_payments() == [(None, 300.0)]


def test_record_payment_with_loan_id(client, make_account):
    make_account("aid", kind="loan")
    make_loan("l1", "1-01 Direct Loan - Subsidized")
    resp = client.post(
        "/api/muse/record-payment",
        json={"account_id": "aid", "amount": 120, "loan_id": "l1"},
        headers=AUTH,
    )
    assert resp.json()["loan"] == "1-01 Direct Loan - Subsidized"
    assert loan_payments() == [("l1", 120.0)]


def test_record_payment_unknown_loan_id_404(client, make_account):
    make_account("aid", kind="loan")
    resp = client.post(
        "/api/muse/record-payment",
        json={"account_id": "aid", "amount": 120, "loan_id": "nope"},
        headers=AUTH,
    )
    assert resp.status_code == 404
    assert loan_payments() == []


def test_record_payment_to_card_not_stored(client, make_account):
    make_account("card", kind="credit")
    resp = client.post(
        "/api/muse/record-payment", json={"account_id": "card", "amount": 50}, headers=AUTH
    )
    assert resp.json()["stored"] is False
    assert loan_payments() == []


def test_record_payment_refuses_autopay_loan(client, make_account):
    make_account("aid", kind="loan")
    with get_conn() as conn:
        conn.execute("INSERT INTO loans (loan_id, name, auto_pay) VALUES ('l1', 'Direct', 1)")
    resp = client.post(
        "/api/muse/record-payment",
        json={"account_id": "aid", "amount": 120, "loan_id": "l1"},
        headers=AUTH,
    )
    assert resp.status_code == 409
    assert loan_payments() == []


def test_record_payment_refuses_combined_when_all_loans_autopay(client, make_account):
    make_account("aid", kind="loan")
    with get_conn() as conn:
        conn.execute("INSERT INTO loans (loan_id, name, auto_pay) VALUES ('l1', 'A', 1)")
        conn.execute("INSERT INTO loans (loan_id, name, auto_pay) VALUES ('l2', 'B', 1)")
    resp = client.post(
        "/api/muse/record-payment", json={"account_id": "aid", "amount": 300}, headers=AUTH
    )
    assert resp.status_code == 409
    assert loan_payments() == []

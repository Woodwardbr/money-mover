from __future__ import annotations

import dataclasses

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
    return TestClient(app)


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
    assert TestClient(app).get("/api/muse/health", headers=AUTH).status_code == 503


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

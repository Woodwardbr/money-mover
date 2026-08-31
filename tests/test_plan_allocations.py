from __future__ import annotations

from money_mover import analytics, db


def test_seed_returns_zero_without_json_file():
    """No data/plan-allocations.json under the (patched) db path -> no-op, no raise."""
    assert analytics._load_default_plan_allocations() == {}
    assert analytics.seed_default_plan_allocations() == 0


def test_loader_follows_patched_db_settings():
    """The loader must resolve its path via db.settings, not a module-level
    config import, so it stays inside the test sandbox even if the real repo
    has a data/plan-allocations.json on disk."""
    # db.settings.db_path is already tmp_path/"test.db" via the autouse fixture;
    # write a JSON file next to it and confirm the loader picks up *that* one.
    target = db.settings.db_path.parent / "plan-allocations.json"
    target.write_text(
        '{"TEST PLAN": [{"label": "Fund A", "ticker": "FNDA", '
        '"allocation_pct": 100.0, "sector": "Bonds"}]}'
    )
    result = analytics._load_default_plan_allocations()
    assert result == {"TEST PLAN": [("Fund A", "FNDA", 100.0, "Bonds")]}


def test_seed_inserts_for_matching_account(make_account):
    target = db.settings.db_path.parent / "plan-allocations.json"
    target.write_text(
        '{"TEST 401(K)": [{"label": "Fund A", "ticker": "FNDA", '
        '"allocation_pct": 60.0, "sector": "Bonds"}, '
        '{"label": "Fund B", "ticker": null, '
        '"allocation_pct": 40.0, "sector": "Cash"}]}'
    )
    make_account("acct1", kind="investment", name="My Test 401(k) Plan")

    inserted = analytics.seed_default_plan_allocations()
    assert inserted == 2

    rows = analytics.plan_allocations_for_account("acct1")
    assert {r["label"] for r in rows} == {"Fund A", "Fund B"}

    # Re-seeding is idempotent: an account that already has allocation rows
    # is skipped entirely.
    assert analytics.seed_default_plan_allocations() == 0

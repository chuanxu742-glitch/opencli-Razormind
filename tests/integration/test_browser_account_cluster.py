from __future__ import annotations

import os

import pytest

from scripts.verify_browser_account_capacity import _run, build_config

pytestmark = pytest.mark.postgres_conformance


@pytest.mark.asyncio
async def test_dedicated_postgres_inventory_is_bounded():
    """Run the real bounded inventory query against an explicitly supplied PG database."""
    database_url = os.environ.get("TEST_DATABASE_URL_PG")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL_PG is required for the PostgreSQL acceptance smoke")
    accounts = int(os.environ.get("QRAC2_CAPACITY_SMOKE_ACCOUNTS", "1000"))
    config = build_config(
        database_url,
        accounts=accounts,
        page_size=100,
        claim_size=100,
        samples=3,
    )
    result = await _run(config)
    assert result["bounded"] is True
    assert result["page_plan_violations"] == []
    assert result["claim_plan_violations"] == []
    assert result["page_rows_last_sample"] <= 100

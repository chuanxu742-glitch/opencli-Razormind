from __future__ import annotations

import pytest

from scripts.verify_browser_account_capacity import (
    CapacityConfigurationError,
    build_config,
    plan_violations,
    validate_database_url,
)


def test_capacity_experiment_requires_explicit_postgres_url():
    with pytest.raises(CapacityConfigurationError, match="TEST_DATABASE_URL_PG"):
        validate_database_url(None)
    with pytest.raises(CapacityConfigurationError, match="PostgreSQL"):
        validate_database_url("sqlite+aiosqlite:///:memory:")


def test_capacity_experiment_rejects_unbounded_query_parameters():
    with pytest.raises(CapacityConfigurationError, match="accounts"):
        build_config(
            "postgresql://user:pass@localhost/db",
            accounts=10_000_001,
            page_size=10,
            claim_size=10,
            samples=1,
        )
    with pytest.raises(CapacityConfigurationError, match="page_size"):
        build_config(
            "postgresql://user:pass@localhost/db",
            accounts=10,
            page_size=101,
            claim_size=10,
            samples=1,
        )
    with pytest.raises(CapacityConfigurationError, match="claim_size"):
        build_config(
            "postgresql://user:pass@localhost/db",
            accounts=10,
            page_size=10,
            claim_size=101,
            samples=1,
        )


def test_capacity_plan_rejects_sequential_scan_and_materialization():
    plan = [
        {
            "Plan": {
                "Node Type": "Index Scan",
                "Plans": [{"Node Type": "Materialize", "Plans": [{"Node Type": "Seq Scan"}]}],
            }
        }
    ]
    assert plan_violations(plan) == ["Materialize", "Seq Scan"]


def test_capacity_plan_accepts_bounded_index_plan():
    plan = [{"Plan": {"Node Type": "Limit", "Plans": [{"Node Type": "Index Scan"}]}}]
    assert plan_violations(plan) == []


def test_full_run_is_explicitly_the_one_to_ten_million_comparison():
    config = build_config(
        "postgresql://user:pass@localhost/opencli_test_db",
        accounts=10_000_000,
        page_size=100,
        claim_size=100,
        samples=1,
        full_run=True,
    )
    assert config.full_run is True
    with pytest.raises(CapacityConfigurationError, match="full_run"):
        build_config(
            "postgresql://user:pass@localhost/opencli_test_db",
            accounts=1_000_000,
            page_size=100,
            claim_size=100,
            samples=1,
            full_run=True,
        )

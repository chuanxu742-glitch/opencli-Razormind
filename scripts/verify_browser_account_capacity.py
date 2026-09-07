"""Measure the real browser-account inventory schema on disposable PostgreSQL.

The default run creates a unique sibling database through the repository's
``tests.postgres_conformance`` helper, applies the real Alembic schema with the
current Python interpreter, and compares one million with ten million dormant
``browser_accounts`` rows.  ``--accounts`` is an explicitly labelled smoke run;
it never claims the ten-million-row acceptance criterion.  Scheduler claim
concurrency is intentionally not measured here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import quantiles
from typing import Any
from urllib.parse import urlsplit

import asyncpg
import psutil

from tests.postgres_conformance import (
    resolve_postgres_test_url,
    temporary_postgres_database,
)

_MAX_ACCOUNTS = 10_000_000
_FULL_BASELINE_ACCOUNTS = 1_000_000
_MAX_SMOKE_ACCOUNTS = 1_000
_MAX_PAGE_SIZE = 100
_MAX_CLAIM_SIZE = 100
_ACTIVE_COHORT = 100
_WORKSPACE_ID = "qrac2-capacity-workspace"
_BATCH_SIZE = 5_000


class CapacityConfigurationError(ValueError):
    """Raised when an experiment is not explicitly configured for PostgreSQL."""


class CapacityMeasurementError(RuntimeError):
    """Raised when the disposable application-schema experiment cannot start."""


@dataclass(frozen=True)
class CapacityConfig:
    database_url: str
    accounts: int
    page_size: int
    claim_size: int
    samples: int
    full_run: bool = False


def validate_database_url(database_url: str | None) -> str:
    if not database_url:
        raise CapacityConfigurationError(
            "TEST_DATABASE_URL_PG must point to a dedicated disposable PostgreSQL database"
        )
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgres", "postgresql", "postgresql+asyncpg"}:
        raise CapacityConfigurationError("TEST_DATABASE_URL_PG must use a PostgreSQL URL")
    if not parsed.hostname:
        raise CapacityConfigurationError("TEST_DATABASE_URL_PG must include a database host")
    return database_url


def build_config(
    database_url: str | None,
    *,
    accounts: int,
    page_size: int,
    claim_size: int,
    samples: int,
    full_run: bool = False,
) -> CapacityConfig:
    if not 1 <= accounts <= _MAX_ACCOUNTS:
        raise CapacityConfigurationError(f"accounts must be between 1 and {_MAX_ACCOUNTS}")
    if full_run and accounts != _MAX_ACCOUNTS:
        raise CapacityConfigurationError(f"full_run requires accounts={_MAX_ACCOUNTS}")
    if not full_run and accounts > _MAX_SMOKE_ACCOUNTS:
        raise CapacityConfigurationError(
            f"smoke accounts must be at most {_MAX_SMOKE_ACCOUNTS}; omit --accounts for full run"
        )
    if not 1 <= page_size <= _MAX_PAGE_SIZE:
        raise CapacityConfigurationError(f"page_size must be between 1 and {_MAX_PAGE_SIZE}")
    if not 1 <= claim_size <= _MAX_CLAIM_SIZE:
        raise CapacityConfigurationError(f"claim_size must be between 1 and {_MAX_CLAIM_SIZE}")
    if not 1 <= samples <= 100:
        raise CapacityConfigurationError("samples must be between 1 and 100")
    return CapacityConfig(
        database_url=validate_database_url(database_url),
        accounts=accounts,
        page_size=page_size,
        claim_size=claim_size,
        samples=samples,
        full_run=full_run,
    )


def _plan_nodes(plan: dict[str, Any]) -> list[str]:
    nodes = [str(plan.get("Node Type", ""))]
    for child in plan.get("Plans", []) or []:
        if isinstance(child, dict):
            nodes.extend(_plan_nodes(child))
    return nodes


def plan_violations(explain_payload: Any) -> list[str]:
    """Return planner operations forbidden by bounded inventory access."""
    if isinstance(explain_payload, str):
        try:
            explain_payload = json.loads(explain_payload)
        except json.JSONDecodeError:
            return ["invalid query plan"]
    if not isinstance(explain_payload, list) or not explain_payload:
        return ["missing query plan"]
    if not isinstance(explain_payload[0], dict):
        return ["missing query plan"]
    plan = explain_payload[0].get("Plan")
    if not isinstance(plan, dict):
        return ["missing root plan"]
    forbidden = {"Seq Scan", "Materialize"}
    return sorted({node for node in _plan_nodes(plan) if node in forbidden})


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    return float(quantiles(values, n=100, method="inclusive")[int(percentile) - 1])


def _safe_database_identity(database_url: str) -> dict[str, str | None]:
    parsed = urlsplit(database_url)
    return {
        "scheme": parsed.scheme,
        "host": parsed.hostname,
        "database": parsed.path.lstrip("/") or None,
    }


def _asyncpg_url(database_url: str) -> str:
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _process_snapshot(process: psutil.Process) -> dict[str, int]:
    """Measure this benchmark process and its currently live descendants."""
    try:
        descendants = process.children(recursive=True)
    except psutil.Error:
        descendants = []
    return {
        "pid": process.pid,
        "rss_bytes": process.memory_info().rss,
        "process_count": 1 + sum(child.is_running() for child in descendants),
    }


def _run_alembic(database_url: str) -> None:
    root = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "DATABASE_URL": database_url,
        "SQLALCHEMY_DATABASE_URI": database_url,
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise CapacityMeasurementError(
            f"Alembic upgrade head failed with exit {result.returncode}: {detail}"
        )


async def _create_workspace(connection: asyncpg.Connection) -> None:
    now = datetime.now(UTC)
    await connection.execute(
        """
        INSERT INTO workspaces (id, name, slug, active, created_at, updated_at)
        VALUES ($1, $2, $3, true, $4, $4)
        ON CONFLICT (id) DO NOTHING
        """,
        _WORKSPACE_ID,
        "Q1 capacity benchmark workspace",
        "qrac2-capacity-workspace",
        now,
    )


_ACCOUNT_INSERT = """
INSERT INTO browser_accounts (
    id, workspace_id, site, label, node_id, profile_id, profile_version,
    profile_manifest_id, runtime_bundle_id, runtime_bundle_version,
    login_rule_id, login_rule_version, auth_required, platform_identity,
    auth_evidence, evidence_source, evidence_observed_at, manual_confirmed_by,
    status, revision, paused, status_reason_code, created_at, updated_at
) VALUES (
    $1, $2, $3, $4, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
    false, NULL, 'unknown', NULL, NULL, NULL, $5, 0, false, NULL, $6, $6
)
"""


def _account_rows(
    start: int,
    end: int,
    *,
    active_count: int,
    now: datetime,
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for number in range(start, end):
        account_id = f"{number:032x}"
        status = "saved" if number <= active_count else "dormant"
        rows.append(
            (
                account_id,
                _WORKSPACE_ID,
                "capacity.example",
                f"capacity-{number}",
                status,
                now,
            )
        )
    return rows


async def _populate_accounts(
    connection: asyncpg.Connection,
    *,
    dormant_start: int,
    dormant_end: int,
    active_count: int,
) -> None:
    now = datetime.now(UTC)
    start = 1 if dormant_start == 1 else active_count + dormant_start
    end = active_count + dormant_end + 1
    for batch_start in range(start, end, _BATCH_SIZE):
        batch_end = min(batch_start + _BATCH_SIZE, end)
        await connection.executemany(
            _ACCOUNT_INSERT,
            _account_rows(batch_start, batch_end, active_count=active_count, now=now),
        )
    await connection.execute("ANALYZE browser_accounts")


_PAGE_SQL = """
SELECT id, workspace_id, site, label, node_id, profile_id, profile_version,
       profile_manifest_id, runtime_bundle_id, runtime_bundle_version,
       login_rule_id, login_rule_version, auth_required, platform_identity,
       auth_evidence, evidence_source, evidence_observed_at, manual_confirmed_by,
       status, revision, paused, status_reason_code, created_at, updated_at
FROM browser_accounts
WHERE workspace_id = $1 AND status = $2 AND id > $3
ORDER BY id ASC
LIMIT $4
"""


async def _measure_phase(
    connection: asyncpg.Connection,
    process: psutil.Process,
    *,
    dormant_accounts: int,
    active_cohort: int,
    page_size: int,
    samples: int,
) -> dict[str, Any]:
    process_before = _process_snapshot(process)
    explain_payload = await connection.fetchval(
        f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {_PAGE_SQL}",
        _WORKSPACE_ID,
        "dormant",
        "0" * 32,
        page_size + 1,
    )
    if isinstance(explain_payload, str):
        explain_payload = json.loads(explain_payload)
    page_times: list[float] = []
    cursor = "0" * 32
    page_rows_last_sample = 0
    for _ in range(samples):
        started = time.perf_counter()
        rows = await connection.fetch(
            _PAGE_SQL,
            _WORKSPACE_ID,
            "dormant",
            cursor,
            page_size + 1,
        )
        page_times.append((time.perf_counter() - started) * 1000)
        visible_rows = rows[:page_size]
        if visible_rows:
            page_rows_last_sample = len(visible_rows)
            cursor = str(visible_rows[-1]["id"])
    process_after = _process_snapshot(process)
    return {
        "dormant_accounts": dormant_accounts,
        "active_cohort": active_cohort,
        "page_rows_last_sample": page_rows_last_sample,
        "page_p95_ms": round(_percentile(page_times, 95), 3),
        "rss_before_bytes": process_before["rss_bytes"],
        "rss_after_bytes": process_after["rss_bytes"],
        "rss_delta_bytes": process_after["rss_bytes"] - process_before["rss_bytes"],
        "process_pid": process_after["pid"],
        "process_count_before": process_before["process_count"],
        "process_count_after": process_after["process_count"],
        "page_plan_violations": plan_violations(explain_payload),
        "page_explain_analyze": explain_payload,
    }


async def _run(config: CapacityConfig) -> dict[str, Any]:
    if resolve_postgres_test_url() is None:
        raise CapacityConfigurationError(
            "TEST_DATABASE_URL_PG must configure a test-only administrative database"
        )
    process = psutil.Process()
    started_at = time.perf_counter()
    active_cohort = min(_ACTIVE_COHORT, max(1, config.accounts // 10))
    phases: list[dict[str, Any]] = []
    async with temporary_postgres_database("browser_account_capacity") as database_url:
        await asyncio.to_thread(_run_alembic, database_url)
        connection = await asyncpg.connect(_asyncpg_url(database_url))
        try:
            await _create_workspace(connection)
            if config.full_run:
                await _populate_accounts(
                    connection,
                    dormant_start=1,
                    dormant_end=_FULL_BASELINE_ACCOUNTS,
                    active_count=_ACTIVE_COHORT,
                )
                phases.append(
                    await _measure_phase(
                        connection,
                        process,
                        dormant_accounts=_FULL_BASELINE_ACCOUNTS,
                        active_cohort=_ACTIVE_COHORT,
                        page_size=config.page_size,
                        samples=config.samples,
                    )
                )
                await _populate_accounts(
                    connection,
                    dormant_start=_FULL_BASELINE_ACCOUNTS + 1,
                    dormant_end=_MAX_ACCOUNTS,
                    active_count=_ACTIVE_COHORT,
                )
                phases.append(
                    await _measure_phase(
                        connection,
                        process,
                        dormant_accounts=_MAX_ACCOUNTS,
                        active_cohort=_ACTIVE_COHORT,
                        page_size=config.page_size,
                        samples=config.samples,
                    )
                )
            else:
                await _populate_accounts(
                    connection,
                    dormant_start=1,
                    dormant_end=config.accounts,
                    active_count=active_cohort,
                )
                phases.append(
                    await _measure_phase(
                        connection,
                        process,
                        dormant_accounts=config.accounts,
                        active_cohort=active_cohort,
                        page_size=config.page_size,
                        samples=config.samples,
                    )
                )
        finally:
            await connection.close()
    measured_page_violations = {
        violation for phase in phases for violation in phase["page_plan_violations"]
    }
    page_ok = not measured_page_violations
    process_ok = all(
        phase["process_pid"] == process.pid
        and phase["process_count_before"] == phase["process_count_after"]
        for phase in phases
    )
    claim_metadata = {
        "status": "NOT VERIFIED",
        "claim_size": config.claim_size,
        "reason": (
            "No scheduler implementation or concurrent claim experiment "
            "is exercised by this script."
        ),
    }
    result: dict[str, Any] = {
        "database": _safe_database_identity(database_url),
        "database_is_disposable": True,
        "page_size": config.page_size,
        "page_rows_last_sample": phases[-1]["page_rows_last_sample"],
        "page_p95_ms": phases[-1]["page_p95_ms"],
        "page_plan_violations": sorted(measured_page_violations),
        "claim": claim_metadata,
        "claim_size": config.claim_size,
        "claim_plan_violations": None,
        "measurement_scope": "inventory_sql_benchmark",
        "application_process_metrics": "NOT VERIFIED",
        "production_acceptance": "NOT VERIFIED",
        "samples": config.samples,
        "full_run": config.full_run,
        "phases": phases,
        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
        "note": (
            "Actual browser_accounts schema and service keyset semantics; "
            "scheduler claims and platform throughput are not verified."
        ),
    }
    if not config.full_run:
        # Tiny tables legitimately choose Seq Scan; retain the raw violation
        # while keeping smoke status about runner/schema execution only.
        result["smoke_page_plan_violations"] = sorted(measured_page_violations)
    if config.full_run:
        baseline, target = phases
        baseline_rss = baseline["rss_after_bytes"]
        rss_growth = target["rss_after_bytes"] - baseline_rss
        rss_growth_ratio = rss_growth / baseline_rss if baseline_rss > 0 else 0.0
        rss_ok = rss_growth_ratio <= 0.20
        page_limit_ok = config.page_size <= _MAX_PAGE_SIZE
        active_ok = baseline["active_cohort"] == target["active_cohort"] == _ACTIVE_COHORT
        process_stable = (
            baseline["process_pid"] == target["process_pid"] == process.pid
            and baseline["process_count_after"] == target["process_count_after"]
        )
        full_criteria = {
            "accounts_1m_measured": baseline["dormant_accounts"] == _FULL_BASELINE_ACCOUNTS,
            "accounts_10m_measured": target["dormant_accounts"] == _MAX_ACCOUNTS,
            "fixed_saved_row_cohort": active_ok,
            "same_benchmark_process": process_stable,
            "page_plans_bounded": page_ok,
            "page_limit_at_most_100": page_limit_ok,
            "rss_growth_at_most_20_percent": rss_ok,
        }
        result["rss_growth_bytes"] = rss_growth
        result["rss_growth_ratio"] = round(rss_growth_ratio, 6)
        result["inventory_criteria"] = full_criteria
        result["inventory_criteria_met"] = all(full_criteria.values())
        result["bounded"] = bool(result["inventory_criteria_met"])
    else:
        result["inventory_criteria"] = "NOT RUN: --accounts smoke run is not ten-million acceptance"
        result["inventory_criteria_met"] = None
        result["bounded"] = process_ok
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--accounts",
        type=int,
        default=None,
        help="Run an isolated smoke benchmark at this size; omitted means the full 1M→10M run.",
    )
    parser.add_argument("--page-size", type=int, default=_MAX_PAGE_SIZE)
    parser.add_argument("--claim-size", type=int, default=_MAX_CLAIM_SIZE)
    parser.add_argument("--samples", type=int, default=20)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        full_run = args.accounts is None
        config = build_config(
            os.environ.get("TEST_DATABASE_URL_PG"),
            accounts=_MAX_ACCOUNTS if full_run else args.accounts,
            page_size=args.page_size,
            claim_size=args.claim_size,
            samples=args.samples,
            full_run=full_run,
        )
        result = asyncio.run(_run(config))
    except (
        CapacityConfigurationError,
        CapacityMeasurementError,
        OSError,
        asyncpg.PostgresError,
    ) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}), file=sys.stderr)
        return 2
    status = (
        "inventory_only_passed"
        if result["inventory_criteria_met"] is True
        else ("passed_smoke" if result["bounded"] else "failed")
    )
    print(json.dumps({"status": status, **result}, ensure_ascii=False, sort_keys=True))
    # A successful SQL benchmark does not certify application RSS, active
    # browser processes, or concurrent claims. Full acceptance stays incomplete.
    return 0 if not config.full_run and result["bounded"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

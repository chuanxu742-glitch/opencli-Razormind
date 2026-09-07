"""Measure bounded browser-account inventory access on an explicit PostgreSQL database.

The experiment uses a temporary table and never writes production account rows. It is
intended for the Q1/I acceptance run with ``TEST_DATABASE_URL_PG`` pointing at a
dedicated disposable database. SQLite and implicit/default connection settings are
rejected so a local unit database cannot be mistaken for a distributed-capacity proof.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from statistics import quantiles
from typing import Any
from urllib.parse import urlsplit

import asyncpg
import psutil

_MAX_ACCOUNTS = 10_000_000
_MAX_PAGE_SIZE = 100
_MAX_CLAIM_SIZE = 100
_WORKSPACE_ID = "qrac2-capacity-workspace"


class CapacityConfigurationError(ValueError):
    """Raised when an experiment is not explicitly configured for PostgreSQL."""


@dataclass(frozen=True)
class CapacityConfig:
    database_url: str
    accounts: int
    page_size: int
    claim_size: int
    samples: int


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
) -> CapacityConfig:
    if not 1 <= accounts <= _MAX_ACCOUNTS:
        raise CapacityConfigurationError(f"accounts must be between 1 and {_MAX_ACCOUNTS}")
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
    )


def _plan_nodes(plan: dict[str, Any]) -> list[str]:
    nodes = [str(plan.get("Node Type", ""))]
    for child in plan.get("Plans", []) or []:
        nodes.extend(_plan_nodes(child))
    return nodes


def plan_violations(explain_payload: Any) -> list[str]:
    """Return planner operations forbidden by the bounded-inventory contract."""
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
    return {"scheme": parsed.scheme, "host": parsed.hostname, "database": parsed.path.lstrip("/") or None}


async def _run(config: CapacityConfig) -> dict[str, Any]:
    table = f"qrac2_capacity_{uuid.uuid4().hex}"
    process = psutil.Process()
    rss_before = process.memory_info().rss
    started_at = time.perf_counter()
    database_url = config.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    connection = await asyncpg.connect(database_url)
    try:
        await connection.execute(
            f"""
            CREATE TEMP TABLE {table} (
                id BIGINT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                status TEXT NOT NULL,
                available_at TIMESTAMPTZ NOT NULL,
                node_id TEXT NULL
            ) ON COMMIT PRESERVE ROWS
            """
        )
        await connection.execute(
            f"CREATE INDEX {table}_workspace_status_id ON {table} (workspace_id, status, id)"
        )
        await connection.execute(
            f"CREATE INDEX {table}_node_status_available_id ON {table} (node_id, status, available_at, id)"
        )
        await connection.execute(
            f"""
            INSERT INTO {table} (id, workspace_id, status, available_at, node_id)
            SELECT value, $2, 'dormant', now() + (value * interval '1 microsecond'), NULL
            FROM generate_series(1, $1::bigint) AS generated(value)
            """,
            config.accounts,
            _WORKSPACE_ID,
        )
        await connection.execute(f"ANALYZE {table}")

        page_sql = (
            f"SELECT id FROM {table} WHERE workspace_id = $1 AND status = 'dormant' "
            "AND id > $2 ORDER BY id LIMIT $3"
        )
        claim_sql = (
            f"SELECT id FROM {table} WHERE workspace_id = $1 AND status = 'dormant' "
            "ORDER BY id LIMIT $2 FOR UPDATE SKIP LOCKED"
        )
        page_plan = await connection.fetchval(
            f"EXPLAIN (FORMAT JSON) {page_sql}", _WORKSPACE_ID, 0, config.page_size
        )
        claim_plan = await connection.fetchval(
            f"EXPLAIN (FORMAT JSON) {claim_sql}", _WORKSPACE_ID, config.claim_size
        )
        page_violations = plan_violations(page_plan)
        claim_violations = plan_violations(claim_plan)

        page_times: list[float] = []
        claim_times: list[float] = []
        cursor = 0
        for _ in range(config.samples):
            begin = time.perf_counter()
            rows = await connection.fetch(page_sql, _WORKSPACE_ID, cursor, config.page_size)
            page_times.append((time.perf_counter() - begin) * 1000)
            if rows:
                cursor = int(rows[-1]["id"])
            begin = time.perf_counter()
            async with connection.transaction():
                await connection.fetch(claim_sql, _WORKSPACE_ID, config.claim_size)
            claim_times.append((time.perf_counter() - begin) * 1000)

        rss_after = process.memory_info().rss
        elapsed = time.perf_counter() - started_at
        return {
            "database": _safe_database_identity(config.database_url),
            "table": table,
            "accounts": config.accounts,
            "page_size": config.page_size,
            "claim_size": config.claim_size,
            "samples": config.samples,
            "page_rows_last_sample": len(rows),
            "page_p95_ms": round(_percentile(page_times, 95), 3),
            "claim_p95_ms": round(_percentile(claim_times, 95), 3),
            "rss_before_bytes": rss_before,
            "rss_after_bytes": rss_after,
            "rss_delta_bytes": rss_after - rss_before,
            "active_processes": 1,
            "elapsed_seconds": round(elapsed, 3),
            "page_plan_violations": page_violations,
            "claim_plan_violations": claim_violations,
            "bounded": not page_violations and not claim_violations,
            "note": "Temporary inventory table only; this is not a distributed scheduler or platform-throughput proof.",
        }
    finally:
        await connection.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accounts", type=int, default=_MAX_ACCOUNTS)
    parser.add_argument("--page-size", type=int, default=_MAX_PAGE_SIZE)
    parser.add_argument("--claim-size", type=int, default=_MAX_CLAIM_SIZE)
    parser.add_argument("--samples", type=int, default=20)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = build_config(
            os.environ.get("TEST_DATABASE_URL_PG"),
            accounts=args.accounts,
            page_size=args.page_size,
            claim_size=args.claim_size,
            samples=args.samples,
        )
        result = asyncio.run(_run(config))
    except (CapacityConfigurationError, OSError, asyncpg.PostgresError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}), file=sys.stderr)
        return 2
    status = "passed" if result["bounded"] else "failed"
    print(json.dumps({"status": status, **result}, ensure_ascii=False, sort_keys=True))
    return 0 if result["bounded"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

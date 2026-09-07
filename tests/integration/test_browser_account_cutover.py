from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.models.browser import BrowserBinding
from tests.postgres_conformance import temporary_postgres_database

_ROOT = Path(__file__).parents[2]
_PRE_CUTOVER_HEAD = "add_browser_portal_security"
_FINAL_HEAD = "finalize_browser_account_cutover"


def _upgrade(database_url: str, revision: str) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "DATABASE_URL": database_url,
        "SQLALCHEMY_DATABASE_URI": database_url,
    }
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


@pytest.mark.asyncio
async def test_legacy_binding_writes_require_workspace_account_migration(client, db_session):
    db_session.add(
        BrowserBinding(
            id="legacy-binding",
            site="example.test",
            browser_endpoint="http://legacy-browser:9222",
            notes="physical profile retained",
        )
    )
    await db_session.commit()

    inspection = await client.get("/api/v1/browsers/bindings/migration")
    assert inspection.status_code == 200
    item = inspection.json()["data"]["items"]
    assert item == [
        {
            "binding_id": "legacy-binding",
            "site": "example.test",
            "browser_endpoint": "http://legacy-browser:9222",
            "profile_name": "http://legacy-browser:9222",
            "status": "account_migration_required",
            "ambiguity": True,
            "account_ids": [],
            "preserved": True,
        }
    ]

    for request in (
        client.post(
            "/api/v1/browsers/bindings",
            json={"site": "example.test", "browser_endpoint": "http://new-browser:9222"},
        ),
        client.delete("/api/v1/browsers/bindings/legacy-binding"),
    ):
        response = await request
        assert response.status_code == 410
        assert response.json()["detail"]["code"] == "browser_account_migration_required"
        assert "workspace browser-account UI" in response.json()["detail"]["message"]

    assert await db_session.get(BrowserBinding, "legacy-binding") is not None


@pytest.mark.asyncio
@pytest.mark.postgres_conformance
async def test_final_cutover_preserves_legacy_profiles_and_allows_same_site_accounts():
    async with temporary_postgres_database("browser_account_cutover") as database_url:
        baseline = await asyncio.to_thread(_upgrade, database_url, _PRE_CUTOVER_HEAD)
        assert baseline.returncode == 0, baseline.stdout + baseline.stderr

        engine = create_async_engine(database_url)
        now = datetime.now(UTC)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO users (id, subject, disabled, created_at, updated_at)
                        VALUES ('cutover-user', 'cutover-user', false, :now, :now)
                        """
                    ),
                    {"now": now},
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO workspaces (id, name, slug, active, created_at, updated_at)
                        VALUES
                            ('cutover-workspace-a', 'Cutover A', 'cutover-a', true, :now, :now),
                            ('cutover-workspace-b', 'Cutover B', 'cutover-b', true, :now, :now)
                        """
                    ),
                    {"now": now},
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO browser_instances
                            (id, endpoint, mode, label, profile_name, created_at, updated_at)
                        VALUES
                            ('legacy-instance', 'http://legacy-browser:9222', 'bridge', 'Legacy',
                             'legacy-physical-profile', :now, :now)
                        """
                    ),
                    {"now": now},
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO browser_bindings
                            (id, site, browser_endpoint, notes, created_at, updated_at)
                        VALUES ('legacy-binding', 'same-site.test', 'http://legacy-browser:9222',
                                'preserve original profile', :now, :now)
                        """
                    ),
                    {"now": now},
                )

            upgraded = await asyncio.to_thread(_upgrade, database_url, "head")
            assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr

            async with engine.begin() as connection:
                preserved = (
                    await connection.execute(
                        text(
                            """
                            SELECT bindings.browser_endpoint, instances.profile_name
                            FROM browser_bindings AS bindings
                            JOIN browser_instances AS instances
                              ON instances.endpoint = bindings.browser_endpoint
                            WHERE bindings.id = 'legacy-binding'
                            """
                        )
                    )
                ).one()
                assert tuple(preserved) == (
                    "http://legacy-browser:9222",
                    "legacy-physical-profile",
                )

                constraints = set(
                    await connection.scalars(
                        text(
                            """
                            SELECT conname
                            FROM pg_constraint
                            WHERE conrelid = 'browser_bindings'::regclass
                            """
                        )
                    )
                )
                assert "uq_browser_bindings_site" not in constraints

                await connection.execute(
                    text(
                        """
                        INSERT INTO browser_bindings
                            (id, site, browser_endpoint, notes, created_at, updated_at)
                        VALUES ('diagnostic-copy', 'same-site.test', 'http://diagnostic-browser:9222',
                                'legacy diagnostics only', :now, :now)
                        """
                    ),
                    {"now": now},
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO browser_accounts
                            (id, workspace_id, site, label, profile_id, created_at, updated_at)
                        VALUES
                            ('account-a', 'cutover-workspace-a', 'same-site.test', 'Account A',
                             'workspace-a-profile', :now, :now),
                            ('account-b', 'cutover-workspace-b', 'same-site.test', 'Account B',
                             'workspace-b-profile', :now, :now)
                        """
                    ),
                    {"now": now},
                )
                accounts = await connection.scalars(
                    text(
                        """
                        SELECT workspace_id
                        FROM browser_accounts
                        WHERE site = 'same-site.test'
                        ORDER BY workspace_id
                        """
                    )
                )
                assert list(accounts) == ["cutover-workspace-a", "cutover-workspace-b"]
                versions = list(await connection.scalars(text("SELECT version_num FROM alembic_version")))
                assert versions == [_FINAL_HEAD]
        finally:
            await engine.dispose()

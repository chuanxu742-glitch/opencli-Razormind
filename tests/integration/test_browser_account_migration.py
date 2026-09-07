from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.postgres_conformance import temporary_postgres_database

pytestmark = pytest.mark.postgres_conformance

_ROOT = Path(__file__).parents[2]


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
    )


@pytest.mark.asyncio
async def test_refine_head_preserves_legacy_binding_and_enforces_workspace_pairs():
    async with temporary_postgres_database("browser_account_migration") as database_url:
        baseline = await asyncio.to_thread(_upgrade, database_url, "add_browser_accounts")
        assert baseline.returncode == 0, baseline.stdout + baseline.stderr

        engine = create_async_engine(database_url)
        now = datetime.now(timezone.utc)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO users (id, subject, disabled, created_at, updated_at)
                    VALUES ('legacy-user', 'legacy-user', false, :now, :now)
                    """
                ),
                {"now": now},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO workspaces (id, name, slug, active, created_at, updated_at)
                    VALUES
                        ('legacy-workspace', 'Legacy workspace', 'legacy-workspace', true, :now, :now),
                        ('other-workspace', 'Other workspace', 'other-workspace', true, :now, :now)
                    """
                ),
                {"now": now},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO projects
                        (id, workspace_id, name, slug, created_by_user_id, archived, created_at, updated_at)
                    VALUES ('legacy-project', 'legacy-workspace', 'Legacy project', 'legacy-project',
                            'legacy-user', false, :now, :now)
                    """
                ),
                {"now": now},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO sources
                        (id, workspace_id, name, slug, adapter_type, status, current_revision_number,
                         created_by_user_id, created_at, updated_at)
                    VALUES ('legacy-source', 'legacy-workspace', 'Legacy source', 'legacy-source',
                            'opencli', 'active', 1, 'legacy-user', :now, :now)
                    """
                ),
                {"now": now},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO source_revisions
                        (id, source_id, revision_number, adapter_config, created_by_user_id,
                         created_at, updated_at)
                    VALUES ('legacy-source-revision', 'legacy-source', 1, '{"site": "legacy"}',
                            'legacy-user', :now, :now)
                    """
                ),
                {"now": now},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO source_bindings
                        (id, project_id, source_id, name, slug, status, current_revision_number,
                         created_by_user_id, created_at, updated_at)
                    VALUES ('legacy-binding', 'legacy-project', 'legacy-source', 'Legacy binding',
                            'legacy-binding', 'active', 1, 'legacy-user', :now, :now)
                    """
                ),
                {"now": now},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO source_binding_revisions
                        (id, source_binding_id, revision_number, pinned_source_revision_id,
                         scope_config, created_by_user_id, created_at, updated_at, account_id)
                    VALUES ('legacy-binding-revision', 'legacy-binding', 1, 'legacy-source-revision',
                            '{"legacy": true}', 'legacy-user', :now, :now, NULL)
                    """
                ),
                {"now": now},
            )

        refined = await asyncio.to_thread(_upgrade, database_url, "head")
        assert refined.returncode == 0, refined.stdout + refined.stderr

        async with engine.begin() as connection:
            legacy = (
                await connection.execute(
                    text(
                        """
                        SELECT workspace_id, account_id
                        FROM source_binding_revisions
                        WHERE id = 'legacy-binding-revision'
                        """
                    )
                )
            ).one()
            assert tuple(legacy) == ("legacy-workspace", None)

            constraints = {
                row[0]
                for row in (
                    await connection.execute(
                        text(
                            """
                            SELECT conname
                            FROM pg_constraint
                            WHERE conrelid IN (
                                'source_binding_revisions'::regclass,
                                'browser_durable_commands'::regclass,
                                'browser_profile_manifests'::regclass
                            )
                            """
                        )
                    )
                ).all()
            }
            assert {
                "fk_source_binding_revision_account_workspace",
                "fk_browser_commands_session_workspace",
                "fk_browser_profile_manifest_command_workspace",
            } <= constraints

            await connection.execute(
                text(
                    """
                    INSERT INTO browser_accounts
                        (id, workspace_id, site, label, created_at, updated_at)
                    VALUES
                        ('account-a', 'legacy-workspace', 'example.test', 'Account A', :now, :now),
                        ('account-b', 'other-workspace', 'example.test', 'Account B', :now, :now)
                    """
                ),
                {"now": now},
            )
            with pytest.raises(IntegrityError):
                await connection.execute(
                    text(
                        """
                        UPDATE source_binding_revisions
                        SET account_id = 'account-b'
                        WHERE id = 'legacy-binding-revision'
                        """
                    )
                )

        await engine.dispose()

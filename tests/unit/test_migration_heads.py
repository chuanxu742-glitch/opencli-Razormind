import asyncio
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.config import get_settings
from tests.postgres_conformance import temporary_postgres_database


def test_alembic_has_one_head():
    config = Config()
    config.set_main_option("script_location", "backend/migrations")

    assert ScriptDirectory.from_config(config).get_heads() == ["add_workflow_run_actor"]


def test_ci_downgrade_target_is_unambiguous():
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    target = re.search(r"alembic downgrade (\S+)", workflow)
    assert target is not None

    config = Config()
    config.set_main_option("script_location", "backend/migrations")
    script = ScriptDirectory.from_config(config)

    assert script._downgrade_revs(target.group(1), tuple(script.get_heads()))


def test_upgrade_head_creates_identity_and_operations_tables(monkeypatch):
    with TemporaryDirectory() as directory:
        database = Path(directory) / "migration.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database.as_posix()}")
        get_settings.cache_clear()
        config = Config()
        config.set_main_option("script_location", "backend/migrations")

        try:
            command.upgrade(config, "head")
        finally:
            get_settings.cache_clear()

        connection = sqlite3.connect(database)
        try:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            task_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(collection_tasks)")
            }
            task_foreign_keys = list(
                connection.execute("PRAGMA foreign_key_list(collection_tasks)")
            )
            workflow_run_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(workflow_runs)")
            }
            workflow_run_foreign_keys = list(
                connection.execute("PRAGMA foreign_key_list(workflow_runs)")
            )
        finally:
            connection.close()

    assert {
        "users",
        "workspace_memberships",
        "operations_work_items",
        "operations_agent_identities",
        "agent_permission_profiles",
        "operations_agent_drafts",
        "published_operations_agent_versions",
        "operations_agent_runs",
        "consumer_grants",
        "automations",
        "projects",
        "workflows",
        "workflow_drafts",
        "workflow_versions",
        "iii_collection_commands",
        "iii_collection_attempts",
        "iii_collection_outbox",
        "iii_collection_lifecycle_observations",
        "browser_accounts",
        "browser_login_sessions",
        "browser_account_leases",
        "browser_durable_commands",
        "browser_profile_manifests",
        "browser_portal_tickets",
        "browser_portal_owners",
    } <= tables
    assert "requested_by_user_id" in task_columns
    assert any(
        row[2] == "users"
        and row[3] == "requested_by_user_id"
        and row[4] == "id"
        and row[6] == "SET NULL"
        for row in task_foreign_keys
    )
    assert "requested_by_user_id" in workflow_run_columns
    assert any(
        row[2] == "users"
        and row[3] == "requested_by_user_id"
        and row[4] == "id"
        and row[6] == "SET NULL"
        for row in workflow_run_foreign_keys
    )


@pytest.mark.asyncio
async def test_execution_actor_migrations_upgrade_disposable_postgres():
    async with temporary_postgres_database("execution_actor_migration") as database_url:
        environment = {**os.environ, "DATABASE_URL": database_url}
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=Path(__file__).parents[2],
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr

        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                columns = (
                    await connection.execute(
                        text(
                            """
                            SELECT table_name, is_nullable
                            FROM information_schema.columns
                            WHERE table_schema = 'public'
                              AND table_name IN ('collection_tasks', 'workflow_runs')
                              AND column_name = 'requested_by_user_id'
                            """
                        )
                    )
                ).all()
                delete_rules = (
                    await connection.execute(
                        text(
                            """
                            SELECT kcu.table_name, rc.delete_rule
                            FROM information_schema.referential_constraints AS rc
                            JOIN information_schema.key_column_usage AS kcu
                              ON kcu.constraint_schema = rc.constraint_schema
                             AND kcu.constraint_name = rc.constraint_name
                            WHERE kcu.table_schema = 'public'
                              AND kcu.table_name IN ('collection_tasks', 'workflow_runs')
                              AND kcu.column_name = 'requested_by_user_id'
                            """
                        )
                    )
                ).all()
        finally:
            await engine.dispose()

    assert set(columns) == {
        ("collection_tasks", "YES"),
        ("workflow_runs", "YES"),
    }
    assert set(delete_rules) == {
        ("collection_tasks", "SET NULL"),
        ("workflow_runs", "SET NULL"),
    }


def test_workflow_run_version_foreign_key_is_restrict(monkeypatch):
    with TemporaryDirectory() as directory:
        database = Path(directory) / "migration.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database.as_posix()}")
        get_settings.cache_clear()
        config = Config()
        config.set_main_option("script_location", "backend/migrations")

        try:
            command.upgrade(config, "head")
        finally:
            get_settings.cache_clear()

        connection = sqlite3.connect(database)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(workflow_runs)")}
            foreign_keys = list(connection.execute("PRAGMA foreign_key_list(workflow_runs)"))
        finally:
            connection.close()

    assert "workflow_version_id" in columns
    assert any(
        row[2] == "workflow_versions"
        and row[3] == "workflow_version_id"
        and row[4] == "id"
        and row[6] == "RESTRICT"
        for row in foreign_keys
    )
    assert "studio_workflow_version_id" in columns
    assert any(
        row[2] == "studio_workflow_versions"
        and row[3] == "studio_workflow_version_id"
        and row[4] == "id"
        and row[6] == "RESTRICT"
        for row in foreign_keys
    )


def test_c2_downgrade_removes_only_operations_agent_tables(monkeypatch):
    with TemporaryDirectory() as directory:
        database = Path(directory) / "migration.db"
        monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database.as_posix()}")
        get_settings.cache_clear()
        config = Config()
        config.set_main_option("script_location", "backend/migrations")

        try:
            command.upgrade(config, "head")
            command.downgrade(config, "b9c0d1e2f3a4")
        finally:
            get_settings.cache_clear()

        connection = sqlite3.connect(database)
        try:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        finally:
            connection.close()

    assert "workspaces" in tables
    assert (
        not {
            "operations_agent_identities",
            "agent_permission_profiles",
            "operations_agent_drafts",
            "published_operations_agent_versions",
            "operations_agent_runs",
        }
        & tables
    )

"""Regression coverage for databases previously run from plugin-hub."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).parents[2]


def _current_migration_head() -> str:
    config = Config()
    config.set_main_option("script_location", str(REPO_ROOT / "backend" / "migrations"))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return head


def _migration_environment(path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{path.as_posix()}",
    }


def _upgrade_to(path: Path, revision: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=REPO_ROOT,
        env=_migration_environment(path),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _add_legacy_edge_node_schema(path: Path) -> None:
    """Model node/browser tables present before account-runtime migrations."""
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS edge_nodes (
            id VARCHAR(36) NOT NULL PRIMARY KEY,
            url VARCHAR(512) NOT NULL UNIQUE,
            label VARCHAR(255) NOT NULL DEFAULT '',
            protocol VARCHAR(10) NOT NULL DEFAULT 'http',
            mode VARCHAR(20) NOT NULL DEFAULT 'bridge',
            status VARCHAR(20) NOT NULL DEFAULT 'offline',
            last_seen_at DATETIME,
            ip VARCHAR(45),
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
        CREATE TABLE IF NOT EXISTS edge_node_events (
            id VARCHAR(36) NOT NULL PRIMARY KEY,
            node_id VARCHAR(36) NOT NULL,
            event VARCHAR(50) NOT NULL,
            ip VARCHAR(45),
            event_meta JSON,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            FOREIGN KEY (node_id) REFERENCES edge_nodes(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS ix_edge_node_events_node_id ON edge_node_events(node_id);
        CREATE TABLE IF NOT EXISTS browser_instances (
            id VARCHAR(36) NOT NULL PRIMARY KEY,
            endpoint VARCHAR(255) NOT NULL UNIQUE,
            mode VARCHAR(20) NOT NULL DEFAULT 'bridge',
            label VARCHAR(100) NOT NULL DEFAULT '',
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
        CREATE TABLE IF NOT EXISTS browser_bindings (
            browser_endpoint VARCHAR(255) NOT NULL,
            site VARCHAR(100) NOT NULL,
            notes TEXT,
            id VARCHAR(36) NOT NULL PRIMARY KEY,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            CONSTRAINT uq_browser_bindings_site UNIQUE (site)
        );
        """
    )
    connection.commit()
    connection.close()


def _create_legacy_plugin_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE alembic_version (
            version_num VARCHAR(32) NOT NULL PRIMARY KEY
        );
        INSERT INTO alembic_version VALUES ('u0z1a2b3c4d5');

        CREATE TABLE source_cursors (
            id VARCHAR(36) NOT NULL PRIMARY KEY,
            source_id VARCHAR(36) NOT NULL,
            cursor JSON NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
        INSERT INTO source_cursors VALUES (
            'cursor-1', 'source-1', '{"offset": 7}',
            '2026-07-19 00:00:00', '2026-07-19 00:00:00'
        );

        CREATE TABLE collected_records (
            id VARCHAR(36) NOT NULL PRIMARY KEY,
            source_id VARCHAR(36) NOT NULL,
            workflow_id VARCHAR(255),
            workflow_run_id VARCHAR(36)
        );
        INSERT INTO collected_records VALUES (
            'record-1', 'source-1', 'workflow-1', 'run-1'
        );

        CREATE TABLE feed_providers (
            id VARCHAR(36) NOT NULL PRIMARY KEY,
            name VARCHAR(255) NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()

    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS collection_tasks (
            source_id VARCHAR(36) NOT NULL,
            trigger_type VARCHAR(50) NOT NULL,
            parameters JSON NOT NULL,
            priority INTEGER NOT NULL DEFAULT 5,
            status VARCHAR(50) NOT NULL DEFAULT 'pending',
            error_message TEXT,
            id VARCHAR(36) NOT NULL PRIMARY KEY,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()
    _upgrade_to(path, "f3g4h5i6j7k8")
    _add_legacy_edge_node_schema(path)


def _create_stamped_database(path: Path, revision: str) -> None:
    _upgrade_to(path, revision)
    _add_legacy_edge_node_schema(path)


def test_legacy_plugin_database_rejoins_current_migration_head(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy-plugin.db"
    _create_legacy_plugin_database(database_path)
    _upgrade_to(database_path, "head")
    connection = sqlite3.connect(database_path)
    try:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        cursor_columns = {row[1] for row in connection.execute("PRAGMA table_info(source_cursors)")}
        record_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(collected_records)")
        }
        record_indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(collected_records)")
        }
        cursor = connection.execute(
            "SELECT source_id, cursor, version FROM source_cursors WHERE id = 'cursor-1'"
        ).fetchone()
        record = connection.execute(
            "SELECT source_id, workflow_id, workflow_run_id, identity_key "
            "FROM collected_records WHERE id = 'record-1'"
        ).fetchone()
    finally:
        connection.close()

    assert revision == (_current_migration_head(),)
    assert "version" in cursor_columns
    assert "identity_key" in record_columns
    assert "ix_collected_records_source_identity" in record_indexes
    assert cursor == ("source-1", '{"offset": 7}', 0)
    assert record == ("source-1", "workflow-1", "run-1", None)


def test_current_database_repairs_missing_plugin_installation_table(tmp_path: Path) -> None:
    database_path = tmp_path / "drifted-current.db"
    _create_stamped_database(database_path, "f3g4h5i6j7k8")
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE plugin_installations")
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'plugin_installations'"
        ).fetchone() is None
    _upgrade_to(database_path, "head")

    connection = sqlite3.connect(database_path)
    try:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'plugin_installations'"
        ).fetchone()
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(plugin_installations)")}
    finally:
        connection.close()

    assert revision == (_current_migration_head(),)
    assert table == ("plugin_installations",)
    assert "ix_plugin_installations_provider_key" in indexes


def test_current_head_repairs_missing_record_identity_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "drifted-record-identity.db"
    _create_stamped_database(database_path, "h5i6j7k8l9m0")
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP INDEX ix_collected_records_source_identity")
        connection.execute("ALTER TABLE collected_records DROP COLUMN identity_key")
        assert "identity_key" not in {
            row[1] for row in connection.execute("PRAGMA table_info(collected_records)")
        }
        assert "ix_collected_records_source_identity" not in {
            row[1] for row in connection.execute("PRAGMA index_list(collected_records)")
        }
    connection = sqlite3.connect(database_path)
    connection.executescript(
        """
        INSERT INTO data_sources (
            id, name, channel_type, channel_config, enabled, tags,
            created_at, updated_at
        ) VALUES (
            'source-1', 'Legacy source', 'rss', '{}', 1, '[]',
            '2026-07-19 00:00:00', '2026-07-19 00:00:00'
        );
        INSERT INTO collection_tasks (
            id, source_id, trigger_type, parameters, priority, status,
            created_at, updated_at
        ) VALUES (
            'task-1', 'source-1', 'manual', '{}', 5, 'completed',
            '2026-07-19 00:00:00', '2026-07-19 00:00:00'
        );
        INSERT INTO collected_records (
            id, task_id, source_id, raw_data, normalized_data,
            content_hash, status, created_at, updated_at
        ) VALUES (
            'record-1', 'task-1', 'source-1', '{}', '{}',
            'hash-1', 'new', '2026-07-19 00:00:00', '2026-07-19 00:00:00'
        );
        """
    )
    connection.commit()
    connection.close()
    _upgrade_to(database_path, "head")

    connection = sqlite3.connect(database_path)
    try:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        columns = {row[1] for row in connection.execute("PRAGMA table_info(collected_records)")}
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(collected_records)")}
        record = connection.execute(
            "SELECT source_id, identity_key FROM collected_records WHERE id = 'record-1'"
        ).fetchone()
    finally:
        connection.close()

    assert revision == (_current_migration_head(),)
    assert "identity_key" in columns
    assert "ix_collected_records_source_identity" in indexes
    assert record == ("source-1", None)

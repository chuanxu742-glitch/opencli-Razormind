"""add workspace browser-account, lease, command, and profile contracts

Revision ID: add_browser_accounts
Revises: r6s7t8u9v0w1

This migration establishes the durable fencing and metadata structures only.
Runtime scheduling, profile movement, and distributed-lock behavior belong to
later owner slices; SQLite execution is schema/constraint proof, not a
PostgreSQL concurrency claim.
"""

from alembic import op
import sqlalchemy as sa

revision = "add_browser_accounts"
down_revision = "r6s7t8u9v0w1"
branch_labels = None
depends_on = None

_ACTIVE_LEASES = sa.text("status = 'active'")


def upgrade() -> None:
    # These nullable additions preserve existing node and source-binding rows;
    # new account-capable registrations populate them explicitly.
    with op.batch_alter_table("edge_nodes") as batch:
        batch.add_column(sa.Column("boot_id", sa.String(128), nullable=True))
        batch.add_column(sa.Column("credential_id", sa.String(128), nullable=True))
        batch.add_column(sa.Column("capacity_revision", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("account_capable", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("quarantined", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.create_index("ix_edge_nodes_boot_id", ["boot_id"])

    with op.batch_alter_table("source_binding_revisions") as batch:
        batch.add_column(sa.Column("account_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("account_revision", sa.Integer(), nullable=True))
        batch.create_index("ix_source_binding_revisions_account_id", ["account_id"])
        batch.create_check_constraint(
            "ck_source_binding_revisions_account_pin",
            "(account_id IS NULL AND account_revision IS NULL) OR "
            "(account_id IS NOT NULL AND account_revision IS NOT NULL AND account_revision >= 1)",
        )

    op.create_table(
        "browser_accounts",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("site", sa.String(255), nullable=False),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column("node_id", sa.String(36), nullable=True),
        sa.Column("profile_id", sa.String(128), nullable=True),
        sa.Column("profile_version", sa.Integer(), nullable=True),
        sa.Column("profile_manifest_id", sa.String(36), nullable=True),
        sa.Column("runtime_bundle_id", sa.String(36), nullable=True),
        sa.Column("runtime_bundle_version", sa.String(100), nullable=True),
        sa.Column("login_rule_id", sa.String(128), nullable=True),
        sa.Column("login_rule_version", sa.String(64), nullable=True),
        sa.Column("auth_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("platform_identity", sa.JSON(), nullable=True),
        sa.Column("auth_evidence", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("evidence_source", sa.String(30), nullable=True),
        sa.Column("evidence_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("manual_confirmed_by", sa.String(36), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="dormant"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status_reason_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "auth_evidence IN ('unknown', 'valid', 'invalid')",
            name="ck_browser_accounts_auth_evidence",
        ),
        sa.CheckConstraint(
            "evidence_source IN ('rule_verified', 'manual_fallback') OR evidence_source IS NULL",
            name="ck_browser_accounts_evidence_source",
        ),
        sa.CheckConstraint(
            "status IN ('opening', 'presenting', 'refreshing', 'verifying', 'challenge', 'unknown', "
            "'saving', 'saved', 'dormant', 'expired', 'closed', 'error')",
            name="ck_browser_accounts_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["node_id"], ["edge_nodes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["runtime_bundle_id"], ["browser_runtime_bundles.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["manual_confirmed_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_browser_accounts_workspace_id"),
        sa.UniqueConstraint("profile_id", name="uq_browser_accounts_profile_id"),
    )
    op.create_index("ix_browser_accounts_workspace_id", "browser_accounts", ["workspace_id"])
    op.create_index("ix_browser_accounts_node_id", "browser_accounts", ["node_id"])
    op.create_index(
        "ix_browser_accounts_workspace_status_id",
        "browser_accounts",
        ["workspace_id", "status", "id"],
    )

    # The source-binding FK is added after browser_accounts exists, preserving
    # upgrade compatibility with SQLite and the old source-only API.
    with op.batch_alter_table("source_binding_revisions") as batch:
        batch.create_foreign_key(
            "fk_source_binding_revisions_account_id",
            "browser_accounts",
            ["account_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    op.create_table(
        "edge_node_capacities",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("node_id", sa.String(36), nullable=False),
        sa.Column("boot_id", sa.String(128), nullable=False),
        sa.Column("slot_limit", sa.Integer(), nullable=False),
        sa.Column("occupied_slots", sa.Integer(), nullable=False),
        sa.Column("disk_available", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("valid", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("slot_limit >= 0", name="ck_edge_node_capacity_slot_limit"),
        sa.CheckConstraint("occupied_slots >= 0", name="ck_edge_node_capacity_occupied_slots"),
        sa.CheckConstraint("occupied_slots <= slot_limit", name="ck_edge_node_capacity_occupied_lte_limit"),
        sa.CheckConstraint("disk_available >= 0", name="ck_edge_node_capacity_disk_available"),
        sa.ForeignKeyConstraint(["node_id"], ["edge_nodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("node_id", "boot_id", name="uq_edge_node_capacity_generation"),
    )
    op.create_index("ix_edge_node_capacities_node_id", "edge_node_capacities", ["node_id"])
    op.create_index("ix_edge_node_capacities_expires_at", "edge_node_capacities", ["expires_at"])

    op.create_table(
        "edge_node_boots",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("node_id", sa.String(36), nullable=False),
        sa.Column("boot_id", sa.String(128), nullable=False),
        sa.Column("max_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("max_epoch >= 0", name="ck_edge_node_boot_max_epoch"),
        sa.ForeignKeyConstraint(["node_id"], ["edge_nodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("node_id", "boot_id", name="uq_edge_node_boot_generation"),
    )
    op.create_index("ix_edge_node_boots_node_id", "edge_node_boots", ["node_id"])

    op.create_table(
        "browser_login_sessions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("account_id", sa.String(36), nullable=False),
        sa.Column("instance_id", sa.String(36), nullable=True),
        sa.Column("node_id", sa.String(36), nullable=True),
        sa.Column("node_boot_id", sa.String(128), nullable=True),
        sa.Column("lease_id", sa.String(36), nullable=True),
        sa.Column("epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("profile_id", sa.String(128), nullable=True),
        sa.Column("profile_version", sa.Integer(), nullable=True),
        sa.Column("profile_state", sa.String(20), nullable=False, server_default="new"),
        sa.Column("login_rule_id", sa.String(128), nullable=True),
        sa.Column("login_rule_version", sa.String(64), nullable=True),
        sa.Column("tab_id", sa.String(255), nullable=True),
        sa.Column("frame_id", sa.String(255), nullable=True),
        sa.Column("document_id", sa.String(255), nullable=True),
        sa.Column("origin", sa.String(2048), nullable=True),
        sa.Column("view_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("purpose", sa.String(20), nullable=False, server_default="login"),
        sa.Column("execution_id", sa.String(128), nullable=True),
        sa.Column("command_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="opening"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("purpose IN ('login', 'execution')", name="ck_browser_login_sessions_purpose"),
        sa.CheckConstraint(
            "status IN ('opening', 'presenting', 'refreshing', 'verifying', 'challenge', 'unknown', "
            "'saving', 'saved', 'expired', 'closed', 'error')",
            name="ck_browser_login_sessions_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "account_id"], ["browser_accounts.workspace_id", "browser_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["node_id"], ["edge_nodes.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_browser_login_sessions_workspace_id"),
    )
    op.create_index("ix_browser_login_sessions_workspace_id", "browser_login_sessions", ["workspace_id"])
    op.create_index("ix_browser_login_sessions_account_id", "browser_login_sessions", ["account_id"])
    with op.batch_alter_table("browser_spaces") as batch:
        batch.add_column(sa.Column("account_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("session_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("lease_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("epoch", sa.Integer(), nullable=False, server_default="0"))
        batch.create_index("ix_browser_spaces_account_id", ["account_id"])
        batch.create_index("ix_browser_spaces_session_id", ["session_id"])
        batch.create_foreign_key(
            "fk_browser_spaces_account",
            "browser_accounts",
            ["workspace_id", "account_id"],
            ["workspace_id", "id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_browser_spaces_session",
            "browser_login_sessions",
            ["workspace_id", "session_id"],
            ["workspace_id", "id"],
            ondelete="SET NULL",
        )

    op.create_table(
        "browser_account_leases",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("account_id", sa.String(36), nullable=False),
        sa.Column("node_id", sa.String(36), nullable=False),
        sa.Column("node_boot_id", sa.String(128), nullable=False),
        sa.Column("lease_id", sa.String(36), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("renewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("isolation_evidence_ref", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active', 'expired', 'released', 'quarantined')", name="ck_browser_account_leases_status"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "account_id"], ["browser_accounts.workspace_id", "browser_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["node_id"], ["edge_nodes.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lease_id", name="uq_browser_account_leases_lease_id"),
    )
    op.create_index("ix_browser_account_leases_workspace_id", "browser_account_leases", ["workspace_id"])
    op.create_index("ix_browser_account_leases_account_id", "browser_account_leases", ["account_id"])
    op.create_index("ix_browser_account_leases_node_id", "browser_account_leases", ["node_id"])
    op.create_index("ix_browser_account_leases_expires_at", "browser_account_leases", ["expires_at"])
    op.create_index(
        "uq_browser_account_leases_active_account",
        "browser_account_leases",
        ["account_id"],
        unique=True,
        sqlite_where=_ACTIVE_LEASES,
        postgresql_where=_ACTIVE_LEASES,
    )

    op.create_table(
        "browser_durable_commands",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("account_id", sa.String(36), nullable=False),
        sa.Column("node_id", sa.String(36), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("idempotency_scope", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("execution_id", sa.String(128), nullable=True),
        sa.Column("binding_revision_id", sa.String(36), nullable=True),
        sa.Column("epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expected_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("session_id", sa.String(36), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('start_login', 'apply_login_rule', 'refresh_login', 'stop_and_save', "
            "'execute_reference', 'close_session', 'isolate', 'migrate')",
            name="ck_browser_commands_kind",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'claimed', 'running', 'succeeded', 'failed', 'expired', 'cancelled')",
            name="ck_browser_commands_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "account_id"], ["browser_accounts.workspace_id", "browser_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["node_id"], ["edge_nodes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["session_id"], ["browser_login_sessions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "idempotency_scope", "idempotency_key", name="uq_browser_commands_idempotency"),
    )
    op.create_index("ix_browser_durable_commands_workspace_id", "browser_durable_commands", ["workspace_id"])
    op.create_index("ix_browser_durable_commands_account_id", "browser_durable_commands", ["account_id"])
    op.create_index("ix_browser_durable_commands_node_status_available_id", "browser_durable_commands", ["node_id", "status", "available_at", "id"])

    op.create_table(
        "browser_profile_manifests",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("account_id", sa.String(36), nullable=False),
        sa.Column("profile_id", sa.String(128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("node_id", sa.String(36), nullable=False),
        sa.Column("command_id", sa.String(36), nullable=False),
        sa.Column("writer_epoch", sa.Integer(), nullable=False),
        sa.Column("bundle_name", sa.String(100), nullable=False),
        sa.Column("browser_version", sa.String(100), nullable=False),
        sa.Column("files_count", sa.Integer(), nullable=False),
        sa.Column("total_bytes", sa.Integer(), nullable=False),
        sa.Column("checksum_manifest_ref", sa.String(255), nullable=False),
        sa.Column("complete_marker", sa.String(255), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("password_inventory_status", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("state", sa.String(20), nullable=False, server_default="committed"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "password_inventory_status IN ('unknown', 'not_present', 'present', 'blocked', 'verified')",
            name="ck_browser_profile_manifests_password_inventory",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id", "account_id"], ["browser_accounts.workspace_id", "browser_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["node_id"], ["edge_nodes.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["command_id"], ["browser_durable_commands.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("profile_id", "version", name="uq_browser_profile_manifest_version"),
    )
    op.create_index("ix_browser_profile_manifests_workspace_id", "browser_profile_manifests", ["workspace_id"])
    op.create_index("ix_browser_profile_manifests_account_id", "browser_profile_manifests", ["account_id"])


def downgrade() -> None:
    op.drop_index("ix_browser_profile_manifests_account_id", table_name="browser_profile_manifests")
    op.drop_index("ix_browser_profile_manifests_workspace_id", table_name="browser_profile_manifests")
    op.drop_table("browser_profile_manifests")
    op.drop_index("ix_browser_durable_commands_node_status_available_id", table_name="browser_durable_commands")
    op.drop_index("ix_browser_durable_commands_account_id", table_name="browser_durable_commands")
    op.drop_index("ix_browser_durable_commands_workspace_id", table_name="browser_durable_commands")
    op.drop_table("browser_durable_commands")
    op.drop_index("uq_browser_account_leases_active_account", table_name="browser_account_leases")
    op.drop_index("ix_browser_account_leases_expires_at", table_name="browser_account_leases")
    op.drop_index("ix_browser_account_leases_node_id", table_name="browser_account_leases")
    op.drop_index("ix_browser_account_leases_account_id", table_name="browser_account_leases")
    op.drop_index("ix_browser_account_leases_workspace_id", table_name="browser_account_leases")
    op.drop_table("browser_account_leases")
    with op.batch_alter_table("browser_spaces") as batch:
        batch.drop_constraint("fk_browser_spaces_session", type_="foreignkey")
        batch.drop_constraint("fk_browser_spaces_account", type_="foreignkey")
        batch.drop_index("ix_browser_spaces_session_id")
        batch.drop_index("ix_browser_spaces_account_id")
        batch.drop_column("epoch")
        batch.drop_column("lease_id")
        batch.drop_column("session_id")
        batch.drop_column("account_id")
    op.drop_index("ix_browser_login_sessions_account_id", table_name="browser_login_sessions")
    op.drop_index("ix_browser_login_sessions_workspace_id", table_name="browser_login_sessions")
    op.drop_table("browser_login_sessions")
    op.drop_index("ix_edge_node_boots_node_id", table_name="edge_node_boots")
    op.drop_table("edge_node_boots")
    op.drop_index("ix_edge_node_capacities_expires_at", table_name="edge_node_capacities")
    op.drop_index("ix_edge_node_capacities_node_id", table_name="edge_node_capacities")
    op.drop_table("edge_node_capacities")
    with op.batch_alter_table("source_binding_revisions") as batch:
        batch.drop_constraint("fk_source_binding_revisions_account_id", type_="foreignkey")
        batch.drop_constraint("ck_source_binding_revisions_account_pin", type_="check")
        batch.drop_index("ix_source_binding_revisions_account_id")
        batch.drop_column("account_revision")
        batch.drop_column("account_id")
    op.drop_index("ix_browser_accounts_workspace_status_id", table_name="browser_accounts")
    op.drop_index("ix_browser_accounts_node_id", table_name="browser_accounts")
    op.drop_index("ix_browser_accounts_workspace_id", table_name="browser_accounts")
    op.drop_table("browser_accounts")
    with op.batch_alter_table("edge_nodes") as batch:
        batch.drop_index("ix_edge_nodes_boot_id")
        batch.drop_column("quarantined")
        batch.drop_column("account_capable")
        batch.drop_column("capacity_revision")
        batch.drop_column("credential_id")
        batch.drop_column("boot_id")

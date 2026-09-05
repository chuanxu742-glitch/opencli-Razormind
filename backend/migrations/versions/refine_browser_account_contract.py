"""Harden account-contract references with workspace-scoped foreign keys.

The initial account migration introduced account identifiers and retained the
legacy source-only rows.  This follow-up keeps those rows readable while
making every selected account reference carry the same workspace boundary as
the owning binding, command, or profile manifest.
"""

from alembic import op
import sqlalchemy as sa

revision = "refine_browser_account_contract"
down_revision = "add_browser_accounts"
branch_labels = None
depends_on = None


def _backfill_binding_workspaces() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        bind.execute(
            sa.text(
                """
                UPDATE source_binding_revisions
                SET workspace_id = (
                    SELECT projects.workspace_id
                    FROM source_bindings
                    JOIN projects ON projects.id = source_bindings.project_id
                    WHERE source_bindings.id = source_binding_revisions.source_binding_id
                )
                WHERE workspace_id IS NULL
                """
            )
        )
        return

    bind.execute(
        sa.text(
            """
            UPDATE source_binding_revisions AS revisions
            SET workspace_id = projects.workspace_id
            FROM source_bindings
            JOIN projects ON projects.id = source_bindings.project_id
            WHERE revisions.source_binding_id = source_bindings.id
              AND revisions.workspace_id IS NULL
            """
        )
    )


def upgrade() -> None:
    # Keep existing source-only revisions valid.  E1 populates account_id and
    # the already-derived workspace_id when selecting an account.
    with op.batch_alter_table("browser_login_sessions") as batch:
        batch.add_column(sa.Column("revision", sa.Integer(), nullable=False, server_default="0"))

    # The profile-manifest composite FK must target an explicit unique key,
    # not merely the command's single-column primary key.
    with op.batch_alter_table("browser_durable_commands") as batch:
        batch.create_unique_constraint(
            "uq_browser_commands_workspace_id", ["workspace_id", "id"]
        )

    with op.batch_alter_table("source_binding_revisions") as batch:
        batch.add_column(sa.Column("workspace_id", sa.String(36), nullable=True))
        batch.create_index("ix_source_binding_revisions_workspace_id", ["workspace_id"])

    _backfill_binding_workspaces()

    with op.batch_alter_table("source_binding_revisions") as batch:
        batch.create_foreign_key(
            "fk_source_binding_revisions_workspace",
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_foreign_key(
            "fk_source_binding_revision_account_workspace",
            "browser_accounts",
            ["workspace_id", "account_id"],
            ["workspace_id", "id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_source_binding_revision_account_workspace_pair",
            "account_id IS NULL OR workspace_id IS NOT NULL",
        )
    with op.batch_alter_table("browser_durable_commands") as batch:
        # Durable history keeps its workspace; referenced sessions are retained
        # rather than attempting a composite SET NULL on a non-null workspace.
        batch.create_foreign_key(
            "fk_browser_commands_session_workspace",
            "browser_login_sessions",
            ["workspace_id", "session_id"],
            ["workspace_id", "id"],
            ondelete="RESTRICT",
        )

    with op.batch_alter_table("browser_profile_manifests") as batch:
        batch.create_foreign_key(
            "fk_browser_profile_manifest_command_workspace",
            "browser_durable_commands",
            ["workspace_id", "command_id"],
            ["workspace_id", "id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("browser_profile_manifests") as batch:
        batch.drop_constraint(
            "fk_browser_profile_manifest_command_workspace", type_="foreignkey"
        )

    with op.batch_alter_table("browser_durable_commands") as batch:
        batch.drop_constraint("fk_browser_commands_session_workspace", type_="foreignkey")
        batch.drop_constraint("uq_browser_commands_workspace_id", type_="unique")

    with op.batch_alter_table("browser_login_sessions") as batch:
        batch.drop_column("revision")
    with op.batch_alter_table("source_binding_revisions") as batch:
        batch.drop_constraint(
            "ck_source_binding_revision_account_workspace_pair", type_="check"
        )
        batch.drop_constraint(
            "fk_source_binding_revision_account_workspace", type_="foreignkey"
        )
        batch.drop_constraint("fk_source_binding_revisions_workspace", type_="foreignkey")
        batch.drop_index("ix_source_binding_revisions_workspace_id")
        batch.drop_column("workspace_id")

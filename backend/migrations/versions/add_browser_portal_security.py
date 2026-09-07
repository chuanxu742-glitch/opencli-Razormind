"""Persist digest-only portal tickets and short-lived owner credentials."""

from alembic import op
import sqlalchemy as sa

revision = "add_browser_portal_security"
down_revision = "refine_browser_account_contract"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "browser_portal_tickets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ticket_id", sa.String(length=36), nullable=False),
        sa.Column("ticket_digest", sa.String(length=64), nullable=False),
        sa.Column("csrf_digest", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("session_revision", sa.Integer(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hard_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("session_revision >= 0", name="ck_browser_portal_tickets_session_revision"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["browser_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["browser_login_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ticket_id", name="uq_browser_portal_tickets_ticket_id"),
    )
    op.create_index(
        "ix_browser_portal_tickets_workspace_id", "browser_portal_tickets", ["workspace_id"]
    )
    op.create_index(
        "ix_browser_portal_tickets_account_id", "browser_portal_tickets", ["account_id"]
    )
    op.create_index(
        "ix_browser_portal_tickets_session_id", "browser_portal_tickets", ["session_id"]
    )
    op.create_index(
        "ix_browser_portal_tickets_scope",
        "browser_portal_tickets",
        ["workspace_id", "account_id", "session_id"],
    )

    op.create_table(
        "browser_portal_owners",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_digest", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("ticket_id", sa.String(length=36), nullable=False),
        sa.Column("session_revision", sa.Integer(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hard_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.CheckConstraint("session_revision >= 0", name="ck_browser_portal_owners_session_revision"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["browser_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["browser_login_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_digest", name="uq_browser_portal_owners_owner_digest"),
    )
    op.create_index(
        "ix_browser_portal_owners_workspace_id", "browser_portal_owners", ["workspace_id"]
    )
    op.create_index(
        "ix_browser_portal_owners_account_id", "browser_portal_owners", ["account_id"]
    )
    op.create_index(
        "ix_browser_portal_owners_session_id", "browser_portal_owners", ["session_id"]
    )
    op.create_index(
        "ix_browser_portal_owners_ticket_id", "browser_portal_owners", ["ticket_id"]
    )
    op.create_index(
        "ix_browser_portal_owners_scope",
        "browser_portal_owners",
        ["workspace_id", "account_id", "session_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_browser_portal_owners_scope", table_name="browser_portal_owners")
    op.drop_index("ix_browser_portal_owners_ticket_id", table_name="browser_portal_owners")
    op.drop_index("ix_browser_portal_owners_session_id", table_name="browser_portal_owners")
    op.drop_index("ix_browser_portal_owners_account_id", table_name="browser_portal_owners")
    op.drop_index("ix_browser_portal_owners_workspace_id", table_name="browser_portal_owners")
    op.drop_table("browser_portal_owners")
    op.drop_index("ix_browser_portal_tickets_scope", table_name="browser_portal_tickets")
    op.drop_index("ix_browser_portal_tickets_session_id", table_name="browser_portal_tickets")
    op.drop_index("ix_browser_portal_tickets_account_id", table_name="browser_portal_tickets")
    op.drop_index("ix_browser_portal_tickets_workspace_id", table_name="browser_portal_tickets")
    op.drop_table("browser_portal_tickets")

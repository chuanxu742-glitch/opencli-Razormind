"""Allow durable manually controlled browser sessions.

Revision ID: add_browser_session_purpose
Revises: add_geo_answer_observations
"""

from alembic import op

revision = "add_browser_session_purpose"
down_revision = "add_geo_answer_observations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite and PostgreSQL both require replacement of a CHECK constraint.
    # batch_alter_table preserves every existing row while rebuilding SQLite.
    with op.batch_alter_table("browser_login_sessions") as batch:
        batch.drop_constraint("ck_browser_login_sessions_purpose", type_="check")
        batch.create_check_constraint(
            "ck_browser_login_sessions_purpose",
            "purpose IN ('login', 'execution', 'browser')",
        )


def downgrade() -> None:
    with op.batch_alter_table("browser_login_sessions") as batch:
        batch.drop_constraint("ck_browser_login_sessions_purpose", type_="check")
        batch.create_check_constraint(
            "ck_browser_login_sessions_purpose",
            "purpose IN ('login', 'execution')",
        )

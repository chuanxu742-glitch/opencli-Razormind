"""Persist the authenticated actor for account-bound task execution."""

import sqlalchemy as sa
from alembic import op

revision = "add_task_execution_actor"
down_revision = "finalize_browser_account_cutover"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("collection_tasks") as batch:
        batch.add_column(sa.Column("requested_by_user_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_collection_tasks_requested_by_user_id_users",
            "users",
            ["requested_by_user_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("collection_tasks") as batch:
        batch.drop_constraint(
            "fk_collection_tasks_requested_by_user_id_users",
            type_="foreignkey",
        )
        batch.drop_column("requested_by_user_id")

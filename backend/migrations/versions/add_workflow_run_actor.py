"""Persist the authenticated actor for account-bound workflow execution."""

import sqlalchemy as sa
from alembic import op

revision = "add_workflow_run_actor"
down_revision = "add_task_execution_actor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("workflow_runs") as batch:
        batch.add_column(sa.Column("requested_by_user_id", sa.String(36), nullable=True))
        batch.create_index(
            "ix_workflow_runs_requested_by_user_id",
            ["requested_by_user_id"],
            unique=False,
        )
        batch.create_foreign_key(
            "fk_workflow_runs_requested_by_user_id_users",
            "users",
            ["requested_by_user_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("workflow_runs") as batch:
        batch.drop_constraint(
            "fk_workflow_runs_requested_by_user_id_users",
            type_="foreignkey",
        )
        batch.drop_index("ix_workflow_runs_requested_by_user_id")
        batch.drop_column("requested_by_user_id")

"""Add append-only GEO answer observations.

Revision ID: add_geo_answer_observations
Revises: add_workflow_run_actor
"""

import sqlalchemy as sa
from alembic import op

revision = "add_geo_answer_observations"
down_revision = "add_workflow_run_actor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "geo_answer_observations",
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("task_run_id", sa.String(length=36), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("raw_data", sa.JSON(), nullable=False),
        sa.Column("normalized_data", sa.JSON(), nullable=False),
        sa.Column("lineage", sa.JSON(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["collection_tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_run_id"], ["task_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_run_id", "source_id", "content_hash",
            name="uq_geo_observation_run_source_content",
        ),
    )
    op.create_index(
        "ix_geo_observations_source_observed_at",
        "geo_answer_observations",
        ["source_id", "observed_at"],
        unique=False,
    )
    op.create_index(
        "ix_geo_observations_task_id",
        "geo_answer_observations",
        ["task_id"],
        unique=False,
    )
    op.create_index(
        "ix_geo_answer_observations_task_run_id",
        "geo_answer_observations",
        ["task_run_id"],
        unique=False,
    )
    op.create_index(
        "ix_geo_answer_observations_source_id",
        "geo_answer_observations",
        ["source_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_geo_answer_observations_source_id", table_name="geo_answer_observations")
    op.drop_index("ix_geo_answer_observations_task_run_id", table_name="geo_answer_observations")
    op.drop_index("ix_geo_observations_task_id", table_name="geo_answer_observations")
    op.drop_index(
        "ix_geo_observations_source_observed_at", table_name="geo_answer_observations"
    )
    op.drop_table("geo_answer_observations")

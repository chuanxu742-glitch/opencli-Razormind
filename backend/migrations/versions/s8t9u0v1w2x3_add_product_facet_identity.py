"""Enforce one current product facet per source without changing other identities.

Revision ID: s8t9u0v1w2x3
Revises: r6s7t8u9v0w1
Create Date: 2026-09-06
"""

import sqlalchemy as sa
from alembic import op

revision = "s8t9u0v1w2x3"
down_revision = "r6s7t8u9v0w1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_collected_records_product_identity",
        "collected_records",
        ["source_id", "identity_key"],
        unique=True,
        sqlite_where=sa.text("json_extract(normalized_data, '$.ecommerce.entity_id') IS NOT NULL"),
        postgresql_where=sa.text("(normalized_data -> 'ecommerce' ->> 'entity_id') IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_collected_records_product_identity", table_name="collected_records")

"""Retire global legacy browser-binding identity.

Legacy bindings remain preserved for migration diagnostics, but account identity is
workspace-scoped and no longer has a global site uniqueness rule.
"""

from alembic import op

revision = "finalize_browser_account_cutover"
down_revision = "add_browser_portal_security"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("browser_bindings") as batch:
        batch.drop_constraint("uq_browser_bindings_site", type_="unique")


def downgrade() -> None:
    with op.batch_alter_table("browser_bindings") as batch:
        batch.create_unique_constraint("uq_browser_bindings_site", ["site"])

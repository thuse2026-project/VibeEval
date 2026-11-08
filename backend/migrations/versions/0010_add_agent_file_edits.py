"""distinguish durable file edits from command operations

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "agent_operations",
        sa.Column("kind", sa.String(16), nullable=False, server_default="command"),
    )
    op.create_check_constraint(
        op.f("ck_agent_operations_kind_valid"),
        "agent_operations",
        "kind IN ('command','file_edit')",
    )
    op.alter_column("agent_operations", "kind", server_default=None)


def downgrade():
    # Old workers cannot recover an edit. Require cancellation/reconciliation
    # before downgrade rather than silently dropping an active edit's kind.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM agent_operations WHERE kind='file_edit' AND result_json IS NULL) THEN RAISE EXCEPTION 'Resolve file edits before downgrade'; END IF; END $$"
    )
    op.drop_constraint(
        op.f("ck_agent_operations_kind_valid"), "agent_operations", type_="check"
    )
    op.drop_column("agent_operations", "kind")

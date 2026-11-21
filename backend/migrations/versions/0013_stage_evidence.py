"""Append-only process evidence scoped to a coding phase."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "stage_workspaces", sa.Column("evidence_status", sa.String(16), nullable=True)
    )
    op.create_table(
        "stage_evidence",
        sa.Column(
            "phase_id",
            sa.String(128),
            sa.ForeignKey("agent_scopes.scope_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("sequence", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("event_id", sa.String(128), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.UniqueConstraint("phase_id", "event_id"),
    )


def downgrade():
    op.drop_table("stage_evidence")
    op.drop_column("stage_workspaces", "evidence_status")

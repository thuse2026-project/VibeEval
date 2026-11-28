"""Persist visible terminal transcripts and confirmed four-stage publications."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0016_r2_publication"
down_revision = "0015_examiner_rooms"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "stage_terminals",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "phase_id",
            sa.String(128),
            sa.ForeignKey("agent_scopes.scope_id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("opened_at", sa.BigInteger, nullable=False),
        sa.Column("transcript", sa.Text, nullable=False),
        sa.Column("truncated", sa.Boolean, nullable=False),
    )
    op.create_table(
        "exam_grade_publications",
        sa.Column(
            "session_id",
            sa.String(128),
            sa.ForeignKey("exam_sessions.session_id"),
            primary_key=True,
        ),
        sa.Column("record", JSONB, nullable=False),
    )


def downgrade():
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM exam_grade_publications) OR EXISTS (SELECT 1 FROM stage_terminals)"
            )
        )
        .scalar()
    ):
        raise RuntimeError("Export transcripts and publications before downgrade")
    op.drop_table("exam_grade_publications")
    op.drop_table("stage_terminals")

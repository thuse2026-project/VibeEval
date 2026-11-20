"""Durable question-independent multi-stage examination aggregates."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "exam_sessions",
        sa.Column("exam_mode", sa.String(16), nullable=False, server_default="single"),
    )
    op.create_check_constraint(
        op.f("ck_exam_sessions_known_exam_mode"),
        "exam_sessions",
        "exam_mode IN ('single', 'multi_stage')",
    )
    op.create_table(
        "exam_flow_attempts",
        sa.Column("session_id", sa.String(128), nullable=False),
        sa.Column("attempt_id", sa.String(128), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("wake_at", sa.BigInteger(), nullable=True),
        sa.Column("state_json", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("session_id", name=op.f("pk_exam_flow_attempts")),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["exam_sessions.session_id"],
            ondelete="CASCADE",
            name=op.f("fk_exam_flow_attempts_session_id_exam_sessions"),
        ),
        sa.UniqueConstraint(
            "attempt_id", name=op.f("uq_exam_flow_attempts_attempt_id")
        ),
        sa.CheckConstraint(
            "revision >= 0", name=op.f("ck_exam_flow_attempts_revision_nonnegative")
        ),
        sa.CheckConstraint(
            "wake_at IS NULL OR wake_at >= 0",
            name=op.f("ck_exam_flow_attempts_wake_nonnegative"),
        ),
        sa.CheckConstraint(
            "state_json->>'session_id' = session_id AND "
            "state_json->>'id' = attempt_id AND "
            "(state_json->>'revision')::bigint = revision",
            name=op.f("ck_exam_flow_attempts_state_identity"),
        ),
    )
    op.create_index("ix_exam_flow_attempts_wake_at", "exam_flow_attempts", ["wake_at"])


def downgrade():
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM exam_flow_attempts) THEN RAISE EXCEPTION 'Archive multi-stage attempts before downgrade'; END IF; END $$"
    )
    op.drop_index("ix_exam_flow_attempts_wake_at", table_name="exam_flow_attempts")
    op.drop_table("exam_flow_attempts")
    op.drop_constraint(
        op.f("ck_exam_sessions_known_exam_mode"), "exam_sessions", type_="check"
    )
    op.drop_column("exam_sessions", "exam_mode")

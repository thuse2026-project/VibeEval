"""create invitations and exam sessions

Revision ID: 0001
Revises: None
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "invitations",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("candidate_id", sa.String(length=120), nullable=False),
        sa.Column("candidate_name", sa.String(length=120), nullable=False),
        sa.Column("exam_id", sa.String(length=120), nullable=False),
        sa.Column("exam_title", sa.String(length=120), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_invitations_token_hash_format"),
        ),
        sa.CheckConstraint(
            "duration_minutes BETWEEN 1 AND 1440",
            name=op.f("ck_invitations_duration_range"),
        ),
        sa.CheckConstraint(
            "expires_at >= 0", name=op.f("ck_invitations_expires_nonnegative")
        ),
        sa.CheckConstraint(
            "length(btrim(candidate_id)) > 0 AND length(btrim(candidate_name)) > 0 "
            "AND length(btrim(exam_id)) > 0 AND length(btrim(exam_title)) > 0",
            name=op.f("ck_invitations_identity_not_blank"),
        ),
        sa.PrimaryKeyConstraint("token_hash", name=op.f("pk_invitations")),
        sa.UniqueConstraint(
            "exam_id", "candidate_id", name="uq_invitations_exam_candidate"
        ),
    )
    op.create_table(
        "exam_sessions",
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("invitation_hash", sa.String(length=64), nullable=False),
        sa.Column("browser_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'admitted'"),
            nullable=False,
        ),
        sa.Column("admitted_at", sa.BigInteger(), nullable=False),
        sa.Column("started_at", sa.BigInteger(), nullable=True),
        sa.Column("deadline_at", sa.BigInteger(), nullable=True),
        sa.Column("submitted_at", sa.BigInteger(), nullable=True),
        sa.Column("sandbox_id", sa.String(length=255), nullable=True),
        sa.CheckConstraint(
            "browser_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_exam_sessions_browser_hash_format"),
        ),
        sa.CheckConstraint(
            "(started_at IS NULL AND deadline_at IS NULL) OR "
            "(started_at IS NOT NULL AND deadline_at IS NOT NULL "
            "AND started_at >= admitted_at AND deadline_at > started_at)",
            name=op.f("ck_exam_sessions_start_deadline_order"),
        ),
        sa.CheckConstraint(
            "admitted_at >= 0", name=op.f("ck_exam_sessions_admitted_nonnegative")
        ),
        sa.CheckConstraint(
            "length(btrim(session_id)) > 0",
            name=op.f("ck_exam_sessions_session_id_not_blank"),
        ),
        sa.CheckConstraint(
            "length(btrim(status)) > 0", name=op.f("ck_exam_sessions_status_not_blank")
        ),
        sa.CheckConstraint(
            "submitted_at IS NULL OR "
            "(started_at IS NOT NULL AND submitted_at >= started_at)",
            name=op.f("ck_exam_sessions_submission_after_start"),
        ),
        sa.ForeignKeyConstraint(
            ["invitation_hash"],
            ["invitations.token_hash"],
            name=op.f("fk_exam_sessions_invitation_hash_invitations"),
        ),
        sa.PrimaryKeyConstraint("session_id", name=op.f("pk_exam_sessions")),
        sa.UniqueConstraint("browser_hash", name=op.f("uq_exam_sessions_browser_hash")),
        sa.UniqueConstraint(
            "invitation_hash", name=op.f("uq_exam_sessions_invitation_hash")
        ),
    )
    op.create_index(
        "ix_exam_sessions_status_deadline",
        "exam_sessions",
        ["status", "deadline_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_exam_sessions_status_deadline", table_name="exam_sessions")
    op.drop_table("exam_sessions")
    op.drop_table("invitations")

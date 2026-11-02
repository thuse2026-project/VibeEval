"""add immutable snapshots and final submissions

Revision ID: 0002
Revises: '0001'
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "snapshots",
        sa.Column("snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_snapshots_digest_format")
        ),
        sa.CheckConstraint(
            "created_at >= 0", name=op.f("ck_snapshots_created_nonnegative")
        ),
        sa.CheckConstraint(
            "file_count >= 0", name=op.f("ck_snapshots_file_count_nonnegative")
        ),
        sa.CheckConstraint("size_bytes > 0", name=op.f("ck_snapshots_size_positive")),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["exam_sessions.session_id"],
            name=op.f("fk_snapshots_session_id_exam_sessions"),
        ),
        sa.PrimaryKeyConstraint("snapshot_id", name=op.f("pk_snapshots")),
        sa.UniqueConstraint(
            "snapshot_id", "session_id", name="uq_snapshots_id_session"
        ),
    )
    op.create_index(
        op.f("ix_snapshots_content_sha256"),
        "snapshots",
        ["content_sha256"],
        unique=False,
    )
    op.create_table(
        "submissions",
        sa.Column("submission_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("candidate_id", sa.String(length=120), nullable=False),
        sa.Column("candidate_name", sa.String(length=120), nullable=False),
        sa.Column("exam_id", sa.String(length=120), nullable=False),
        sa.Column("exam_title", sa.String(length=120), nullable=False),
        sa.Column("admitted_at", sa.BigInteger(), nullable=False),
        sa.Column("started_at", sa.BigInteger(), nullable=False),
        sa.Column("submitted_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "idempotency_key ~ '^[A-Za-z0-9_-]{1,128}$'",
            name=op.f("ck_submissions_idempotency_format"),
        ),
        sa.CheckConstraint(
            "admitted_at >= 0 AND started_at >= admitted_at AND submitted_at >= started_at",
            name=op.f("ck_submissions_timeline_order"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["exam_sessions.session_id"],
            name=op.f("fk_submissions_session_id_exam_sessions"),
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id", "session_id"],
            ["snapshots.snapshot_id", "snapshots.session_id"],
            name=op.f("fk_submissions_snapshot_id_snapshots"),
        ),
        sa.PrimaryKeyConstraint("submission_id", name=op.f("pk_submissions")),
        sa.UniqueConstraint(
            "exam_id", "candidate_id", name="uq_submissions_exam_candidate"
        ),
        sa.UniqueConstraint("session_id", name=op.f("uq_submissions_session_id")),
    )
    op.create_index(
        op.f("ix_submissions_submitted_at"),
        "submissions",
        ["submitted_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_submissions_submitted_at"), table_name="submissions")
    op.drop_table("submissions")
    op.drop_index(op.f("ix_snapshots_content_sha256"), table_name="snapshots")
    op.drop_table("snapshots")

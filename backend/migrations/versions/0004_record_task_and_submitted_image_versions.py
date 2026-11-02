"""record task and submitted image versions

Revision ID: 0004
Revises: '0003'
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "exam_sessions",
        sa.Column("task_version_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "submissions",
        sa.Column("task_version_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "submissions",
        sa.Column("sandbox_image_ref", sa.String(length=512), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_exam_sessions_task_version_not_blank"),
        "exam_sessions",
        "task_version_id IS NULL OR length(btrim(task_version_id)) > 0",
    )
    op.create_check_constraint(
        op.f("ck_submissions_task_version_not_blank"),
        "submissions",
        "task_version_id IS NULL OR length(btrim(task_version_id)) > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_submissions_task_version_not_blank"), "submissions", type_="check"
    )
    op.drop_constraint(
        op.f("ck_exam_sessions_task_version_not_blank"), "exam_sessions", type_="check"
    )
    op.drop_column("submissions", "sandbox_image_ref")
    op.drop_column("submissions", "task_version_id")
    op.drop_column("exam_sessions", "task_version_id")

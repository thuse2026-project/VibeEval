"""add sandbox lifecycle bindings

Revision ID: 0003
Revises: '0002'
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "exam_sessions",
        sa.Column("sandbox_resource_key", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "exam_sessions",
        sa.Column("sandbox_image_ref", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "exam_sessions",
        sa.Column("sandbox_backend_ref", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "exam_sessions", sa.Column("sandbox_deleted_at", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "exam_sessions",
        sa.Column("lifecycle_error_code", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "exam_sessions", sa.Column("lifecycle_error_at", sa.BigInteger(), nullable=True)
    )
    op.create_unique_constraint(
        op.f("uq_exam_sessions_sandbox_resource_key"),
        "exam_sessions",
        ["sandbox_resource_key"],
    )
    op.create_check_constraint(
        op.f("ck_exam_sessions_known_status"),
        "exam_sessions",
        "status IN ('admitted', 'provisioning', 'ready', 'in_progress', "
        "'provision_failed', 'submitting', 'submission_failed', 'submitted')",
    )
    op.create_check_constraint(
        op.f("ck_exam_sessions_sandbox_plan_complete"),
        "exam_sessions",
        "(sandbox_resource_key IS NULL AND sandbox_image_ref IS NULL AND sandbox_backend_ref IS NULL) OR "
        "(sandbox_resource_key IS NOT NULL AND sandbox_image_ref IS NOT NULL AND sandbox_backend_ref IS NOT NULL "
        "AND length(btrim(sandbox_resource_key)) > 0 AND length(btrim(sandbox_image_ref)) > 0 "
        "AND length(btrim(sandbox_backend_ref)) > 0)",
    )
    op.create_check_constraint(
        op.f("ck_exam_sessions_cleanup_after_submission"),
        "exam_sessions",
        "sandbox_deleted_at IS NULL OR (sandbox_id IS NOT NULL AND submitted_at IS NOT NULL "
        "AND sandbox_deleted_at >= submitted_at)",
    )
    op.create_check_constraint(
        op.f("ck_exam_sessions_lifecycle_error_complete"),
        "exam_sessions",
        "(lifecycle_error_code IS NULL AND lifecycle_error_at IS NULL) OR "
        "(lifecycle_error_code IS NOT NULL AND lifecycle_error_at IS NOT NULL AND lifecycle_error_at >= 0)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_exam_sessions_lifecycle_error_complete"),
        "exam_sessions",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_exam_sessions_cleanup_after_submission"),
        "exam_sessions",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_exam_sessions_sandbox_plan_complete"), "exam_sessions", type_="check"
    )
    op.drop_constraint(
        op.f("ck_exam_sessions_known_status"), "exam_sessions", type_="check"
    )
    op.drop_constraint(
        op.f("uq_exam_sessions_sandbox_resource_key"), "exam_sessions", type_="unique"
    )
    op.drop_column("exam_sessions", "lifecycle_error_at")
    op.drop_column("exam_sessions", "lifecycle_error_code")
    op.drop_column("exam_sessions", "sandbox_deleted_at")
    op.drop_column("exam_sessions", "sandbox_backend_ref")
    op.drop_column("exam_sessions", "sandbox_image_ref")
    op.drop_column("exam_sessions", "sandbox_resource_key")

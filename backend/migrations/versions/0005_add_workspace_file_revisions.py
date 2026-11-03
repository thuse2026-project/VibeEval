"""add persistent workspace file revisions

Revision ID: 0005
Revises: '0004'
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_file_revisions",
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("path", sa.String(length=500), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "length(path) > 0", name=op.f("ck_workspace_file_revisions_path_not_empty")
        ),
        sa.CheckConstraint(
            "version > 0", name=op.f("ck_workspace_file_revisions_version_positive")
        ),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_workspace_file_revisions_digest_format"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["exam_sessions.session_id"],
            name=op.f("fk_workspace_file_revisions_session_id_exam_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "session_id", "path", name=op.f("pk_workspace_file_revisions")
        ),
    )


def downgrade() -> None:
    op.drop_table("workspace_file_revisions")

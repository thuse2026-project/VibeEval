"""Persist the initial grading task for a sealed R2 submission."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0014_stage_grading_jobs"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "stage_grading_jobs",
        sa.Column("submission_id", sa.String(128), primary_key=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("basis", JSONB, nullable=False),
        sa.Column("components", JSONB, nullable=False),
    )


def downgrade():
    op.drop_table("stage_grading_jobs")

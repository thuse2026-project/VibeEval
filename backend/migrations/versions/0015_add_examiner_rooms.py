"""Persist examiner rooms and irreversible review decisions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0015_examiner_rooms"
down_revision = "0014_stage_grading_jobs"
branch_labels = None
depends_on = None


def upgrade():
    # The separate room pilot used 0013 for its two room tables. Preserve them
    # and replay only the missing evidence DDL when upgrading that lineage.
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    rooms = {"examiner_rooms", "examiner_room_reviews"}
    if existing & rooms:
        if not rooms <= existing:
            raise RuntimeError("Incomplete examiner room schema")
        if "stage_evidence" not in existing:
            from pathlib import Path

            from alembic.util import load_python_file

            load_python_file(Path(__file__).parent, "0013_stage_evidence.py").upgrade()
        return
    op.create_table(
        "examiner_rooms",
        sa.Column("room_id", sa.String(120), primary_key=True),
        sa.Column(
            "invitation_hash",
            sa.String(64),
            sa.ForeignKey("invitations.token_hash"),
            nullable=False,
            unique=True,
        ),
        sa.Column("creator", sa.String(64), nullable=False),
        sa.Column("request_key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("definition", JSONB(), nullable=False),
        sa.UniqueConstraint("creator", "request_key"),
    )
    op.create_table(
        "examiner_room_reviews",
        sa.Column("submission_id", sa.String(128), primary_key=True),
        sa.Column(
            "room_id",
            sa.String(120),
            sa.ForeignKey("examiner_rooms.room_id"),
            nullable=False,
        ),
        sa.Column("decision", JSONB(), nullable=False),
    )


def downgrade():
    connection = op.get_bind()
    if connection.execute(
        sa.text("SELECT EXISTS (SELECT 1 FROM examiner_rooms)")
    ).scalar():
        raise RuntimeError(
            "Export examiner rooms and review decisions before downgrading"
        )
    op.drop_table("examiner_room_reviews")
    op.drop_table("examiner_rooms")

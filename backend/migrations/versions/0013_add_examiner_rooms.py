"""Persist examiner rooms and irreversible review decisions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade():
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

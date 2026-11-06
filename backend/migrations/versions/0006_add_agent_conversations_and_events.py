"""add scoped agent conversation snapshots and transactional events

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_conversations",
        sa.Column("conversation_id", sa.String(128), nullable=False),
        sa.Column("exam_session_id", sa.String(128), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("event_sequence", sa.BigInteger(), nullable=False),
        sa.Column("state_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "revision >= 0 AND event_sequence >= 1",
            name=op.f("ck_agent_conversations_counters_valid"),
        ),
        sa.CheckConstraint(
            "length(btrim(title)) > 0",
            name=op.f("ck_agent_conversations_title_not_blank"),
        ),
        sa.CheckConstraint(
            "created_at >= 0 AND updated_at >= created_at",
            name=op.f("ck_agent_conversations_timeline_order"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(state_json) = 'object' AND coalesce(state_json->>'id', '') = conversation_id AND coalesce(state_json->>'scope_id', '') = exam_session_id AND coalesce(state_json->>'revision', '') = revision::text",
            name=op.f("ck_agent_conversations_state_identity"),
        ),
        sa.ForeignKeyConstraint(
            ["exam_session_id"],
            ["exam_sessions.session_id"],
            ondelete="CASCADE",
            name=op.f("fk_agent_conversations_exam_session_id_exam_sessions"),
        ),
        sa.PrimaryKeyConstraint("conversation_id", name=op.f("pk_agent_conversations")),
    )
    op.create_index(
        "ix_agent_conversations_exam_created",
        "agent_conversations",
        ["exam_session_id", "created_at", "conversation_id"],
    )
    op.create_table(
        "agent_events",
        sa.Column("conversation_id", sa.String(128), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(80), nullable=False),
        sa.Column("data_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "sequence >= 1 AND created_at >= 0",
            name=op.f("ck_agent_events_counters_valid"),
        ),
        sa.CheckConstraint(
            "length(btrim(kind)) > 0", name=op.f("ck_agent_events_kind_not_blank")
        ),
        sa.CheckConstraint(
            "jsonb_typeof(data_json) = 'object'",
            name=op.f("ck_agent_events_data_object"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["agent_conversations.conversation_id"],
            ondelete="CASCADE",
            name=op.f("fk_agent_events_conversation_id_agent_conversations"),
        ),
        sa.PrimaryKeyConstraint(
            "conversation_id", "sequence", name=op.f("pk_agent_events")
        ),
    )


def downgrade():
    op.drop_table("agent_events")
    op.drop_index(
        "ix_agent_conversations_exam_created", table_name="agent_conversations"
    )
    op.drop_table("agent_conversations")

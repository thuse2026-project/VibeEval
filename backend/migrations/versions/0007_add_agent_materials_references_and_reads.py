"""add immutable materials, user references and actual tool-read observations

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint(
        "uq_agent_conversations_identity_scope",
        "agent_conversations",
        ["conversation_id", "exam_session_id"],
    )
    op.create_table(
        "agent_materials",
        sa.Column("material_id", sa.String(128), nullable=False),
        sa.Column("exam_session_id", sa.String(128), nullable=False),
        sa.Column("material_key", sa.String(64), nullable=False),
        sa.Column("source_kind", sa.String(40), nullable=False),
        sa.Column("source_id", sa.String(512), nullable=False),
        sa.Column("source_version", sa.String(200), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("material_id", name=op.f("pk_agent_materials")),
        sa.ForeignKeyConstraint(
            ["exam_session_id"],
            ["exam_sessions.session_id"],
            name=op.f("fk_agent_materials_exam_session_id_exam_sessions"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "exam_session_id", "material_key", name="uq_agent_materials_scope_key"
        ),
        sa.UniqueConstraint(
            "material_id", "exam_session_id", name="uq_agent_materials_identity_scope"
        ),
        sa.CheckConstraint(
            "material_key ~ '^[0-9a-f]{64}$' AND content_sha256 ~ '^[0-9a-f]{64}$' AND source_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_agent_materials_digests_valid"),
        ),
        sa.CheckConstraint(
            "size_bytes = octet_length(content_text) AND size_bytes <= 8388608 AND created_at >= 0",
            name=op.f("ck_agent_materials_size_timeline_valid"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(metadata_json) = 'object'",
            name=op.f("ck_agent_materials_metadata_object"),
        ),
    )
    op.create_table(
        "agent_input_references",
        sa.Column("reference_id", sa.String(128), nullable=False),
        sa.Column("conversation_id", sa.String(128), nullable=False),
        sa.Column("exam_session_id", sa.String(128), nullable=False),
        sa.Column("material_id", sa.String(128), nullable=True),
        sa.Column("reference_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("reference_id", name=op.f("pk_agent_input_references")),
        sa.ForeignKeyConstraint(
            ["conversation_id", "exam_session_id"],
            [
                "agent_conversations.conversation_id",
                "agent_conversations.exam_session_id",
            ],
            name=op.f("fk_agent_input_references_conversation_id_agent_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["material_id", "exam_session_id"],
            ["agent_materials.material_id", "agent_materials.exam_session_id"],
            name=op.f("fk_agent_input_references_material_id_agent_materials"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(reference_json) = 'object' AND created_at >= 0",
            name=op.f("ck_agent_input_references_data_timeline_valid"),
        ),
    )
    op.create_index(
        "ix_agent_input_references_conversation",
        "agent_input_references",
        ["conversation_id", "created_at", "reference_id"],
    )
    op.create_table(
        "agent_read_observations",
        sa.Column("observation_id", sa.String(128), nullable=False),
        sa.Column("conversation_id", sa.String(128), nullable=False),
        sa.Column("exam_session_id", sa.String(128), nullable=False),
        sa.Column("turn_id", sa.String(128), nullable=False),
        sa.Column("step_id", sa.String(128), nullable=False),
        sa.Column("tool_call_id", sa.String(128), nullable=False),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("call_sha256", sa.String(64), nullable=False),
        sa.Column("action_kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("material_id", sa.String(128), nullable=True),
        sa.Column("result_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "observation_id", name=op.f("pk_agent_read_observations")
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "exam_session_id"],
            [
                "agent_conversations.conversation_id",
                "agent_conversations.exam_session_id",
            ],
            name=op.f("fk_agent_read_observations_conversation_id_agent_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["material_id", "exam_session_id"],
            ["agent_materials.material_id", "agent_materials.exam_session_id"],
            name=op.f("fk_agent_read_observations_material_id_agent_materials"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "conversation_id",
            "step_id",
            "tool_call_id",
            name="uq_agent_read_observations_call",
        ),
        sa.CheckConstraint(
            "call_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_agent_read_observations_digest_valid"),
        ),
        sa.CheckConstraint(
            "action_kind IN ('query', 'read') AND status IN ('succeeded', 'error', 'denied')",
            name=op.f("ck_agent_read_observations_known_status"),
        ),
        sa.CheckConstraint(
            "(status = 'succeeded' AND material_id IS NOT NULL) OR (status != 'succeeded' AND material_id IS NULL)",
            name=op.f("ck_agent_read_observations_material_status"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(result_json) = 'object' AND created_at >= 0",
            name=op.f("ck_agent_read_observations_data_timeline_valid"),
        ),
    )
    op.create_index(
        "ix_agent_read_observations_conversation",
        "agent_read_observations",
        ["conversation_id", "created_at", "observation_id"],
    )


def downgrade():
    op.drop_index(
        "ix_agent_read_observations_conversation", table_name="agent_read_observations"
    )
    op.drop_table("agent_read_observations")
    op.drop_index(
        "ix_agent_input_references_conversation", table_name="agent_input_references"
    )
    op.drop_table("agent_input_references")
    op.drop_table("agent_materials")
    op.drop_constraint(
        "uq_agent_conversations_identity_scope", "agent_conversations", type_="unique"
    )

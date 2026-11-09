"""add durable agent runs, provider attempts and bound interactions

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def identity_columns():
    return [
        sa.Column("conversation_id", sa.String(128), nullable=False),
        sa.Column("exam_session_id", sa.String(128), nullable=False),
    ]


def run_fk(table):
    return sa.ForeignKeyConstraint(
        ["run_id", "conversation_id", "exam_session_id"],
        [
            "agent_runs.run_id",
            "agent_runs.conversation_id",
            "agent_runs.exam_session_id",
        ],
        name=op.f("fk_" + table + "_run_id_agent_runs"),
        ondelete="CASCADE",
    )


def upgrade():
    op.create_table(
        "agent_runs",
        sa.Column("run_id", sa.String(128), nullable=False),
        *identity_columns(),
        sa.Column("turn_id", sa.String(128)),
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("input_json", JSONB(), nullable=False),
        sa.Column("policy_json", JSONB(), nullable=False),
        sa.Column("tools_sha256", sa.String(64), nullable=False),
        *[
            sa.Column(name, sa.Integer(), nullable=False)
            for name in (
                "model_calls",
                "compaction_calls",
                "tool_calls",
                "overflow_retries",
            )
        ],
        sa.Column("compact_requested", sa.String(16)),
        sa.Column("lease_token", sa.String(128)),
        sa.Column("lease_expires_at", sa.BigInteger()),
        sa.Column("error_code", sa.String(80)),
        *[
            sa.Column(name, sa.BigInteger(), nullable=False)
            for name in ("created_at", "updated_at", "deadline_at")
        ],
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_agent_runs")),
        sa.ForeignKeyConstraint(
            ["conversation_id", "exam_session_id"],
            [
                "agent_conversations.conversation_id",
                "agent_conversations.exam_session_id",
            ],
            name=op.f("fk_agent_runs_conversation_id_agent_conversations"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "conversation_id", "request_key", name="uq_agent_runs_request"
        ),
        sa.UniqueConstraint(
            "run_id",
            "conversation_id",
            "exam_session_id",
            name="uq_agent_runs_identity_scope",
        ),
        sa.CheckConstraint(
            "kind IN ('turn', 'compact')", name=op.f("ck_agent_runs_kind_valid")
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','waiting_approval','waiting_user_input','completed','failed','interrupted','unknown','capacity_exceeded','budget_exhausted','expired')",
            name=op.f("ck_agent_runs_status_valid"),
        ),
        sa.CheckConstraint(
            "model_calls >= 0 AND compaction_calls >= 0 AND tool_calls >= 0 AND overflow_retries BETWEEN 0 AND 1",
            name=op.f("ck_agent_runs_counters_valid"),
        ),
        sa.CheckConstraint(
            "created_at >= 0 AND updated_at >= created_at AND deadline_at > created_at",
            name=op.f("ck_agent_runs_timeline_valid"),
        ),
        sa.CheckConstraint(
            "(lease_token IS NULL) = (lease_expires_at IS NULL)",
            name=op.f("ck_agent_runs_lease_complete"),
        ),
    )
    op.create_index(
        "uq_agent_runs_active_conversation",
        "agent_runs",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('queued','running','waiting_approval','waiting_user_input')"
        ),
    )
    op.create_table(
        "agent_attempts",
        sa.Column("attempt_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        *identity_columns(),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("step_id", sa.String(128)),
        sa.Column("source_revision", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("request_json", JSONB(), nullable=False),
        sa.Column("manifest_json", JSONB()),
        sa.Column("response_json", JSONB()),
        sa.Column("usage_json", JSONB()),
        sa.Column("error_code", sa.String(80)),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("sent_at", sa.BigInteger()),
        sa.Column("finished_at", sa.BigInteger()),
        sa.PrimaryKeyConstraint("attempt_id", name=op.f("pk_agent_attempts")),
        run_fk("agent_attempts"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_agent_attempts_sequence"),
        sa.CheckConstraint(
            "kind IN ('model', 'compaction')", name=op.f("ck_agent_attempts_kind_valid")
        ),
        sa.CheckConstraint(
            "status IN ('prepared','running','adopted','rejected','failed','unknown','late')",
            name=op.f("ck_agent_attempts_status_valid"),
        ),
        sa.CheckConstraint(
            "sequence >= 1 AND source_revision >= 0 AND created_at >= 0",
            name=op.f("ck_agent_attempts_counters_valid"),
        ),
    )
    op.create_index(
        "uq_agent_attempts_pending_run",
        "agent_attempts",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('prepared','running')"),
    )
    op.create_table(
        "agent_interactions",
        sa.Column("interaction_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        *identity_columns(),
        sa.Column("step_id", sa.String(128), nullable=False),
        sa.Column("tool_call_id", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("call_sha256", sa.String(64), nullable=False),
        sa.Column("binding_json", JSONB(), nullable=False),
        sa.Column("request_json", JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("answer_json", JSONB()),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("finished_at", sa.BigInteger()),
        sa.PrimaryKeyConstraint("interaction_id", name=op.f("pk_agent_interactions")),
        run_fk("agent_interactions"),
        sa.UniqueConstraint(
            "run_id", "step_id", "tool_call_id", name="uq_agent_interactions_call"
        ),
        sa.CheckConstraint(
            "kind IN ('approval','user_input')",
            name=op.f("ck_agent_interactions_kind_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','answered','expired','cancelled')",
            name=op.f("ck_agent_interactions_status_valid"),
        ),
        sa.CheckConstraint(
            "call_sha256 ~ '^[0-9a-f]{64}$' AND expires_at > created_at",
            name=op.f("ck_agent_interactions_binding_valid"),
        ),
    )


def downgrade():
    op.drop_table("agent_interactions")
    op.drop_index("uq_agent_attempts_pending_run", table_name="agent_attempts")
    op.drop_table("agent_attempts")
    op.drop_index("uq_agent_runs_active_conversation", table_name="agent_runs")
    op.drop_table("agent_runs")

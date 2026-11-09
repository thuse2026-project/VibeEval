"""add fenced sandbox command operations and operation waiting state

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

OLD = "status IN ('queued','running','waiting_approval','waiting_user_input','completed','failed','interrupted','unknown','capacity_exceeded','budget_exhausted','expired')"
NEW = OLD.replace("'waiting_user_input',", "'waiting_user_input','waiting_operation',")
ACTIVE = "status IN ('queued','running','waiting_approval','waiting_user_input')"


def upgrade():
    op.drop_constraint(op.f("ck_agent_runs_status_valid"), "agent_runs", type_="check")
    op.create_check_constraint(op.f("ck_agent_runs_status_valid"), "agent_runs", NEW)
    op.drop_index("uq_agent_runs_active_conversation", table_name="agent_runs")
    op.create_index(
        "uq_agent_runs_active_conversation",
        "agent_runs",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text(
            ACTIVE.replace(
                "'waiting_user_input'", "'waiting_user_input','waiting_operation'"
            )
        ),
    )
    op.create_table(
        "agent_operations",
        *[
            sa.Column(name, sa.String(128), nullable=False)
            for name in (
                "operation_id",
                "run_id",
                "conversation_id",
                "exam_session_id",
                "turn_id",
                "step_id",
                "tool_call_id",
            )
        ],
        sa.Column("call_sha256", sa.String(64), nullable=False),
        sa.Column("binding_sha256", sa.String(64), nullable=False),
        *[
            sa.Column(name, JSONB(), nullable=False)
            for name in ("args_json", "binding_json", "config_json")
        ],
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("policy_decision", sa.String(16)),
        sa.Column("reason_code", sa.String(80)),
        sa.Column("handle", sa.String(128)),
        sa.Column("receipt_json", JSONB()),
        sa.Column("result_json", JSONB()),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("holds_workspace", sa.Boolean(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("operation_id", name=op.f("pk_agent_operations")),
        sa.ForeignKeyConstraint(
            ["run_id", "conversation_id", "exam_session_id"],
            [
                "agent_runs.run_id",
                "agent_runs.conversation_id",
                "agent_runs.exam_session_id",
            ],
            name=op.f("fk_agent_operations_run_id_agent_runs"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "run_id", "step_id", "tool_call_id", name="uq_agent_operations_call"
        ),
        sa.CheckConstraint(
            "status IN ('requested','waiting_approval','preparing','prepared','dispatching','running','succeeded','failed','conflict','denied','cancelled','timed_out','unknown')",
            name=op.f("ck_agent_operations_status_valid"),
        ),
        sa.CheckConstraint(
            "revision >= 0 AND created_at >= 0 AND updated_at >= created_at",
            name=op.f("ck_agent_operations_timeline_valid"),
        ),
        sa.CheckConstraint(
            "call_sha256 ~ '^[0-9a-f]{64}$' AND binding_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_agent_operations_digests_valid"),
        ),
    )
    op.create_index(
        "uq_agent_operations_workspace_holder",
        "agent_operations",
        ["exam_session_id"],
        unique=True,
        postgresql_where=sa.text("holds_workspace"),
    )


def downgrade():
    op.drop_index("uq_agent_operations_workspace_holder", table_name="agent_operations")
    op.drop_table("agent_operations")
    op.execute(
        "UPDATE agent_runs SET status='interrupted', lease_token=NULL, lease_expires_at=NULL WHERE status='waiting_operation'"
    )
    op.drop_index("uq_agent_runs_active_conversation", table_name="agent_runs")
    op.create_index(
        "uq_agent_runs_active_conversation",
        "agent_runs",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text(ACTIVE),
    )
    op.drop_constraint(op.f("ck_agent_runs_status_valid"), "agent_runs", type_="check")
    op.create_check_constraint(op.f("ck_agent_runs_status_valid"), "agent_runs", OLD)

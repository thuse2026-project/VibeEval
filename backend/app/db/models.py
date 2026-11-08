"""Shared entry/session records; timestamps are UTC Unix seconds."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "pk": "pk_%(table_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "ix": "ix_%(table_name)s_%(column_0_name)s",
        }
    )


class Invitation(Base):
    __tablename__ = "invitations"
    __table_args__ = (
        UniqueConstraint(
            "exam_id", "candidate_id", name="uq_invitations_exam_candidate"
        ),
        CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="token_hash_format"),
        CheckConstraint("duration_minutes BETWEEN 1 AND 1440", name="duration_range"),
        CheckConstraint("expires_at >= 0", name="expires_nonnegative"),
        CheckConstraint(
            "length(btrim(candidate_id)) > 0 AND length(btrim(candidate_name)) > 0 "
            "AND length(btrim(exam_id)) > 0 AND length(btrim(exam_title)) > 0",
            name="identity_not_blank",
        ),
    )

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(String(120))
    candidate_name: Mapped[str] = mapped_column(String(120))
    exam_id: Mapped[str] = mapped_column(String(120))
    exam_title: Mapped[str] = mapped_column(String(120))
    duration_minutes: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[int] = mapped_column(BigInteger)
    session: Mapped[ExamSession | None] = relationship(back_populates="invitation")


class ExamSession(Base):
    __tablename__ = "exam_sessions"
    __table_args__ = (
        CheckConstraint("length(btrim(session_id)) > 0", name="session_id_not_blank"),
        CheckConstraint("browser_hash ~ '^[0-9a-f]{64}$'", name="browser_hash_format"),
        CheckConstraint("length(btrim(status)) > 0", name="status_not_blank"),
        CheckConstraint(
            "task_version_id IS NULL OR length(btrim(task_version_id)) > 0",
            name="task_version_not_blank",
        ),
        CheckConstraint(
            "status IN ('admitted', 'provisioning', 'ready', 'in_progress', "
            "'provision_failed', 'submitting', 'submission_failed', 'submitted')",
            name="known_status",
        ),
        CheckConstraint(
            "(sandbox_resource_key IS NULL AND sandbox_image_ref IS NULL AND sandbox_backend_ref IS NULL) OR "
            "(sandbox_resource_key IS NOT NULL AND sandbox_image_ref IS NOT NULL AND sandbox_backend_ref IS NOT NULL "
            "AND length(btrim(sandbox_resource_key)) > 0 AND length(btrim(sandbox_image_ref)) > 0 "
            "AND length(btrim(sandbox_backend_ref)) > 0)",
            name="sandbox_plan_complete",
        ),
        CheckConstraint(
            "sandbox_deleted_at IS NULL OR (sandbox_id IS NOT NULL AND submitted_at IS NOT NULL "
            "AND sandbox_deleted_at >= submitted_at)",
            name="cleanup_after_submission",
        ),
        CheckConstraint(
            "(lifecycle_error_code IS NULL AND lifecycle_error_at IS NULL) OR "
            "(lifecycle_error_code IS NOT NULL AND lifecycle_error_at IS NOT NULL AND lifecycle_error_at >= 0)",
            name="lifecycle_error_complete",
        ),
        CheckConstraint("admitted_at >= 0", name="admitted_nonnegative"),
        CheckConstraint(
            "(started_at IS NULL AND deadline_at IS NULL) OR "
            "(started_at IS NOT NULL AND deadline_at IS NOT NULL "
            "AND started_at >= admitted_at AND deadline_at > started_at)",
            name="start_deadline_order",
        ),
        CheckConstraint(
            "submitted_at IS NULL OR "
            "(started_at IS NOT NULL AND submitted_at >= started_at)",
            name="submission_after_start",
        ),
        Index("ix_exam_sessions_status_deadline", "status", "deadline_at"),
    )

    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    invitation_hash: Mapped[str] = mapped_column(
        ForeignKey("invitations.token_hash"), unique=True
    )
    browser_hash: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(32), server_default=text("'admitted'"))
    admitted_at: Mapped[int] = mapped_column(BigInteger)
    started_at: Mapped[int | None] = mapped_column(BigInteger)
    deadline_at: Mapped[int | None] = mapped_column(BigInteger)
    submitted_at: Mapped[int | None] = mapped_column(BigInteger)
    sandbox_id: Mapped[str | None] = mapped_column(String(255))
    task_version_id: Mapped[str | None] = mapped_column(String(128))
    sandbox_resource_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    sandbox_image_ref: Mapped[str | None] = mapped_column(String(512))
    sandbox_backend_ref: Mapped[str | None] = mapped_column(String(128))
    sandbox_deleted_at: Mapped[int | None] = mapped_column(BigInteger)
    lifecycle_error_code: Mapped[str | None] = mapped_column(String(64))
    lifecycle_error_at: Mapped[int | None] = mapped_column(BigInteger)
    invitation: Mapped[Invitation] = relationship(back_populates="session")


class WorkspaceFileRevision(Base):
    __tablename__ = "workspace_file_revisions"
    __table_args__ = (
        CheckConstraint("length(path) > 0", name="path_not_empty"),
        CheckConstraint("version > 0", name="version_positive"),
        CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="digest_format"),
    )

    session_id: Mapped[str] = mapped_column(
        ForeignKey("exam_sessions.session_id", ondelete="CASCADE"), primary_key=True
    )
    path: Mapped[str] = mapped_column(String(500), primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger)
    content_sha256: Mapped[str] = mapped_column(String(64))


class AgentConversation(Base):
    __tablename__ = "agent_conversations"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "exam_session_id",
            name="uq_agent_conversations_identity_scope",
        ),
        CheckConstraint("revision >= 0 AND event_sequence >= 1", name="counters_valid"),
        CheckConstraint("length(btrim(title)) > 0", name="title_not_blank"),
        CheckConstraint(
            "created_at >= 0 AND updated_at >= created_at", name="timeline_order"
        ),
        CheckConstraint(
            "jsonb_typeof(state_json) = 'object' AND "
            "coalesce(state_json->>'id', '') = conversation_id AND "
            "coalesce(state_json->>'scope_id', '') = exam_session_id AND "
            "coalesce(state_json->>'revision', '') = revision::text",
            name="state_identity",
        ),
        Index(
            "ix_agent_conversations_exam_created",
            "exam_session_id",
            "created_at",
            "conversation_id",
        ),
    )

    conversation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    exam_session_id: Mapped[str] = mapped_column(
        ForeignKey("exam_sessions.session_id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(200))
    revision: Mapped[int] = mapped_column(BigInteger)
    event_sequence: Mapped[int] = mapped_column(BigInteger)
    state_json: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)


class AgentEvent(Base):
    __tablename__ = "agent_events"
    __table_args__ = (
        CheckConstraint("sequence >= 1 AND created_at >= 0", name="counters_valid"),
        CheckConstraint("length(btrim(kind)) > 0", name="kind_not_blank"),
        CheckConstraint("jsonb_typeof(data_json) = 'object'", name="data_object"),
    )

    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("agent_conversations.conversation_id", ondelete="CASCADE"),
        primary_key=True,
    )
    sequence: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(80))
    data_json: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[int] = mapped_column(BigInteger)


class AgentMaterial(Base):
    __tablename__ = "agent_materials"
    __table_args__ = (
        UniqueConstraint(
            "exam_session_id", "material_key", name="uq_agent_materials_scope_key"
        ),
        UniqueConstraint(
            "material_id", "exam_session_id", name="uq_agent_materials_identity_scope"
        ),
        CheckConstraint(
            "material_key ~ '^[0-9a-f]{64}$' AND content_sha256 ~ '^[0-9a-f]{64}$' AND source_sha256 ~ '^[0-9a-f]{64}$'",
            name="digests_valid",
        ),
        CheckConstraint(
            "size_bytes = octet_length(content_text) AND size_bytes <= 8388608 AND created_at >= 0",
            name="size_timeline_valid",
        ),
        CheckConstraint(
            "jsonb_typeof(metadata_json) = 'object'", name="metadata_object"
        ),
    )

    material_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    exam_session_id: Mapped[str] = mapped_column(
        ForeignKey("exam_sessions.session_id", ondelete="CASCADE")
    )
    material_key: Mapped[str] = mapped_column(String(64))
    source_kind: Mapped[str] = mapped_column(String(40))
    source_id: Mapped[str] = mapped_column(String(512))
    source_version: Mapped[str] = mapped_column(String(200))
    source_sha256: Mapped[str] = mapped_column(String(64))
    content_sha256: Mapped[str] = mapped_column(String(64))
    content_text: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    metadata_json: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[int] = mapped_column(BigInteger)


class AgentInputReference(Base):
    __tablename__ = "agent_input_references"
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "exam_session_id"],
            [
                "agent_conversations.conversation_id",
                "agent_conversations.exam_session_id",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["material_id", "exam_session_id"],
            ["agent_materials.material_id", "agent_materials.exam_session_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "jsonb_typeof(reference_json) = 'object' AND created_at >= 0",
            name="data_timeline_valid",
        ),
        Index(
            "ix_agent_input_references_conversation",
            "conversation_id",
            "created_at",
            "reference_id",
        ),
    )

    reference_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(128))
    exam_session_id: Mapped[str] = mapped_column(String(128))
    material_id: Mapped[str | None] = mapped_column(String(128))
    reference_json: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[int] = mapped_column(BigInteger)


class AgentReadObservation(Base):
    __tablename__ = "agent_read_observations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "exam_session_id"],
            [
                "agent_conversations.conversation_id",
                "agent_conversations.exam_session_id",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["material_id", "exam_session_id"],
            ["agent_materials.material_id", "agent_materials.exam_session_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "conversation_id",
            "step_id",
            "tool_call_id",
            name="uq_agent_read_observations_call",
        ),
        CheckConstraint("call_sha256 ~ '^[0-9a-f]{64}$'", name="digest_valid"),
        CheckConstraint(
            "action_kind IN ('query', 'read') AND status IN ('succeeded', 'error', 'denied')",
            name="known_status",
        ),
        CheckConstraint(
            "(status = 'succeeded' AND material_id IS NOT NULL) OR (status != 'succeeded' AND material_id IS NULL)",
            name="material_status",
        ),
        CheckConstraint(
            "jsonb_typeof(result_json) = 'object' AND created_at >= 0",
            name="data_timeline_valid",
        ),
        Index(
            "ix_agent_read_observations_conversation",
            "conversation_id",
            "created_at",
            "observation_id",
        ),
    )

    observation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(128))
    exam_session_id: Mapped[str] = mapped_column(String(128))
    turn_id: Mapped[str] = mapped_column(String(128))
    step_id: Mapped[str] = mapped_column(String(128))
    tool_call_id: Mapped[str] = mapped_column(String(128))
    tool_name: Mapped[str] = mapped_column(String(64))
    call_sha256: Mapped[str] = mapped_column(String(64))
    action_kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    material_id: Mapped[str | None] = mapped_column(String(128))
    result_json: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[int] = mapped_column(BigInteger)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "exam_session_id"],
            [
                "agent_conversations.conversation_id",
                "agent_conversations.exam_session_id",
            ],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "conversation_id", "request_key", name="uq_agent_runs_request"
        ),
        UniqueConstraint(
            "run_id",
            "conversation_id",
            "exam_session_id",
            name="uq_agent_runs_identity_scope",
        ),
        CheckConstraint("kind IN ('turn', 'compact')", name="kind_valid"),
        CheckConstraint(
            "status IN ('queued','running','waiting_approval','waiting_user_input','waiting_operation','completed','failed','interrupted','unknown','capacity_exceeded','budget_exhausted','expired')",
            name="status_valid",
        ),
        CheckConstraint(
            "model_calls >= 0 AND compaction_calls >= 0 AND tool_calls >= 0 AND overflow_retries BETWEEN 0 AND 1",
            name="counters_valid",
        ),
        CheckConstraint(
            "created_at >= 0 AND updated_at >= created_at AND deadline_at > created_at",
            name="timeline_valid",
        ),
        CheckConstraint(
            "(lease_token IS NULL) = (lease_expires_at IS NULL)", name="lease_complete"
        ),
        Index(
            "uq_agent_runs_active_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text(
                "status IN ('queued','running','waiting_approval','waiting_user_input','waiting_operation')"
            ),
        ),
    )

    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(128))
    exam_session_id: Mapped[str] = mapped_column(String(128))
    turn_id: Mapped[str | None] = mapped_column(String(128))
    request_key: Mapped[str] = mapped_column(String(128))
    input_sha256: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32))
    input_json: Mapped[dict] = mapped_column(JSONB)
    policy_json: Mapped[dict] = mapped_column(JSONB)
    tools_sha256: Mapped[str] = mapped_column(String(64))
    model_calls: Mapped[int] = mapped_column(Integer, default=0)
    compaction_calls: Mapped[int] = mapped_column(Integer, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    overflow_retries: Mapped[int] = mapped_column(Integer, default=0)
    compact_requested: Mapped[str | None] = mapped_column(String(16))
    lease_token: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[int | None] = mapped_column(BigInteger)
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)
    deadline_at: Mapped[int] = mapped_column(BigInteger)


class AgentAttempt(Base):
    __tablename__ = "agent_attempts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "conversation_id", "exam_session_id"],
            [
                "agent_runs.run_id",
                "agent_runs.conversation_id",
                "agent_runs.exam_session_id",
            ],
            ondelete="CASCADE",
        ),
        UniqueConstraint("run_id", "sequence", name="uq_agent_attempts_sequence"),
        CheckConstraint("kind IN ('model', 'compaction')", name="kind_valid"),
        CheckConstraint(
            "status IN ('prepared','running','adopted','rejected','failed','unknown','late')",
            name="status_valid",
        ),
        CheckConstraint(
            "sequence >= 1 AND source_revision >= 0 AND created_at >= 0",
            name="counters_valid",
        ),
        Index(
            "uq_agent_attempts_pending_run",
            "run_id",
            unique=True,
            postgresql_where=text("status IN ('prepared','running')"),
        ),
    )

    attempt_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(128))
    conversation_id: Mapped[str] = mapped_column(String(128))
    exam_session_id: Mapped[str] = mapped_column(String(128))
    sequence: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    trigger: Mapped[str] = mapped_column(String(16))
    step_id: Mapped[str | None] = mapped_column(String(128))
    source_revision: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16))
    request_json: Mapped[dict] = mapped_column(JSONB)
    manifest_json: Mapped[dict | None] = mapped_column(JSONB)
    response_json: Mapped[dict | None] = mapped_column(JSONB)
    usage_json: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[int] = mapped_column(BigInteger)
    sent_at: Mapped[int | None] = mapped_column(BigInteger)
    finished_at: Mapped[int | None] = mapped_column(BigInteger)


class AgentInteraction(Base):
    __tablename__ = "agent_interactions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "conversation_id", "exam_session_id"],
            [
                "agent_runs.run_id",
                "agent_runs.conversation_id",
                "agent_runs.exam_session_id",
            ],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "run_id", "step_id", "tool_call_id", name="uq_agent_interactions_call"
        ),
        CheckConstraint("kind IN ('approval','user_input')", name="kind_valid"),
        CheckConstraint(
            "status IN ('pending','approved','rejected','answered','expired','cancelled')",
            name="status_valid",
        ),
        CheckConstraint(
            "call_sha256 ~ '^[0-9a-f]{64}$' AND expires_at > created_at",
            name="binding_valid",
        ),
    )

    interaction_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(128))
    conversation_id: Mapped[str] = mapped_column(String(128))
    exam_session_id: Mapped[str] = mapped_column(String(128))
    step_id: Mapped[str] = mapped_column(String(128))
    tool_call_id: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))
    call_sha256: Mapped[str] = mapped_column(String(64))
    binding_json: Mapped[dict] = mapped_column(JSONB)
    request_json: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16))
    answer_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[int] = mapped_column(BigInteger)
    finished_at: Mapped[int | None] = mapped_column(BigInteger)


class AgentOperation(Base):
    __tablename__ = "agent_operations"
    __table_args__ = (
        CheckConstraint("kind IN ('command','file_edit')", name="kind_valid"),
        ForeignKeyConstraint(
            ["run_id", "conversation_id", "exam_session_id"],
            [
                "agent_runs.run_id",
                "agent_runs.conversation_id",
                "agent_runs.exam_session_id",
            ],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "run_id", "step_id", "tool_call_id", name="uq_agent_operations_call"
        ),
        CheckConstraint(
            "status IN ('requested','waiting_approval','preparing','prepared','dispatching','running','succeeded','failed','conflict','denied','cancelled','timed_out','unknown')",
            name="status_valid",
        ),
        CheckConstraint(
            "revision >= 0 AND created_at >= 0 AND updated_at >= created_at",
            name="timeline_valid",
        ),
        CheckConstraint(
            "call_sha256 ~ '^[0-9a-f]{64}$' AND binding_sha256 ~ '^[0-9a-f]{64}$'",
            name="digests_valid",
        ),
        Index(
            "uq_agent_operations_workspace_holder",
            "exam_session_id",
            unique=True,
            postgresql_where=text("holds_workspace"),
        ),
    )

    operation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    run_id: Mapped[str] = mapped_column(String(128))
    conversation_id: Mapped[str] = mapped_column(String(128))
    exam_session_id: Mapped[str] = mapped_column(String(128))
    turn_id: Mapped[str] = mapped_column(String(128))
    step_id: Mapped[str] = mapped_column(String(128))
    tool_call_id: Mapped[str] = mapped_column(String(128))
    call_sha256: Mapped[str] = mapped_column(String(64))
    args_json: Mapped[dict] = mapped_column(JSONB)
    binding_json: Mapped[dict] = mapped_column(JSONB)
    binding_sha256: Mapped[str] = mapped_column(String(64))
    config_json: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(32))
    policy_decision: Mapped[str | None] = mapped_column(String(16))
    reason_code: Mapped[str | None] = mapped_column(String(80))
    handle: Mapped[str | None] = mapped_column(String(128))
    receipt_json: Mapped[dict | None] = mapped_column(JSONB)
    result_json: Mapped[dict | None] = mapped_column(JSONB)
    revision: Mapped[int] = mapped_column(Integer)
    holds_workspace: Mapped[bool] = mapped_column(Boolean)
    cancel_requested: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[int] = mapped_column(BigInteger)


class Snapshot(Base):
    __tablename__ = "snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "session_id", name="uq_snapshots_id_session"),
        CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="digest_format"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
        CheckConstraint("file_count >= 0", name="file_count_nonnegative"),
        CheckConstraint("created_at >= 0", name="created_nonnegative"),
    )

    snapshot_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("exam_sessions.session_id"))
    content_sha256: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    file_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[int] = mapped_column(BigInteger)


class Submission(Base):
    __tablename__ = "submissions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["snapshot_id", "session_id"],
            ["snapshots.snapshot_id", "snapshots.session_id"],
        ),
        UniqueConstraint(
            "exam_id", "candidate_id", name="uq_submissions_exam_candidate"
        ),
        CheckConstraint(
            "idempotency_key ~ '^[A-Za-z0-9_-]{1,128}$'", name="idempotency_format"
        ),
        CheckConstraint(
            "admitted_at >= 0 AND started_at >= admitted_at AND submitted_at >= started_at",
            name="timeline_order",
        ),
        CheckConstraint(
            "task_version_id IS NULL OR length(btrim(task_version_id)) > 0",
            name="task_version_not_blank",
        ),
    )

    submission_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("exam_sessions.session_id"), unique=True
    )
    snapshot_id: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    candidate_id: Mapped[str] = mapped_column(String(120))
    candidate_name: Mapped[str] = mapped_column(String(120))
    exam_id: Mapped[str] = mapped_column(String(120))
    exam_title: Mapped[str] = mapped_column(String(120))
    admitted_at: Mapped[int] = mapped_column(BigInteger)
    started_at: Mapped[int] = mapped_column(BigInteger)
    submitted_at: Mapped[int] = mapped_column(BigInteger, index=True)
    task_version_id: Mapped[str | None] = mapped_column(String(128))
    sandbox_image_ref: Mapped[str | None] = mapped_column(String(512))

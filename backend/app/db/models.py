"""Shared entry/session records; timestamps are UTC Unix seconds."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    UniqueConstraint,
    text,
)
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

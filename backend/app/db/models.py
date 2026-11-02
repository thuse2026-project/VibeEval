"""Shared entry/session records; timestamps are UTC Unix seconds."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
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
    invitation: Mapped[Invitation] = relationship(back_populates="session")

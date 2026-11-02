"""Persist already-frozen data. The caller must stop sandbox writers first."""

import re
import time
import uuid
from collections.abc import Callable, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import ExamSession, Invitation, Snapshot, Submission
from app.db.session import get_session_factory, session_scope

from .storage import SnapshotFile, SnapshotStore


class SubmissionNotFound(LookupError):
    pass


class InvalidSubmissionState(ValueError):
    pass


def validate_idempotency_key(idempotency_key: str) -> None:
    if (
        not isinstance(idempotency_key, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", idempotency_key) is None
    ):
        raise ValueError(
            "Idempotency key must contain 1 to 128 letters, digits, underscores or hyphens"
        )


class SubmissionService:
    def __init__(
        self,
        store: SnapshotStore | None = None,
        factory: sessionmaker[Session] | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.store = store if store is not None else SnapshotStore()
        self.factory = factory if factory is not None else get_session_factory()
        self.clock = clock

    @staticmethod
    def _receipt(record: Submission) -> dict:
        return {
            "submission_id": record.submission_id,
            "session_id": record.session_id,
            "snapshot_id": record.snapshot_id,
            "submitted_at": record.submitted_at,
            "status": "frozen",
        }

    @staticmethod
    def _summary(record: Submission) -> dict:
        return {
            field: getattr(record, field)
            for field in (
                "submission_id",
                "session_id",
                "candidate_id",
                "candidate_name",
                "exam_id",
                "exam_title",
                "submitted_at",
                "admitted_at",
                "started_at",
            )
        }

    def persist_frozen_submission(
        self,
        session_id: str,
        idempotency_key: str,
        files: Iterable[SnapshotFile],
        *,
        public_test_output: str | None = None,
    ) -> dict:
        """The session must be submitting and its workspace must already be frozen.

        A replay always returns the first committed submission, even with a new key.
        No sandbox, authentication or HTTP operation is performed by this method.
        """
        with session_scope(self.factory) as session:
            return self.persist_in_transaction(
                session,
                session_id,
                idempotency_key,
                files,
                public_test_output=public_test_output,
            )

    def persist_in_transaction(
        self,
        session: Session,
        session_id: str,
        idempotency_key: str,
        files: Iterable[SnapshotFile],
        *,
        public_test_output: str | None = None,
    ) -> dict:
        """Join a lifecycle transaction; its owner must commit before returning a receipt."""
        if not session.in_transaction():
            raise RuntimeError("An active caller-owned transaction is required")
        validate_idempotency_key(idempotency_key)
        exam = session.scalar(
            select(ExamSession)
            .where(ExamSession.session_id == session_id)
            .with_for_update()
        )
        if exam is None:
            raise SubmissionNotFound("Exam session was not found")
        existing = session.scalar(
            select(Submission).where(Submission.session_id == session_id)
        )
        if existing is not None:
            return self._receipt(existing)
        if exam.status != "submitting" or exam.started_at is None:
            raise InvalidSubmissionState(
                "Session must be started and submitting before persistence"
            )
        submitted_at = int(self.clock())
        if submitted_at < exam.started_at:
            raise InvalidSubmissionState("Submission time cannot precede exam start")
        identity = session.get(Invitation, exam.invitation_hash)
        stored = self.store.write(files, public_test_output)
        snapshot = Snapshot(
            snapshot_id="snp_" + uuid.uuid4().hex,
            session_id=session_id,
            content_sha256=stored.content_sha256,
            size_bytes=stored.size_bytes,
            file_count=stored.file_count,
            created_at=submitted_at,
        )
        session.add(snapshot)
        session.flush()
        record = Submission(
            submission_id="sub_" + uuid.uuid4().hex,
            session_id=session_id,
            snapshot_id=snapshot.snapshot_id,
            idempotency_key=idempotency_key,
            candidate_id=identity.candidate_id,
            candidate_name=identity.candidate_name,
            exam_id=identity.exam_id,
            exam_title=identity.exam_title,
            admitted_at=exam.admitted_at,
            started_at=exam.started_at,
            submitted_at=submitted_at,
            task_version_id=exam.task_version_id,
            sandbox_image_ref=exam.sandbox_image_ref,
        )
        session.add(record)
        exam.status = "submitted"
        exam.submitted_at = submitted_at
        exam.lifecycle_error_code = None
        exam.lifecycle_error_at = None
        session.flush()
        return self._receipt(record)

    def get_session_receipt(self, session_id: str) -> dict | None:
        with session_scope(self.factory) as session:
            record = session.scalar(
                select(Submission).where(Submission.session_id == session_id)
            )
            return self._receipt(record) if record is not None else None

    def list_submissions(self) -> list[dict]:
        with session_scope(self.factory) as session:
            records = session.scalars(
                select(Submission).order_by(
                    Submission.submitted_at.desc(), Submission.submission_id
                )
            )
            return [self._summary(record) for record in records]

    def get_submission(self, submission_id: str) -> dict | None:
        with session_scope(self.factory) as session:
            record = session.get(Submission, submission_id)
            if record is None:
                return None
            snapshot = session.get(Snapshot, record.snapshot_id)
            content = self.store.read(
                snapshot.content_sha256,
                size_bytes=snapshot.size_bytes,
                file_count=snapshot.file_count,
            )
            return {
                **self._summary(record),
                "snapshot_id": snapshot.snapshot_id,
                "files": [
                    {"path": entry.path, "content": entry.content}
                    for entry in content.files
                ],
                "public_test_output": content.public_test_output,
            }

"""Persistent sandbox bindings and transaction-scoped cross-process row locks."""

import hashlib
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from .models import ExamSession, Invitation, Submission, WorkspaceFileRevision
from .session import get_session_factory, session_scope
from .states import SessionStatus as Status

if TYPE_CHECKING:
    from collections.abc import Iterable

    from app.submissions.service import SubmissionService
    from app.submissions.storage import SnapshotFile


class LifecycleNotFound(LookupError):
    pass


class SessionBusy(RuntimeError):
    pass


class LifecycleConflict(ValueError):
    pass


@dataclass(frozen=True)
class SessionView:
    session_id: str
    status: str
    task_version_id: str | None
    sandbox_id: str | None
    sandbox_resource_key: str | None
    sandbox_image_ref: str | None
    sandbox_backend_ref: str | None
    admitted_at: int
    started_at: int | None
    deadline_at: int | None
    submitted_at: int | None
    sandbox_deleted_at: int | None
    lifecycle_error_code: str | None
    lifecycle_error_at: int | None


def _view(record: ExamSession) -> SessionView:
    return SessionView(
        **{field: getattr(record, field) for field in SessionView.__dataclass_fields__}
    )


class LockedSession:
    """Valid only inside SessionLifecycle.lock_session; its transaction owns the lock."""

    def __init__(
        self, session: Session, record: ExamSession, clock: Callable[[], float]
    ):
        self._session = session
        self._record = record
        self._clock = clock
        self._transaction = session.get_transaction()
        self._closed = False
        self._cleanup_allowed = (
            record.status == Status.SUBMITTED
            and session.scalar(
                select(Submission.submission_id).where(
                    Submission.session_id == record.session_id
                )
            )
            is not None
        )

    def _active(self) -> None:
        if self._closed or self._session.get_transaction() is not self._transaction:
            raise RuntimeError("Session lock context has ended")

    def _now(self) -> int:
        now = int(self._clock())
        if now < 0:
            raise LifecycleConflict("Server time must be a nonnegative Unix timestamp")
        return now

    @property
    def view(self) -> SessionView:
        self._active()
        return _view(self._record)

    def _clear_error(self) -> None:
        self._record.lifecycle_error_code = None
        self._record.lifecycle_error_at = None

    def observe_file_revision(self, path: str, content_sha256: str) -> int:
        """Record an observed digest in this lock's transaction, without committing.

        The caller authorizes the file and reads its bytes under the existing
        session lock. This method only tracks observations, not filesystem writes.
        """
        self._active()
        if (
            not isinstance(path, str)
            or not 1 <= len(path) <= 500
            or any(char in path for char in ("\\", ":"))
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or any(part in ("", ".", "..") for part in path.split("/"))
        ):
            raise ValueError("File revision path must be a relative POSIX file path")
        if (
            not isinstance(content_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", content_sha256) is None
        ):
            raise ValueError("File revision digest must be lowercase SHA-256 hex")
        revision = self._session.get(
            WorkspaceFileRevision, (self._record.session_id, path)
        )
        if revision is None:
            revision = WorkspaceFileRevision(
                session_id=self._record.session_id,
                path=path,
                version=1,
                content_sha256=content_sha256,
            )
            self._session.add(revision)
        elif revision.content_sha256 != content_sha256:
            revision.version += 1
            revision.content_sha256 = content_sha256
        else:
            return revision.version
        # Flush so repeated observations also work with autoflush disabled.
        # The outer lock context still owns commit/rollback for all pending work.
        self._session.flush()
        return revision.version

    def _error(self, code: str) -> None:
        if (
            not isinstance(code, str)
            or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) is None
        ):
            raise ValueError("Lifecycle error must be a safe uppercase error code")
        self._record.lifecycle_error_code = code
        self._record.lifecycle_error_at = self._now()

    def pin_task_version(self, task_version_id: str) -> SessionView:
        self._active()
        if (
            not isinstance(task_version_id, str)
            or not 1 <= len(task_version_id) <= 128
            or task_version_id != task_version_id.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in task_version_id)
        ):
            raise ValueError(
                "Task version must be a nonblank release identifier of at most 128 characters"
            )
        row = self._record
        if row.task_version_id is not None:
            if row.task_version_id != task_version_id:
                raise LifecycleConflict("The task version is fixed for this session")
            return self.view
        if row.started_at is not None or row.status not in (
            Status.ADMITTED,
            Status.PROVISIONING,
            Status.PROVISION_FAILED,
            Status.READY,
        ):
            raise LifecycleConflict(
                "An unknown task version cannot be inferred after exam start"
            )
        row.task_version_id = task_version_id
        return self.view

    def prepare_sandbox(
        self, image_ref: str, backend_ref: str, *, task_version_id: str | None = None
    ) -> SessionView:
        self._active()
        if task_version_id is not None:
            self.pin_task_version(task_version_id)
        if (
            not isinstance(image_ref, str)
            or len(image_ref) > 512
            or re.fullmatch(r"(?:[^\s@]+@)?sha256:[0-9a-f]{64}", image_ref) is None
        ):
            raise ValueError(
                "Sandbox image must be an immutable SHA-256 image reference"
            )
        if (
            not isinstance(backend_ref, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", backend_ref) is None
        ):
            raise ValueError(
                "Sandbox backend must be a stable configured backend identifier"
            )
        row = self._record
        if row.status in (
            Status.SUBMITTING,
            Status.SUBMISSION_FAILED,
            Status.SUBMITTED,
        ):
            raise LifecycleConflict(
                "A submitting or submitted session cannot be provisioned"
            )
        if row.sandbox_image_ref is not None:
            if (
                row.sandbox_image_ref != image_ref
                or row.sandbox_backend_ref != backend_ref
            ):
                raise LifecycleConflict(
                    "The sandbox image and backend are fixed for this session"
                )
        elif row.status not in (
            Status.ADMITTED,
            Status.PROVISIONING,
            Status.PROVISION_FAILED,
        ):
            raise LifecycleConflict(
                "Cannot initialize a sandbox plan in this session state"
            )
        else:
            digest = hashlib.sha256(
                (backend_ref + "\0" + row.session_id).encode("utf-8")
            ).hexdigest()[:32]
            row.sandbox_resource_key = "vibeeval-" + digest
            row.sandbox_image_ref = image_ref
            row.sandbox_backend_ref = backend_ref
        if row.status in (Status.ADMITTED, Status.PROVISION_FAILED):
            row.status = Status.PROVISIONING
            self._clear_error()
        return self.view

    def bind_sandbox(self, sandbox_id: str, *, image_ref: str) -> SessionView:
        self._active()
        row = self._record
        if (
            not isinstance(sandbox_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}", sandbox_id) is None
        ):
            raise ValueError("Invalid sandbox identifier")
        if row.sandbox_image_ref is None or image_ref != row.sandbox_image_ref:
            raise LifecycleConflict("Sandbox image does not match the saved plan")
        if row.sandbox_deleted_at is not None:
            raise LifecycleConflict("A cleaned sandbox cannot be rebound")
        if row.sandbox_id is not None:
            if row.sandbox_id != sandbox_id:
                raise LifecycleConflict(
                    "An existing sandbox binding cannot be replaced"
                )
            return self.view
        if row.status != Status.PROVISIONING:
            raise LifecycleConflict("A sandbox can only be bound while provisioning")
        row.sandbox_id = sandbox_id
        return self.view

    def mark_ready(self) -> SessionView:
        self._active()
        row = self._record
        if not row.sandbox_id or row.sandbox_deleted_at is not None:
            raise LifecycleConflict("A live sandbox binding is required")
        if row.status in (Status.READY, Status.IN_PROGRESS):
            return self.view
        if row.status != Status.PROVISIONING:
            raise LifecycleConflict("Session is not provisioning")
        row.status = Status.IN_PROGRESS if row.started_at is not None else Status.READY
        self._clear_error()
        return self.view

    def start_exam(self) -> SessionView:
        self._active()
        row = self._record
        if row.started_at is not None and row.status in (
            Status.IN_PROGRESS,
            Status.SUBMITTING,
            Status.SUBMISSION_FAILED,
            Status.SUBMITTED,
        ):
            return self.view
        if row.started_at is not None:
            raise LifecycleConflict(
                "A started exam must recover without resetting its deadline"
            )
        if (
            row.status != Status.READY
            or not row.sandbox_id
            or row.sandbox_deleted_at is not None
        ):
            raise LifecycleConflict("Exam can only start with a ready sandbox")
        now = self._now()
        if now < row.admitted_at:
            raise LifecycleConflict("Exam start cannot precede admission")
        invitation = self._session.get(Invitation, row.invitation_hash)
        row.started_at = now
        row.deadline_at = now + invitation.duration_minutes * 60
        row.status = Status.IN_PROGRESS
        self._clear_error()
        return self.view

    def require_writable(self) -> SessionView:
        self._active()
        row = self._record
        if (
            row.status != Status.IN_PROGRESS
            or not row.sandbox_id
            or row.sandbox_deleted_at is not None
            or row.deadline_at is None
            or self._now() >= row.deadline_at
        ):
            raise LifecycleConflict("Session is not writable")
        return self.view

    def require_agent_idle(self):
        from .models import AgentOperation

        self._active()
        active = self._session.scalar(
            select(AgentOperation.operation_id).where(
                AgentOperation.exam_session_id == self._record.session_id,
                AgentOperation.holds_workspace.is_(True),
            )
        )
        if active is not None:
            raise SessionBusy("agent operation still owns the workspace")

    def begin_submission(self) -> SessionView:
        self._active()
        row = self._record
        if row.status == Status.SUBMITTED:
            return self.view
        if (
            row.status
            not in (Status.IN_PROGRESS, Status.SUBMITTING, Status.SUBMISSION_FAILED)
            or row.started_at is None
            or not row.sandbox_id
            or row.sandbox_deleted_at is not None
        ):
            raise LifecycleConflict("Session cannot enter submission")
        row.status = Status.SUBMITTING
        self._clear_error()
        return self.view

    def mark_provision_failed(self, code: str) -> SessionView:
        self._active()
        if self._record.status not in (
            Status.PROVISIONING,
            Status.READY,
            Status.IN_PROGRESS,
            Status.PROVISION_FAILED,
        ):
            raise LifecycleConflict(
                "Provision failure cannot change this session state"
            )
        self._error(code)
        self._record.status = Status.PROVISION_FAILED
        return self.view

    def mark_submission_failed(self, code: str) -> SessionView:
        self._active()
        if self._record.status == Status.SUBMITTED:
            return self.view
        if self._record.status not in (Status.SUBMITTING, Status.SUBMISSION_FAILED):
            raise LifecycleConflict("Session is not submitting")
        self._error(code)
        self._record.status = Status.SUBMISSION_FAILED
        return self.view

    def persist_frozen_submission(
        self,
        service: "SubmissionService",
        idempotency_key: str,
        files: "Iterable[SnapshotFile]",
        *,
        public_test_output: str | None = None,
    ) -> dict:
        """Return only after the surrounding lock context commits successfully."""
        self._active()
        return service.persist_in_transaction(
            self._session,
            self._record.session_id,
            idempotency_key,
            files,
            public_test_output=public_test_output,
        )

    def require_cleanup(self) -> SessionView:
        self._active()
        if not self._cleanup_allowed:
            raise LifecycleConflict(
                "Cleanup must use a new lock context after submission commits"
            )
        row = self._record
        committed = self._session.scalar(
            select(Submission.submission_id).where(
                Submission.session_id == row.session_id
            )
        )
        if row.status != Status.SUBMITTED or committed is None or not row.sandbox_id:
            raise LifecycleConflict("Cleanup requires a committed final submission")
        return self.view

    def mark_sandbox_deleted(self, sandbox_id: str) -> SessionView:
        self.require_cleanup()
        row = self._record
        if sandbox_id != row.sandbox_id:
            raise LifecycleConflict("Cleanup sandbox does not match the saved binding")
        if row.sandbox_deleted_at is None:
            now = self._now()
            if now < row.submitted_at:
                raise LifecycleConflict("Cleanup cannot precede submission")
            row.sandbox_deleted_at = now
        self._clear_error()
        return self.view

    def mark_cleanup_failed(self, code: str) -> SessionView:
        self.require_cleanup()
        if self._record.sandbox_deleted_at is not None:
            return self.view
        self._error(code)
        return self.view


class SessionLifecycle:
    def __init__(
        self,
        factory: sessionmaker[Session] | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.factory = factory if factory is not None else get_session_factory()
        self.clock = clock

    def get_session(self, session_id: str) -> SessionView | None:
        with session_scope(self.factory) as session:
            row = session.get(ExamSession, session_id)
            return _view(row) if row is not None else None

    @contextmanager
    def lock_session(self, session_id: str) -> Iterator[LockedSession]:
        locked = None
        try:
            with session_scope(self.factory) as session:
                row = session.scalar(
                    select(ExamSession)
                    .where(ExamSession.session_id == session_id)
                    .with_for_update(nowait=True)
                )
                if row is None:
                    raise LifecycleNotFound("Exam session was not found")
                if row.exam_mode == "multi_stage":
                    raise LifecycleConflict(
                        "Use the multi-stage examination coordinator"
                    )
                locked = LockedSession(session, row, self.clock)
                yield locked
        except OperationalError as error:
            if getattr(error.orig, "sqlstate", None) == "55P03":
                raise SessionBusy(
                    "Another process is operating on this session; retry or poll its status"
                ) from None
            raise
        finally:
            if locked is not None:
                locked._closed = True

    def prepare_sandbox(
        self,
        session_id: str,
        image_ref: str,
        *,
        backend_ref: str = "docker-gvisor:default",
        task_version_id: str | None = None,
    ) -> SessionView:
        with self.lock_session(session_id) as locked:
            return locked.prepare_sandbox(
                image_ref, backend_ref, task_version_id=task_version_id
            )

    def start_exam(self, session_id: str) -> SessionView:
        with self.lock_session(session_id) as locked:
            return locked.start_exam()

    def begin_submission(self, session_id: str) -> SessionView:
        with self.lock_session(session_id) as locked:
            return locked.begin_submission()

    @contextmanager
    def write_guard(self, session_id: str) -> Iterator[SessionView]:
        with self.lock_session(session_id) as locked:
            yield locked.require_writable()

    def list_cleanup_candidates(self, *, limit: int = 100) -> list[SessionView]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Cleanup limit must be an integer from 1 to 1000")
        with session_scope(self.factory) as session:
            records = session.scalars(
                select(ExamSession)
                .join(Submission, Submission.session_id == ExamSession.session_id)
                .where(
                    ExamSession.status == Status.SUBMITTED,
                    ExamSession.sandbox_id.is_not(None),
                    ExamSession.sandbox_deleted_at.is_(None),
                )
                .order_by(Submission.submitted_at, ExamSession.session_id)
                .limit(limit)
            )
            return [_view(row) for row in records]

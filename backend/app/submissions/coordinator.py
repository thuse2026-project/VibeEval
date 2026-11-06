"""Coordinate a trusted sandbox adapter and durable submission transactions."""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from app.db.lifecycle import SessionBusy, SessionLifecycle, SessionView
from app.db.states import SessionStatus

from .service import SubmissionService, validate_idempotency_key
from .storage import SnapshotFile

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FrozenWorkspace:
    session_id: str
    sandbox_id: str
    files: Iterable[SnapshotFile]
    public_test_output: str | None = None


class SubmissionSandbox(Protocol):
    """Both operations must be bounded, synchronous and safe to retry.

    Freeze must fence new file/PTY writes, drain accepted saves, pause ALL sandbox
    processes and return stable data for this exact binding. Keep it paused on
    failure. Export iterables must remain readable until persistence completes.
    Delete must treat an already absent container as success and retain candidate
    files. Neither method may start another database transaction on the locked row.
    """

    def freeze_and_export(self, view: SessionView) -> FrozenWorkspace: ...

    def delete_sandbox(self, view: SessionView) -> None: ...


class SubmissionAttemptFailed(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(
            "Submission could not be completed; retry with the same session"
        )


class SandboxCleanupFailed(RuntimeError):
    def __init__(self):
        self.code = "SANDBOX_CLEANUP_FAILED"
        super().__init__("Submission is saved; sandbox cleanup must be retried")


class SubmissionCoordinator:
    def __init__(
        self,
        sandbox: SubmissionSandbox,
        *,
        lifecycle: SessionLifecycle | None = None,
        submissions: SubmissionService | None = None,
    ):
        if not all(
            callable(getattr(sandbox, method, None))
            for method in ("freeze_and_export", "delete_sandbox")
        ):
            raise TypeError("A complete submission sandbox adapter is required")
        if lifecycle is None:
            lifecycle = (
                SessionLifecycle(submissions.factory, clock=submissions.clock)
                if submissions is not None
                else SessionLifecycle()
            )
        if submissions is None:
            submissions = SubmissionService(
                factory=lifecycle.factory, clock=lifecycle.clock
            )
        if lifecycle.factory is not submissions.factory:
            raise ValueError("Lifecycle and submissions must share one session factory")
        self.sandbox = sandbox
        self.lifecycle = lifecycle
        self.submissions = submissions

    def submit(self, session_id: str, idempotency_key: str) -> dict:
        """Return only a committed receipt; callers must authorize session ownership."""
        validate_idempotency_key(idempotency_key)
        receipt = self.submissions.get_session_receipt(session_id)
        if receipt is not None:
            return receipt

        # Commit the write fence before calling external sandbox operations.
        self.lifecycle.begin_submission(session_id)
        code = "SANDBOX_FREEZE_EXPORT_FAILED"
        try:
            with self.lifecycle.lock_session(session_id) as locked:
                if locked.view.status == SessionStatus.SUBMITTED:
                    # Another request finished between the fence and this lock.
                    receipt = locked.persist_frozen_submission(
                        self.submissions, idempotency_key, ()
                    )
                else:
                    locked.begin_submission()
                    view = locked.view
                    workspace = self.sandbox.freeze_and_export(view)
                    code = "SANDBOX_EXPORT_MISMATCH"
                    if (
                        not isinstance(workspace, FrozenWorkspace)
                        or workspace.session_id != view.session_id
                        or workspace.sandbox_id != view.sandbox_id
                    ):
                        raise ValueError("Export does not match the locked sandbox")
                    code = "SNAPSHOT_PERSIST_FAILED"
                    receipt = locked.persist_frozen_submission(
                        self.submissions,
                        idempotency_key,
                        workspace.files,
                        public_test_output=workspace.public_test_output,
                    )
            # Exiting the lock transaction must succeed before cleanup or ACK.
        except SessionBusy:
            raise
        except Exception:
            # A connection can fail after PostgreSQL has committed. Recover that
            # receipt before treating the attempt as failed or freezing again.
            receipt = self._recover_receipt(session_id)
            if receipt is None:
                self._record_failure(session_id, code, cleanup=False)
                raise SubmissionAttemptFailed(code) from None

        try:
            self.cleanup(session_id)
        except Exception:
            # Cleanup cannot revoke the durable receipt. A later cleanup sweep
            # uses list_cleanup_candidates(); never log adapter exception text.
            logger.warning("Committed submission has pending sandbox cleanup")
        return receipt

    def cleanup(self, session_id: str) -> SessionView:
        """Retry cleanup independently, only after a committed final submission."""
        attempted = False
        try:
            with self.lifecycle.lock_session(session_id) as locked:
                view = locked.require_cleanup()
                if view.sandbox_deleted_at is None:
                    attempted = True
                    self.sandbox.delete_sandbox(view)
                    view = locked.mark_sandbox_deleted(view.sandbox_id)
            return view
        except SessionBusy:
            raise
        except Exception:
            if not attempted:
                raise
            # Deletion and the DB commit cannot be atomic. An idempotent adapter
            # handles retry if the container is gone but its marker was rolled back.
            try:
                view = self.lifecycle.get_session(session_id)
                if view is not None and view.sandbox_deleted_at is not None:
                    return view
            except Exception:
                pass
            self._record_failure(session_id, "SANDBOX_CLEANUP_FAILED", cleanup=True)
            raise SandboxCleanupFailed() from None

    def _recover_receipt(self, session_id: str) -> dict | None:
        try:
            return self.submissions.get_session_receipt(session_id)
        except Exception:
            return None

    def _record_failure(self, session_id: str, code: str, *, cleanup: bool) -> None:
        try:
            with self.lifecycle.lock_session(session_id) as locked:
                if cleanup:
                    locked.mark_cleanup_failed(code)
                else:
                    locked.mark_submission_failed(code)
        except Exception:
            # DB outages or another active attempt may prevent recording. The
            # committed submitting/submitted state still fences candidate writes.
            logger.warning("Could not record submission lifecycle failure")

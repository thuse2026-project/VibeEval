"""Scan durable session state for expiry, interrupted submissions and cleanup."""

import argparse
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass

from sqlalchemy import and_, or_, select

from app.db.lifecycle import (
    LifecycleConflict,
    LifecycleNotFound,
    SessionBusy,
    SessionView,
)
from app.db.models import ExamSession, Submission
from app.db.session import session_scope
from app.db.states import SessionStatus as Status

from .coordinator import SubmissionCoordinator
from .service import SubmissionService
from .storage import SnapshotStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MaintenanceResult:
    examined: int = 0
    submitted: int = 0
    cleaned: int = 0
    deferred: int = 0
    busy: int = 0
    failed: int = 0


def _validate_limits(batch_size: int, retry_delay: int) -> None:
    if type(batch_size) is not int or not 1 <= batch_size <= 100:
        raise ValueError("Batch size must be an integer from 1 to 100")
    if type(retry_delay) is not int or not 1 <= retry_delay <= 3600:
        raise ValueError("Retry delay must be an integer from 1 to 3600 seconds")


class SubmissionMaintenance:
    def __init__(
        self,
        coordinator: SubmissionCoordinator,
        backend_ref: str,
        *,
        batch_size: int = 20,
        retry_delay: int = 30,
    ):
        _validate_limits(batch_size, retry_delay)
        if (
            not isinstance(backend_ref, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", backend_ref) is None
        ):
            raise ValueError("A stable sandbox backend identifier is required")
        self.coordinator = coordinator
        self.lifecycle = coordinator.lifecycle
        self.backend_ref = backend_ref
        self.batch_size = batch_size
        self.retry_delay = retry_delay

    def _now(self) -> int:
        now = int(self.lifecycle.clock())
        if now < 0:
            raise ValueError("Server time must be nonnegative")
        return now

    def _candidate_ids(self) -> list[str]:
        now = self._now()
        committed = (
            select(Submission.submission_id)
            .where(Submission.session_id == ExamSession.session_id)
            .exists()
        )
        eligible = or_(
            and_(
                ExamSession.status == Status.IN_PROGRESS, ExamSession.deadline_at <= now
            ),
            ExamSession.status.in_((Status.SUBMITTING, Status.SUBMISSION_FAILED)),
            and_(ExamSession.status == Status.SUBMITTED, committed),
        )
        with session_scope(self.lifecycle.factory) as session:
            return list(
                session.scalars(
                    select(ExamSession.session_id)
                    .where(
                        eligible,
                        ExamSession.sandbox_backend_ref == self.backend_ref,
                        ExamSession.sandbox_id.is_not(None),
                        ExamSession.sandbox_deleted_at.is_(None),
                        ExamSession.started_at.is_not(None),
                        or_(
                            ExamSession.lifecycle_error_at.is_(None),
                            ExamSession.lifecycle_error_at <= now - self.retry_delay,
                        ),
                    )
                    .order_by(
                        ExamSession.lifecycle_error_at.asc().nulls_first(),
                        ExamSession.deadline_at,
                        ExamSession.session_id,
                    )
                    .limit(self.batch_size)
                    .with_for_update(skip_locked=True)
                )
            )

    def _kind(self, view: SessionView) -> str | None:
        now = self._now()
        if (
            view.sandbox_backend_ref != self.backend_ref
            or not view.sandbox_id
            or view.sandbox_deleted_at is not None
            or view.started_at is None
            or (
                view.lifecycle_error_at is not None
                and view.lifecycle_error_at > now - self.retry_delay
            )
        ):
            return None
        if view.status == Status.SUBMITTED:
            return "cleaned"
        if view.status in (Status.SUBMITTING, Status.SUBMISSION_FAILED) or (
            view.status == Status.IN_PROGRESS
            and view.deadline_at is not None
            and view.deadline_at <= now
        ):
            return "submitted"
        return None

    def run_once(self) -> MaintenanceResult:
        """One bounded scan; no browser credential or in-memory work queue is used."""
        candidates = self._candidate_ids()
        counts = dict(submitted=0, cleaned=0, deferred=0, busy=0, failed=0)
        for session_id in candidates:
            try:
                # Recheck scope, retry delay and server deadline under the shared
                # lock. Commit the fence before calling the regular coordinator.
                with self.lifecycle.lock_session(session_id) as locked:
                    kind = self._kind(locked.view)
                    if kind == "submitted":
                        locked.begin_submission()
                    elif kind == "cleaned":
                        locked.require_cleanup()
                if kind is None:
                    counts["deferred"] += 1
                    continue
                if kind == "submitted":
                    key = (
                        "maintenance_" + hashlib.sha256(session_id.encode()).hexdigest()
                    )
                    self.coordinator.submit(session_id, key)
                else:
                    self.coordinator.cleanup(session_id)
                counts[kind] += 1
            except SessionBusy:
                counts["busy"] += 1
            except (LifecycleNotFound, LifecycleConflict):
                counts["deferred"] += 1
            except Exception:
                counts["failed"] += 1
                logger.warning("Submission maintenance operation failed")
        return MaintenanceResult(examined=len(candidates), **counts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument(
        "--snapshot-root", help="Use the same persistent snapshot directory as HTTP"
    )
    parser.add_argument(
        "--retry-delay", type=int, default=30, help="Seconds after a recorded failure"
    )
    parser.add_argument(
        "--watch", action="store_true", help="Keep scanning until stopped"
    )
    parser.add_argument(
        "--interval", type=int, default=5, help="Seconds between completed passes"
    )
    args = parser.parse_args(argv)
    try:
        _validate_limits(args.batch_size, args.retry_delay)
        if not 1 <= args.interval <= 3600:
            raise ValueError("Interval must be from 1 to 3600 seconds")
        if args.snapshot_root is not None and not args.snapshot_root.strip():
            raise ValueError("Snapshot root must not be blank")
    except ValueError as error:
        parser.error(str(error))
    try:
        from app.sandbox.runtime import create_submission_sandbox

        sandbox = create_submission_sandbox()
        if args.snapshot_root is None:
            coordinator = SubmissionCoordinator(sandbox)
        else:
            coordinator = SubmissionCoordinator(
                sandbox,
                submissions=SubmissionService(store=SnapshotStore(args.snapshot_root)),
            )
        maintenance = SubmissionMaintenance(
            coordinator,
            os.environ["SANDBOX_BACKEND_REF"],
            batch_size=args.batch_size,
            retry_delay=args.retry_delay,
        )
        while True:
            result = maintenance.run_once()
            print(json.dumps(asdict(result)), flush=True)
            if not args.watch:
                return int(result.failed > 0)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 130
    except Exception:
        logger.error(
            "Submission maintenance unavailable; check database and sandbox settings"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

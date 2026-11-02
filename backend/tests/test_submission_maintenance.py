"""Durable scans, expiry, shared retry delay and contention against PostgreSQL."""

import hashlib
import io
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import test_db_postgres as database_tests
from sqlalchemy.orm import sessionmaker

from app.db.lifecycle import LifecycleConflict, SessionLifecycle
from app.db.models import ExamSession, Invitation
from app.db.session import session_scope
from app.submissions.coordinator import FrozenWorkspace, SubmissionCoordinator
from app.submissions.maintenance import MaintenanceResult, SubmissionMaintenance, main
from app.submissions.service import SubmissionService
from app.submissions.storage import SnapshotFile, SnapshotStore

BACKEND = "docker-gvisor:worker-01"
IMAGE = "sha256:" + "a" * 64


class SandboxDouble:
    def __init__(self):
        self.events = []
        self.freeze_failures = set()
        self.delete_failures = set()
        self.on_freeze = lambda view: None

    def freeze_and_export(self, view):
        self.events.append(("freeze", view.session_id))
        self.on_freeze(view)
        if view.session_id in self.freeze_failures:
            raise OSError("private engine details")
        return FrozenWorkspace(
            view.session_id,
            view.sandbox_id,
            [SnapshotFile("answer.py", view.session_id)],
        )

    def delete_sandbox(self, view):
        self.events.append(("delete", view.session_id))
        if view.session_id in self.delete_failures:
            raise OSError("private engine details")


class MaintenanceConfigurationTests(unittest.TestCase):
    def test_invalid_limits_and_backend_fail_before_database_access(self):
        for change in (
            {"batch_size": 0},
            {"batch_size": 101},
            {"batch_size": True},
            {"retry_delay": 0},
            {"retry_delay": 3601},
            {"retry_delay": 1.5},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                SubmissionMaintenance(None, BACKEND, **change)
        for backend in ("", "with space", "x" * 129, None):
            with self.subTest(backend=backend), self.assertRaises(ValueError):
                SubmissionMaintenance(None, backend)

    def test_invalid_cli_arguments_do_not_construct_the_adapter(self):
        with patch("app.sandbox.runtime.create_submission_sandbox") as create:
            for arguments in (
                ["--batch-size", "0"],
                ["--retry-delay", "0"],
                ["--interval", "0"],
            ):
                with (
                    redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as error,
                ):
                    main(arguments)
                self.assertEqual(error.exception.code, 2)
            create.assert_not_called()

    def test_cli_once_outputs_only_counts_and_returns_failure_status(self):
        output = io.StringIO()
        result = MaintenanceResult(examined=2, submitted=1, failed=1)
        with (
            patch.dict(os.environ, {"SANDBOX_BACKEND_REF": BACKEND}),
            patch("app.sandbox.runtime.create_submission_sandbox"),
            patch("app.submissions.maintenance.SubmissionCoordinator"),
            patch("app.submissions.maintenance.SubmissionMaintenance") as runner,
            redirect_stdout(output),
        ):
            runner.return_value.run_once.return_value = result
            self.assertEqual(main([]), 1)
        self.assertEqual(json.loads(output.getvalue())["failed"], 1)
        self.assertEqual(
            set(json.loads(output.getvalue())),
            {"examined", "submitted", "cleaned", "deferred", "busy", "failed"},
        )

    def test_cli_hides_configuration_and_database_exception_text(self):
        with (
            patch.dict(os.environ, {"SANDBOX_BACKEND_REF": BACKEND}),
            patch(
                "app.sandbox.runtime.create_submission_sandbox",
                side_effect=RuntimeError("private credentials"),
            ),
            self.assertLogs("app.submissions.maintenance", level="ERROR") as logs,
        ):
            self.assertEqual(main([]), 1)
        self.assertNotIn("private", " ".join(logs.output))

    def test_cli_custom_snapshot_root_is_used_for_persistent_content(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"SANDBOX_BACKEND_REF": BACKEND}),
            patch("app.sandbox.runtime.create_submission_sandbox"),
            patch(
                "app.submissions.service.get_session_factory",
                return_value=sessionmaker(),
            ),
            patch("app.submissions.maintenance.SubmissionCoordinator") as coordinator,
            patch("app.submissions.maintenance.SubmissionMaintenance") as runner,
            redirect_stdout(io.StringIO()),
        ):
            runner.return_value.run_once.return_value = MaintenanceResult()
            self.assertEqual(main(["--snapshot-root", directory]), 0)
            service = coordinator.call_args.kwargs["submissions"]
            self.assertEqual(service.store.root, Path(directory).resolve())
            stored = service.store.write(
                [SnapshotFile("answer.py", "persistent answer")]
            )
            self.assertEqual(
                service.store.read(
                    stored.content_sha256,
                    size_bytes=stored.size_bytes,
                    file_count=stored.file_count,
                )
                .files[0]
                .content,
                "persistent answer",
            )

    def test_watch_can_be_stopped_without_starting_an_os_scheduler(self):
        with (
            patch.dict(os.environ, {"SANDBOX_BACKEND_REF": BACKEND}),
            patch("app.sandbox.runtime.create_submission_sandbox"),
            patch("app.submissions.maintenance.SubmissionCoordinator"),
            patch("app.submissions.maintenance.SubmissionMaintenance") as runner,
            patch(
                "app.submissions.maintenance.time.sleep", side_effect=KeyboardInterrupt
            ),
            redirect_stdout(io.StringIO()),
        ):
            runner.return_value.run_once.return_value = MaintenanceResult()
            self.assertEqual(main(["--watch"]), 130)
            runner.return_value.run_once.assert_called_once()


@unittest.skipUnless(
    os.environ.get("VIBEEVAL_RUN_DB_TESTS") == "1", "Requires PostgreSQL"
)
class SubmissionMaintenanceTests(unittest.TestCase):
    setUpClass = classmethod(database_tests.PostgreSQLTests.setUpClass.__func__)
    drop_test_database = classmethod(
        database_tests.PostgreSQLTests.drop_test_database.__func__
    )

    def setUp(self):
        database_tests.PostgreSQLTests.setUp(self)
        self.now = 1200
        self.lifecycle = SessionLifecycle(self.factory, clock=lambda: self.now)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.service = SubmissionService(
            SnapshotStore(directory.name), self.factory, clock=lambda: self.now
        )
        self.sandbox = SandboxDouble()
        self.coordinator = SubmissionCoordinator(
            self.sandbox, lifecycle=self.lifecycle, submissions=self.service
        )
        self.runner = SubmissionMaintenance(self.coordinator, BACKEND)
        self.started = self.seed("1")

    def seed(self, suffix, *, backend=BACKEND, start=True):
        digest = hashlib.sha256(suffix.encode()).hexdigest()
        sid = "ses_" + suffix
        with session_scope(self.factory) as session:
            session.add(
                Invitation(
                    token_hash=digest,
                    candidate_id="candidate-" + suffix,
                    candidate_name="考生",
                    exam_id="exam-1",
                    exam_title="自动交卷",
                    duration_minutes=60,
                    expires_at=10000,
                )
            )
            session.flush()
            session.add(
                ExamSession(
                    session_id=sid,
                    invitation_hash=digest,
                    browser_hash=digest,
                    admitted_at=1000,
                )
            )
        self.lifecycle.prepare_sandbox(
            sid, IMAGE, backend_ref=backend, task_version_id="problem:v1"
        )
        with self.lifecycle.lock_session(sid) as locked:
            locked.bind_sandbox("container-" + suffix, image_ref=IMAGE)
            locked.mark_ready()
        return (
            self.lifecycle.start_exam(sid) if start else self.lifecycle.get_session(sid)
        )

    def test_expiry_uses_server_deadline_and_commits_fence_before_export(self):
        self.now = self.started.deadline_at - 1
        self.assertEqual(self.runner.run_once(), MaintenanceResult())
        self.assertEqual(self.sandbox.events, [])
        self.now += 1
        self.sandbox.on_freeze = lambda view: self.assertEqual(
            self.lifecycle.get_session(view.session_id).status, "submitting"
        )
        result = self.runner.run_once()
        self.assertEqual((result.examined, result.submitted), (1, 1))
        receipt = self.service.get_session_receipt("ses_1")
        self.assertEqual(receipt["submitted_at"], self.started.deadline_at)
        self.assertEqual(
            self.lifecycle.get_session("ses_1").deadline_at, self.started.deadline_at
        )
        self.assertEqual(
            self.service.get_submission(receipt["submission_id"])["files"],
            [{"path": "answer.py", "content": "ses_1"}],
        )
        self.assertEqual(self.runner.run_once(), MaintenanceResult())

    def test_recovery_of_interrupted_manual_submission_before_deadline(self):
        self.lifecycle.begin_submission("ses_1")
        self.assertEqual(self.runner.run_once().submitted, 1)
        self.assertLess(
            self.service.get_session_receipt("ses_1")["submitted_at"],
            self.started.deadline_at,
        )

    def test_process_exit_after_fence_is_recoverable_by_a_new_runner(self):
        self.now = self.started.deadline_at
        with patch.object(self.coordinator, "submit", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.runner.run_once()
        self.assertEqual(self.lifecycle.get_session("ses_1").status, "submitting")
        self.assertEqual(self.sandbox.events, [])
        restarted = SubmissionMaintenance(self.coordinator, BACKEND)
        self.assertEqual(restarted.run_once().submitted, 1)

    def test_submission_failure_waits_for_shared_backoff_and_preserves_exam(self):
        self.now = self.started.deadline_at
        self.sandbox.freeze_failures.add("ses_1")
        with self.assertLogs("app.submissions.maintenance", level="WARNING") as logs:
            result = self.runner.run_once()
        self.assertEqual(result.failed, 1)
        self.assertNotIn("private", " ".join(logs.output))
        failed = self.lifecycle.get_session("ses_1")
        self.assertEqual(failed.status, "submission_failed")
        self.assertEqual(failed.sandbox_id, "container-1")
        self.assertEqual(failed.deadline_at, self.started.deadline_at)
        with self.assertRaises(LifecycleConflict), self.lifecycle.write_guard("ses_1"):
            self.fail("Submission failure cannot reopen editing")
        self.now += 29
        self.assertEqual(self.runner.run_once(), MaintenanceResult())
        self.sandbox.freeze_failures.clear()
        self.now += 1
        self.assertEqual(
            SubmissionMaintenance(self.coordinator, BACKEND).run_once().submitted, 1
        )
        self.assertEqual(
            self.sandbox.events,
            [("freeze", "ses_1"), ("freeze", "ses_1"), ("delete", "ses_1")],
        )

    def test_failed_manual_submission_can_retry_before_expiry(self):
        self.lifecycle.begin_submission("ses_1")
        with self.lifecycle.lock_session("ses_1") as locked:
            locked.mark_submission_failed("SNAPSHOT_PERSIST_FAILED")
        self.now += 30
        self.assertEqual(self.runner.run_once().submitted, 1)
        self.assertLess(self.now, self.started.deadline_at)

    def test_committed_receipt_cleanup_retries_without_exporting_again(self):
        self.sandbox.delete_failures.add("ses_1")
        with self.assertLogs("app.submissions.coordinator", level="WARNING"):
            receipt = self.coordinator.submit("ses_1", "manual-key")
        self.now += 29
        self.assertEqual(self.runner.run_once(), MaintenanceResult())
        self.sandbox.delete_failures.clear()
        self.now += 1
        self.assertEqual(self.runner.run_once().cleaned, 1)
        self.assertEqual(self.service.get_session_receipt("ses_1"), receipt)
        self.assertEqual(
            self.sandbox.events,
            [("freeze", "ses_1"), ("delete", "ses_1"), ("delete", "ses_1")],
        )
        self.assertEqual(self.runner.run_once(), MaintenanceResult())

    def test_cleanup_failure_stays_committed_and_retry_is_delayed(self):
        self.sandbox.delete_failures.add("ses_1")
        with self.assertLogs("app.submissions.coordinator", level="WARNING"):
            receipt = self.coordinator.submit("ses_1", "manual-key")
        self.now += 30
        with self.assertLogs("app.submissions.maintenance", level="WARNING"):
            self.assertEqual(self.runner.run_once().failed, 1)
        self.assertEqual(self.lifecycle.get_session("ses_1").status, "submitted")
        self.assertEqual(self.service.get_session_receipt("ses_1"), receipt)
        self.assertEqual(self.runner.run_once(), MaintenanceResult())

    def test_scans_are_scoped_to_one_physical_backend(self):
        self.seed("2", backend="docker-gvisor:other-host")
        self.now = self.started.deadline_at
        self.assertEqual(self.runner.run_once().submitted, 1)
        self.assertEqual(self.lifecycle.get_session("ses_2").status, "in_progress")
        self.assertEqual(
            self.sandbox.events, [("freeze", "ses_1"), ("delete", "ses_1")]
        )

    def test_unstarted_provision_failure_and_forged_submission_are_not_touched(self):
        self.seed("2", start=False)
        with self.lifecycle.lock_session("ses_1") as locked:
            locked.mark_provision_failed("SANDBOX_CONNECT_FAILED")
        self.seed("3")
        with session_scope(self.factory) as session:
            row = session.get(ExamSession, "ses_3")
            row.status, row.submitted_at = "submitted", self.now
        self.now = self.started.deadline_at
        self.assertEqual(self.runner.run_once(), MaintenanceResult())
        self.assertEqual(self.sandbox.events, [])

    def test_locked_oldest_row_does_not_starve_other_due_sessions(self):
        self.seed("2")
        self.now = self.started.deadline_at
        runner = SubmissionMaintenance(self.coordinator, BACKEND, batch_size=1)
        with self.lifecycle.lock_session("ses_1"):
            self.assertEqual(runner.run_once().submitted, 1)
            self.assertIsNone(self.service.get_session_receipt("ses_1"))
            self.assertIsNotNone(self.service.get_session_receipt("ses_2"))
        self.assertEqual(runner.run_once().submitted, 1)

    def test_two_runners_share_database_lock_and_only_one_exports(self):
        self.now = self.started.deadline_at
        freezing, release = threading.Event(), threading.Event()

        def wait(view):
            freezing.set()
            if not release.wait(10):
                raise TimeoutError("Test failed to release export")

        self.sandbox.on_freeze = wait
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.runner.run_once)
            try:
                self.assertTrue(freezing.wait(10))
                second = SubmissionMaintenance(self.coordinator, BACKEND).run_once()
                self.assertEqual(second, MaintenanceResult())
            finally:
                release.set()
            self.assertEqual(first.result(timeout=10).submitted, 1)
        self.assertEqual(
            self.sandbox.events, [("freeze", "ses_1"), ("delete", "ses_1")]
        )

    def test_completed_session_after_scan_is_rechecked_before_action(self):
        self.now = self.started.deadline_at
        scan = self.runner._candidate_ids

        def scan_then_submit():
            candidates = scan()
            self.coordinator.submit("ses_1", "manual-key")
            return candidates

        with patch.object(self.runner, "_candidate_ids", scan_then_submit):
            result = self.runner.run_once()
        self.assertEqual((result.examined, result.deferred), (1, 1))
        self.assertEqual(
            self.sandbox.events, [("freeze", "ses_1"), ("delete", "ses_1")]
        )

    def test_clock_rollback_after_scan_does_not_submit_early(self):
        self.now = self.started.deadline_at
        scan = self.runner._candidate_ids

        def scan_then_rollback():
            candidates = scan()
            self.now -= 1
            return candidates

        with patch.object(self.runner, "_candidate_ids", scan_then_rollback):
            self.assertEqual(self.runner.run_once().deferred, 1)
        self.assertEqual(self.lifecycle.get_session("ses_1").status, "in_progress")
        self.assertEqual(self.sandbox.events, [])

    def test_old_failure_does_not_starve_unattempted_rows(self):
        self.seed("2")
        self.now = self.started.deadline_at
        self.sandbox.freeze_failures.add("ses_1")
        runner = SubmissionMaintenance(self.coordinator, BACKEND, batch_size=1)
        with self.assertLogs("app.submissions.maintenance", level="WARNING"):
            self.assertEqual(runner.run_once().failed, 1)
        self.now += 30
        self.assertEqual(runner.run_once().submitted, 1)
        self.assertIsNotNone(self.service.get_session_receipt("ses_2"))
        self.assertIsNone(self.service.get_session_receipt("ses_1"))

    def test_one_record_failure_does_not_stop_the_batch(self):
        self.seed("2")
        self.now = self.started.deadline_at
        self.sandbox.freeze_failures.add("ses_1")
        with self.assertLogs("app.submissions.maintenance", level="WARNING"):
            result = self.runner.run_once()
        self.assertEqual((result.examined, result.submitted, result.failed), (2, 1, 1))

    def test_batch_limit_is_shared_across_submit_and_cleanup_work(self):
        self.seed("2")
        self.seed("3")
        self.now = self.started.deadline_at
        result = SubmissionMaintenance(
            self.coordinator, BACKEND, batch_size=1
        ).run_once()
        self.assertEqual((result.examined, result.submitted), (1, 1))
        self.assertEqual(len(self.service.list_submissions()), 1)

    def test_busy_race_after_scan_defers_without_sandbox_calls(self):
        self.now = self.started.deadline_at
        from app.db.lifecycle import SessionBusy

        with patch.object(self.lifecycle, "lock_session", side_effect=SessionBusy):
            self.assertEqual(self.runner.run_once().busy, 1)
        self.assertEqual(self.sandbox.events, [])

    def test_missing_row_after_scan_is_safe(self):
        from app.db.lifecycle import LifecycleNotFound

        with (
            patch.object(self.runner, "_candidate_ids", return_value=["missing"]),
            patch.object(self.lifecycle, "lock_session", side_effect=LifecycleNotFound),
        ):
            self.assertEqual(self.runner.run_once().deferred, 1)

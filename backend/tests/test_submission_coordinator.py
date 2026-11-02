"""Exercise orchestration against PostgreSQL, with only the sandbox replaced."""

import os
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app.db.config import get_database_url
from app.db.lifecycle import (
    LifecycleConflict,
    LifecycleNotFound,
    SessionBusy,
    SessionLifecycle,
)
from app.db.models import ExamSession, Invitation, Snapshot, Submission
from app.db.session import create_db_engine, session_scope
from app.submissions.coordinator import (
    FrozenWorkspace,
    SandboxCleanupFailed,
    SubmissionAttemptFailed,
    SubmissionCoordinator,
)
from app.submissions.service import SubmissionService
from app.submissions.storage import SnapshotFile, SnapshotStorageError, SnapshotStore

IMAGE = "registry.example/problem@sha256:" + "a" * 64


class FakeSandbox:
    def __init__(self):
        self.events = []
        self.paused = False
        self.deleted = False
        self.freeze_error = None
        self.delete_error = None
        self.on_freeze = lambda view: None
        self.on_delete = lambda view: None
        self.workspace = FrozenWorkspace(
            "ses_1", "abc123", [SnapshotFile("src/main.py", "print(1)\n")], "1 passed\n"
        )

    def freeze_and_export(self, view):
        self.events.append("freeze")
        self.paused = True
        self.on_freeze(view)
        if self.freeze_error is not None:
            raise self.freeze_error
        return self.workspace

    def delete_sandbox(self, view):
        self.events.append("delete")
        self.on_delete(view)
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted = True


class CoordinatorConfigurationTests(unittest.TestCase):
    def test_missing_adapter_is_rejected_before_database_access(self):
        for adapter in (None, object()):
            with self.subTest(adapter=adapter), self.assertRaises(TypeError):
                SubmissionCoordinator(adapter)

    def test_dependencies_must_use_the_same_factory(self):
        first, second = sessionmaker(), sessionmaker()
        with tempfile.TemporaryDirectory() as directory:
            service = SubmissionService(SnapshotStore(Path(directory)), second)
            with self.assertRaises(ValueError):
                SubmissionCoordinator(
                    FakeSandbox(),
                    lifecycle=SessionLifecycle(first),
                    submissions=service,
                )

    def test_one_supplied_dependency_defines_factory_and_clock(self):
        factory = sessionmaker()
        clock = lambda: 1234  # noqa: E731
        with tempfile.TemporaryDirectory() as directory:
            service = SubmissionService(
                SnapshotStore(Path(directory)), factory, clock=clock
            )
            coordinator = SubmissionCoordinator(FakeSandbox(), submissions=service)
            self.assertIs(coordinator.lifecycle.factory, factory)
            self.assertIs(coordinator.lifecycle.clock, clock)
        lifecycle = SessionLifecycle(factory, clock=clock)
        coordinator = SubmissionCoordinator(FakeSandbox(), lifecycle=lifecycle)
        self.assertIs(coordinator.submissions.factory, factory)
        self.assertIs(coordinator.submissions.clock, clock)


@unittest.skipUnless(
    os.environ.get("VIBEEVAL_RUN_DB_TESTS") == "1",
    "Set VIBEEVAL_RUN_DB_TESTS=1 for coordinator tests",
)
class SubmissionCoordinatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = get_database_url()
        cls.admin = create_engine(
            url.set(database="postgres"),
            isolation_level="AUTOCOMMIT",
            hide_parameters=True,
        )
        cls.addClassCleanup(cls.admin.dispose)
        cls.database_name = "vibeeval_test_" + uuid.uuid4().hex
        with cls.admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{cls.database_name}"')
        cls.addClassCleanup(cls.drop_test_database)
        cls.engine = create_db_engine(url.set(database=cls.database_name))
        cls.addClassCleanup(cls.engine.dispose)
        cls.factory = sessionmaker(cls.engine, expire_on_commit=False)
        migration = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        migration.attributes["database_url"] = url.set(database=cls.database_name)
        command.upgrade(migration, "head")

    @classmethod
    def drop_test_database(cls):
        with cls.admin.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{cls.database_name}"')

    def setUp(self):
        with self.engine.begin() as connection:
            for model in (Submission, Snapshot, ExamSession, Invitation):
                connection.execute(model.__table__.delete())
        with session_scope(self.factory) as session:
            session.add(
                Invitation(
                    token_hash="1" * 64,
                    candidate_id="candidate-1",
                    candidate_name="测试考生",
                    exam_id="exam-1",
                    exam_title="测试场次",
                    duration_minutes=60,
                    expires_at=5000,
                )
            )
            session.flush()
            session.add(
                ExamSession(
                    session_id="ses_1",
                    invitation_hash="1" * 64,
                    browser_hash="1" * 64,
                    admitted_at=1000,
                )
            )
        self.now = 1200
        self.lifecycle = SessionLifecycle(self.factory, clock=lambda: self.now)
        self.lifecycle.prepare_sandbox("ses_1", IMAGE, task_version_id="problem:v1")
        with self.lifecycle.lock_session("ses_1") as locked:
            locked.bind_sandbox("abc123", image_ref=IMAGE)
            locked.mark_ready()
        self.lifecycle.start_exam("ses_1")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SnapshotStore(Path(directory.name) / "snapshots")
        self.service = SubmissionService(
            self.store, self.factory, clock=lambda: self.now
        )
        self.sandbox = FakeSandbox()
        self.coordinator = SubmissionCoordinator(
            self.sandbox, lifecycle=self.lifecycle, submissions=self.service
        )

    def submit(self, key="key-1"):
        return self.coordinator.submit("ses_1", key)

    def assert_record_counts(self, expected):
        with session_scope(self.factory) as session:
            for model in (Submission, Snapshot):
                self.assertEqual(
                    session.scalar(select(func.count()).select_from(model)), expected
                )

    def assert_failed_and_preserved(self, code):
        view = self.lifecycle.get_session("ses_1")
        self.assertEqual(view.status, "submission_failed")
        self.assertEqual(view.lifecycle_error_code, code)
        self.assertEqual((view.sandbox_id, view.deadline_at), ("abc123", 4800))
        self.assertIsNone(view.submitted_at)
        self.assertIsNone(view.sandbox_deleted_at)
        self.assertTrue(self.sandbox.paused)
        self.assertFalse(self.sandbox.deleted)
        self.assertIsNone(self.service.get_session_receipt("ses_1"))
        self.assert_record_counts(0)
        with self.assertRaises(LifecycleConflict), self.lifecycle.write_guard("ses_1"):
            self.fail("Failed submission must not reopen editing")

    def test_fence_then_freeze_then_commit_then_cleanup(self):
        def check_freeze(view):
            committed = self.lifecycle.get_session("ses_1")
            self.assertEqual((view.status, committed.status), ("submitting",) * 2)
            self.assertIsNone(self.service.get_session_receipt("ses_1"))

        def check_delete(view):
            self.assertTrue(self.sandbox.paused)
            self.assertEqual(view.status, "submitted")
            receipt = self.service.get_session_receipt("ses_1")
            self.assertIsNotNone(receipt)
            detail = self.service.get_submission(receipt["submission_id"])
            self.assertEqual(detail["files"][0]["content"], "print(1)\n")
            self.assertEqual(detail["public_test_output"], "1 passed\n")

        self.sandbox.on_freeze, self.sandbox.on_delete = check_freeze, check_delete
        receipt = self.submit()
        self.assertEqual(receipt, self.service.get_session_receipt("ses_1"))
        self.assertEqual(self.sandbox.events, ["freeze", "delete"])
        self.assertEqual(self.lifecycle.get_session("ses_1").sandbox_deleted_at, 1200)
        self.assertEqual(self.lifecycle.list_cleanup_candidates(), [])
        self.assert_record_counts(1)

    def test_replays_return_first_receipt_without_adapter_calls(self):
        receipt = self.submit()
        self.sandbox.freeze_error = AssertionError("Must not export a replay")
        for key in ("key-1", "new-key"):
            self.assertEqual(self.submit(key), receipt)
        self.assertEqual(self.sandbox.events, ["freeze", "delete"])
        self.assert_record_counts(1)

    def test_another_request_can_finish_between_fence_and_export_lock(self):
        begin = self.lifecycle.begin_submission
        other = SubmissionCoordinator(
            self.sandbox,
            lifecycle=SessionLifecycle(self.factory, clock=lambda: self.now),
            submissions=self.service,
        )
        completed = []

        def finish_other_request(session_id):
            view = begin(session_id)
            completed.append(other.submit(session_id, "other-key"))
            return view

        with patch.object(self.lifecycle, "begin_submission", finish_other_request):
            receipt = self.submit()
        self.assertEqual(receipt, completed[0])
        self.assertEqual(self.sandbox.events, ["freeze", "delete"])
        self.assert_record_counts(1)

    def test_invalid_key_does_not_fence_or_freeze(self):
        for key in ("", "with space", None, "a" * 129):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.submit(key)
        self.assertEqual(self.lifecycle.get_session("ses_1").status, "in_progress")
        self.assertEqual(self.sandbox.events, [])

    def test_unknown_session_does_not_call_sandbox(self):
        with self.assertRaises(LifecycleNotFound):
            self.coordinator.submit("missing", "key")
        self.assertEqual(self.sandbox.events, [])

    def test_unstarted_session_cannot_submit(self):
        with session_scope(self.factory) as session:
            row = session.get(ExamSession, "ses_1")
            row.status, row.started_at, row.deadline_at = "ready", None, None
        with self.assertRaises(LifecycleConflict):
            self.submit()
        self.assertEqual(self.sandbox.events, [])

    def test_freeze_failure_preserves_workspace_and_retry_uses_original_binding(self):
        self.sandbox.freeze_error = RuntimeError("private engine credentials")
        with self.assertRaises(SubmissionAttemptFailed) as raised:
            self.submit()
        self.assertNotIn("private", str(raised.exception))
        self.assertEqual(raised.exception.code, "SANDBOX_FREEZE_EXPORT_FAILED")
        self.assert_failed_and_preserved(raised.exception.code)
        self.sandbox.freeze_error = None
        self.now = 1250
        receipt = self.submit("retry-key")
        self.assertEqual(receipt["submitted_at"], 1250)
        self.assertEqual(self.lifecycle.get_session("ses_1").deadline_at, 4800)
        self.assertEqual(self.sandbox.events, ["freeze", "freeze", "delete"])

    def test_export_must_match_both_session_and_sandbox(self):
        for workspace in (
            FrozenWorkspace("another-session", "abc123", []),
            FrozenWorkspace("ses_1", "another-container", []),
            object(),
        ):
            self.sandbox.workspace = workspace
            with (
                self.subTest(workspace=workspace),
                self.assertRaises(SubmissionAttemptFailed) as raised,
            ):
                self.submit()
            self.assertEqual(raised.exception.code, "SANDBOX_EXPORT_MISMATCH")
            self.assert_failed_and_preserved(raised.exception.code)

    def test_storage_failure_never_publishes_a_final_or_cleans_up(self):
        with (
            patch.object(
                self.store,
                "write",
                side_effect=SnapshotStorageError("private disk path"),
            ),
            self.assertRaises(SubmissionAttemptFailed) as raised,
        ):
            self.submit()
        self.assertEqual(raised.exception.code, "SNAPSHOT_PERSIST_FAILED")
        self.assert_failed_and_preserved(raised.exception.code)
        self.submit()
        self.assert_record_counts(1)

    def test_lazy_export_failure_is_rolled_back(self):
        def broken_export():
            yield SnapshotFile("a.py", "first file")
            raise OSError("lost export stream")

        self.sandbox.workspace = FrozenWorkspace("ses_1", "abc123", broken_export())
        with self.assertRaises(SubmissionAttemptFailed) as raised:
            self.submit()
        self.assert_failed_and_preserved(raised.exception.code)

    def listen(self, name, listener):
        event.listen(self.factory, name, listener)
        self.addCleanup(event.remove, self.factory, name, listener)

    def test_commit_failure_keeps_unreferenced_blob_and_allows_retry(self):
        fail = True

        def fail_final_commit(session):
            nonlocal fail
            row = session.get(ExamSession, "ses_1")
            if fail and row is not None and row.status == "submitted":
                fail = False
                raise RuntimeError("connection failed before final commit")

        self.listen("before_commit", fail_final_commit)
        with self.assertRaises(SubmissionAttemptFailed) as raised:
            self.submit()
        self.assert_failed_and_preserved(raised.exception.code)
        self.assertEqual(len(list(self.store.root.glob("*/*.json"))), 1)
        self.submit()
        self.assert_record_counts(1)
        self.assertEqual(len(list(self.store.root.glob("*/*.json"))), 1)

    def test_uncertain_commit_recovers_receipt_without_freezing_again(self):
        fail = True

        def remember_final(session):
            row = session.get(ExamSession, "ses_1")
            session.info["final"] = row is not None and row.status == "submitted"

        def lose_commit_ack(session):
            nonlocal fail
            if fail and session.info.get("final"):
                fail = False
                raise RuntimeError("connection failed after successful commit")

        self.listen("before_commit", remember_final)
        self.listen("after_commit", lose_commit_ack)
        receipt = self.submit()
        self.assertFalse(fail)
        self.assertEqual(self.service.get_session_receipt("ses_1"), receipt)
        self.assertEqual(self.lifecycle.get_session("ses_1").status, "submitted")
        self.assertEqual(self.sandbox.events, ["freeze", "delete"])
        self.assert_record_counts(1)

    def test_cleanup_failure_keeps_receipt_and_can_be_retried_independently(self):
        self.sandbox.delete_error = RuntimeError("private engine credentials")
        with self.assertLogs("app.submissions.coordinator", level="WARNING") as logged:
            receipt = self.submit()
        self.assertNotIn("private", " ".join(logged.output))
        pending = self.lifecycle.list_cleanup_candidates()
        self.assertEqual([view.session_id for view in pending], ["ses_1"])
        self.assertEqual(pending[0].status, "submitted")
        self.assertEqual(pending[0].lifecycle_error_code, "SANDBOX_CLEANUP_FAILED")
        self.assertEqual(self.submit("new-key"), receipt)
        self.assertEqual(self.sandbox.events, ["freeze", "delete"])
        with self.assertRaises(SandboxCleanupFailed) as raised:
            self.coordinator.cleanup("ses_1")
        self.assertNotIn("private", str(raised.exception))
        self.sandbox.delete_error = None
        self.coordinator.cleanup("ses_1")
        self.coordinator.cleanup("ses_1")
        self.assertEqual(self.sandbox.events, ["freeze", "delete", "delete", "delete"])
        self.assertEqual(self.lifecycle.list_cleanup_candidates(), [])
        self.assertEqual(self.service.get_session_receipt("ses_1"), receipt)

    def test_cleanup_before_submission_is_rejected(self):
        with self.assertRaises(LifecycleConflict):
            self.coordinator.cleanup("ses_1")
        self.assertEqual(self.sandbox.events, [])

    def test_cleanup_lock_competition_does_not_revoke_a_committed_receipt(self):
        with (
            patch.object(
                self.coordinator, "cleanup", side_effect=SessionBusy("Another cleaner")
            ),
            self.assertLogs("app.submissions.coordinator", level="WARNING"),
        ):
            receipt = self.submit()
        self.assertEqual(receipt, self.service.get_session_receipt("ses_1"))
        with self.lifecycle.lock_session("ses_1"):
            with self.assertRaises(SessionBusy):
                self.coordinator.cleanup("ses_1")
        self.assertEqual(self.sandbox.events, ["freeze"])
        self.coordinator.cleanup("ses_1")
        self.assertEqual(self.sandbox.events, ["freeze", "delete"])

    def test_delete_then_marker_commit_failure_retries_idempotent_deletion(self):
        fail = True

        def fail_cleanup_commit(session):
            nonlocal fail
            row = session.get(ExamSession, "ses_1")
            if fail and row is not None and row.sandbox_deleted_at is not None:
                fail = False
                raise RuntimeError("cleanup marker commit failed")

        self.listen("before_commit", fail_cleanup_commit)
        with self.assertLogs("app.submissions.coordinator", level="WARNING"):
            receipt = self.submit()
        self.assertTrue(self.sandbox.deleted)
        self.assertIsNone(self.lifecycle.get_session("ses_1").sandbox_deleted_at)
        self.assertEqual(len(self.lifecycle.list_cleanup_candidates()), 1)
        self.coordinator.cleanup("ses_1")
        self.assertEqual(self.sandbox.events, ["freeze", "delete", "delete"])
        self.assertEqual(self.service.get_session_receipt("ses_1"), receipt)

    def test_uncertain_cleanup_commit_recovers_marker(self):
        fail = True

        def remember_cleanup(session):
            row = session.get(ExamSession, "ses_1")
            session.info["cleaned"] = (
                row is not None and row.sandbox_deleted_at is not None
            )

        def lose_cleanup_ack(session):
            nonlocal fail
            if fail and session.info.get("cleaned"):
                fail = False
                raise RuntimeError("cleanup commit acknowledgement lost")

        self.listen("before_commit", remember_cleanup)
        self.listen("after_commit", lose_cleanup_ack)
        self.submit()
        self.assertFalse(fail)
        self.assertEqual(self.lifecycle.list_cleanup_candidates(), [])
        self.assertEqual(self.sandbox.events, ["freeze", "delete"])

    def test_concurrent_submit_is_busy_then_returns_the_same_receipt(self):
        freezing, resume = threading.Event(), threading.Event()

        def wait_for_second_request(view):
            freezing.set()
            if not resume.wait(10):
                raise TimeoutError("Test did not release freeze")

        self.sandbox.on_freeze = wait_for_second_request
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(self.submit)
            try:
                self.assertTrue(freezing.wait(10))
                with self.assertRaises(SessionBusy):
                    self.submit("second-key")
                self.assertEqual(self.sandbox.events, ["freeze"])
            finally:
                resume.set()
            receipt = first.result(timeout=10)
        self.assertEqual(self.submit("second-key"), receipt)
        self.assertEqual(self.sandbox.events, ["freeze", "delete"])
        self.assert_record_counts(1)

    def test_inflight_save_prevents_fence_until_lock_is_released(self):
        with self.lifecycle.write_guard("ses_1"):
            with self.assertRaises(SessionBusy):
                self.submit()
            self.assertEqual(self.lifecycle.get_session("ses_1").status, "in_progress")
            self.assertEqual(self.sandbox.events, [])
        self.submit()
        self.assert_record_counts(1)

    def test_failure_recording_outage_keeps_committed_write_fence(self):
        self.sandbox.freeze_error = OSError("sandbox export unavailable")
        real_lock = self.lifecycle.lock_session
        calls = 0

        def fail_recording(session_id):
            nonlocal calls
            calls += 1
            if calls >= 3:
                raise OSError("database unavailable")
            return real_lock(session_id)

        with (
            patch.object(self.lifecycle, "lock_session", fail_recording),
            self.assertLogs("app.submissions.coordinator", level="WARNING"),
            self.assertRaises(SubmissionAttemptFailed),
        ):
            self.submit()
        self.assertEqual(self.lifecycle.get_session("ses_1").status, "submitting")
        with self.assertRaises(LifecycleConflict), self.lifecycle.write_guard("ses_1"):
            self.fail("Committed submitting state must still fence writes")
        self.assertFalse(self.sandbox.deleted)

"""Real PostgreSQL tests, including a lock held by a separate OS process."""

import multiprocessing
import os
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
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
from app.submissions.service import SubmissionService
from app.submissions.storage import SnapshotFile, SnapshotStore

IMAGE = "registry.example/problems/python-debug-01@sha256:" + "a" * 64
BACKEND = "docker-gvisor:exam-node-01"


def hold_lock_in_child(url, ready):
    engine = create_db_engine(url)
    try:
        lifecycle = SessionLifecycle(sessionmaker(engine, expire_on_commit=False))
        with lifecycle.lock_session("ses_1"):
            ready.set()
            time.sleep(15)
    finally:
        engine.dispose()


@unittest.skipUnless(
    os.environ.get("VIBEEVAL_RUN_DB_TESTS") == "1",
    "Set VIBEEVAL_RUN_DB_TESTS=1 for lifecycle tests",
)
class SessionLifecycleTests(unittest.TestCase):
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
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.submissions = SubmissionService(
            SnapshotStore(Path(directory.name)), self.factory, clock=lambda: self.now
        )

    def ready(self, *, task_version_id=None):
        self.lifecycle.prepare_sandbox(
            "ses_1", IMAGE, backend_ref=BACKEND, task_version_id=task_version_id
        )
        with self.lifecycle.lock_session("ses_1") as locked:
            locked.bind_sandbox("abc123", image_ref=IMAGE)
            locked.mark_ready()

    def running(self):
        self.ready()
        return self.lifecycle.start_exam("ses_1")

    def commit_answer(self):
        self.running()
        self.lifecycle.begin_submission("ses_1")
        with self.lifecycle.lock_session("ses_1") as locked:
            receipt = locked.persist_frozen_submission(
                self.submissions, "key", [SnapshotFile("a.py", "print(1)")]
            )
        return receipt

    def test_plan_and_binding_survive_a_new_lifecycle_instance(self):
        plan = self.lifecycle.prepare_sandbox("ses_1", IMAGE, backend_ref=BACKEND)
        self.assertEqual(plan.status, "provisioning")
        self.assertIsNone(plan.started_at)
        self.assertIsNone(plan.deadline_at)
        with self.lifecycle.lock_session("ses_1") as locked:
            locked.bind_sandbox("abc123", image_ref=IMAGE)
            locked.mark_ready()
        restarted = SessionLifecycle(self.factory)
        view = restarted.get_session("ses_1")
        self.assertEqual(
            (view.sandbox_id, view.sandbox_image_ref, view.sandbox_backend_ref),
            ("abc123", IMAGE, BACKEND),
        )
        self.assertEqual(view.sandbox_resource_key, plan.sandbox_resource_key)
        self.assertEqual(view.status, "ready")

    def test_same_session_cannot_change_image_backend_or_binding(self):
        self.ready()
        for image, backend in (("sha256:" + "b" * 64, BACKEND), (IMAGE, "other-node")):
            with self.assertRaises(LifecycleConflict):
                self.lifecycle.prepare_sandbox("ses_1", image, backend_ref=backend)
        with (
            self.assertRaises(LifecycleConflict),
            self.lifecycle.lock_session("ses_1") as locked,
        ):
            locked.bind_sandbox("different-id", image_ref=IMAGE)
        with (
            self.assertRaises(LifecycleConflict),
            self.lifecycle.lock_session("ses_1") as locked,
        ):
            locked.bind_sandbox("abc123", image_ref="sha256:" + "b" * 64)

    def test_mutable_image_tag_is_rejected(self):
        with self.assertRaises(ValueError):
            self.lifecycle.prepare_sandbox("ses_1", "python:latest")
        self.assertEqual(self.lifecycle.get_session("ses_1").status, "admitted")

    def test_task_version_and_image_are_stored_separately_across_reconnect(self):
        self.ready(task_version_id="python-debug-01:v1")
        restored = SessionLifecycle(self.factory).get_session("ses_1")
        self.assertEqual(restored.task_version_id, "python-debug-01:v1")
        self.assertEqual(restored.sandbox_image_ref, IMAGE)

    def test_task_version_is_pinned_and_cannot_change_during_an_exam(self):
        self.ready(task_version_id="python-debug-01:v1")
        started = self.lifecycle.start_exam("ses_1")
        replay = self.lifecycle.prepare_sandbox(
            "ses_1", IMAGE, backend_ref=BACKEND, task_version_id="python-debug-01:v1"
        )
        self.assertEqual(replay.deadline_at, started.deadline_at)
        with self.assertRaises(LifecycleConflict):
            self.lifecycle.prepare_sandbox(
                "ses_1",
                IMAGE,
                backend_ref=BACKEND,
                task_version_id="python-debug-01:v2",
            )
        self.assertEqual(
            self.lifecycle.get_session("ses_1").task_version_id, "python-debug-01:v1"
        )

    def test_unknown_legacy_task_version_is_not_guessed_after_exam_start(self):
        self.running()
        self.assertIsNone(self.lifecycle.get_session("ses_1").task_version_id)
        with self.assertRaises(LifecycleConflict):
            self.lifecycle.prepare_sandbox(
                "ses_1",
                IMAGE,
                backend_ref=BACKEND,
                task_version_id="python-debug-01:v1",
            )
        self.assertIsNone(self.lifecycle.get_session("ses_1").task_version_id)

    def test_known_task_version_can_be_filled_before_exam_start(self):
        self.ready()
        view = self.lifecycle.prepare_sandbox(
            "ses_1", IMAGE, backend_ref=BACKEND, task_version_id="python-debug-01:v1"
        )
        self.assertEqual(
            (view.task_version_id, view.status), ("python-debug-01:v1", "ready")
        )

    def test_final_submission_keeps_its_original_task_and_image_references(self):
        self.ready(task_version_id="python-debug-01:v1")
        self.lifecycle.start_exam("ses_1")
        self.lifecycle.begin_submission("ses_1")
        with self.lifecycle.lock_session("ses_1") as locked:
            receipt = locked.persist_frozen_submission(self.submissions, "key", [])
        with session_scope(self.factory) as session:
            row = session.get(ExamSession, "ses_1")
            row.task_version_id = "later-metadata-edit"
            row.sandbox_image_ref = "sha256:" + "b" * 64
        with session_scope(self.factory) as session:
            saved = session.get(Submission, receipt["submission_id"])
            self.assertEqual(
                (saved.task_version_id, saved.sandbox_image_ref),
                ("python-debug-01:v1", IMAGE),
            )

    def test_invalid_task_version_identifier_is_rejected(self):
        for version in ("", " ", "line\nbreak", "a" * 129):
            with self.subTest(version=version), self.assertRaises(ValueError):
                self.lifecycle.prepare_sandbox("ses_1", IMAGE, task_version_id=version)
        self.assertIsNone(self.lifecycle.get_session("ses_1").task_version_id)

    def test_create_then_database_rollback_is_recovered_by_stable_resource_key(self):
        plan = self.lifecycle.prepare_sandbox("ses_1", IMAGE, backend_ref=BACKEND)
        containers = {}
        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            with self.lifecycle.lock_session("ses_1") as locked:
                containers[locked.view.sandbox_resource_key] = "already-created"
                locked.bind_sandbox("already-created", image_ref=IMAGE)
                raise RuntimeError("simulated crash")
        self.assertIsNone(self.lifecycle.get_session("ses_1").sandbox_id)
        recovered_plan = self.lifecycle.prepare_sandbox(
            "ses_1", IMAGE, backend_ref=BACKEND
        )
        self.assertEqual(plan.sandbox_resource_key, recovered_plan.sandbox_resource_key)
        with self.lifecycle.lock_session("ses_1") as locked:
            existing_id = containers[locked.view.sandbox_resource_key]
            locked.bind_sandbox(existing_id, image_ref=IMAGE)
            locked.mark_ready()
        self.assertEqual(len(containers), 1)
        self.assertEqual(
            self.lifecycle.get_session("ses_1").sandbox_id, "already-created"
        )

    def test_two_independent_connections_cannot_hold_the_same_session(self):
        second_engine = create_db_engine(self.engine.url)
        try:
            other = SessionLifecycle(
                sessionmaker(second_engine, expire_on_commit=False)
            )
            with self.lifecycle.lock_session("ses_1"):
                with self.assertRaises(SessionBusy):
                    with other.lock_session("ses_1"):
                        self.fail("Two connections acquired the same row lock")
            with other.lock_session("ses_1"):
                pass
        finally:
            second_engine.dispose()

    def test_lock_is_enforced_between_two_os_processes_and_released_on_process_death(
        self,
    ):
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        process = context.Process(
            target=hold_lock_in_child,
            args=(
                self.engine.url.render_as_string(hide_password=False),
                ready,
            ),
        )
        process.start()
        try:
            self.assertTrue(
                ready.wait(10), "Child process failed to acquire the row lock"
            )
            with self.assertRaises(SessionBusy):
                with self.lifecycle.lock_session("ses_1"):
                    pass
            process.terminate()
            process.join(10)
            self.assertFalse(process.is_alive())
            for attempt in range(50):
                try:
                    with self.lifecycle.lock_session("ses_1"):
                        break
                except SessionBusy:
                    if attempt == 49:
                        self.fail(
                            "Database did not release the terminated process's lock"
                        )
                    time.sleep(0.05)
        finally:
            if process.is_alive():
                process.terminate()
            process.join(10)

    def test_write_guard_prevents_submission_until_inflight_save_finishes(self):
        self.running()
        with self.lifecycle.write_guard("ses_1"):
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(self.lifecycle.begin_submission, "ses_1")
                with self.assertRaises(SessionBusy):
                    future.result(timeout=5)
        self.lifecycle.begin_submission("ses_1")
        with self.assertRaises(LifecycleConflict), self.lifecycle.write_guard("ses_1"):
            pass

    def test_start_is_idempotent_and_recovery_does_not_reset_deadline(self):
        started = self.running()
        self.now += 100
        self.assertEqual(
            self.lifecycle.start_exam("ses_1").deadline_at, started.deadline_at
        )
        with self.lifecycle.lock_session("ses_1") as locked:
            locked.mark_provision_failed("SANDBOX_UNAVAILABLE")
        self.lifecycle.prepare_sandbox("ses_1", IMAGE, backend_ref=BACKEND)
        with self.lifecycle.lock_session("ses_1") as locked:
            locked.bind_sandbox("abc123", image_ref=IMAGE)
            recovered = locked.mark_ready()
        self.assertEqual(
            (recovered.status, recovered.started_at, recovered.deadline_at),
            ("in_progress", started.started_at, started.deadline_at),
        )

    def test_expired_exam_rejects_writes_but_can_submit(self):
        started = self.running()
        self.now = started.deadline_at
        with self.assertRaises(LifecycleConflict), self.lifecycle.write_guard("ses_1"):
            pass
        self.assertEqual(self.lifecycle.begin_submission("ses_1").status, "submitting")

    def test_inconsistent_ready_state_cannot_reset_an_existing_exam_clock(self):
        started = self.running()
        with session_scope(self.factory) as session:
            session.get(ExamSession, "ses_1").status = "ready"
        self.now += 100
        with self.assertRaises(LifecycleConflict):
            self.lifecycle.start_exam("ses_1")
        self.assertEqual(
            self.lifecycle.get_session("ses_1").deadline_at, started.deadline_at
        )

    def test_submission_failure_keeps_binding_deadline_and_write_block(self):
        started = self.running()
        self.lifecycle.begin_submission("ses_1")
        with self.lifecycle.lock_session("ses_1") as locked:
            locked.mark_submission_failed("SNAPSHOT_WRITE_FAILED")
        failed = self.lifecycle.get_session("ses_1")
        self.assertEqual(
            (failed.status, failed.sandbox_id, failed.deadline_at),
            ("submission_failed", "abc123", started.deadline_at),
        )
        self.assertEqual(failed.lifecycle_error_code, "SNAPSHOT_WRITE_FAILED")
        with self.assertRaises(LifecycleConflict), self.lifecycle.write_guard("ses_1"):
            pass
        self.assertEqual(self.lifecycle.begin_submission("ses_1").status, "submitting")

    def test_submission_joins_the_existing_lifecycle_transaction_without_deadlock(self):
        receipt = self.commit_answer()
        self.assertEqual(self.submissions.get_session_receipt("ses_1"), receipt)
        self.assertEqual(self.lifecycle.get_session("ses_1").status, "submitted")
        with self.lifecycle.lock_session("ses_1") as locked:
            locked.mark_submission_failed("LATE_NETWORK_ERROR")
        self.assertEqual(self.lifecycle.get_session("ses_1").status, "submitted")

    def test_outer_transaction_failure_does_not_publish_a_receipt_or_allow_cleanup(
        self,
    ):
        self.running()
        self.lifecycle.begin_submission("ses_1")
        with self.assertRaisesRegex(RuntimeError, "rollback outer transaction"):
            with self.lifecycle.lock_session("ses_1") as locked:
                locked.persist_frozen_submission(
                    self.submissions, "key", [SnapshotFile("a.py", "1")]
                )
                raise RuntimeError("rollback outer transaction")
        self.assertIsNone(self.submissions.get_session_receipt("ses_1"))
        self.assertEqual(self.lifecycle.get_session("ses_1").status, "submitting")
        with (
            self.assertRaises(LifecycleConflict),
            self.lifecycle.lock_session("ses_1") as locked,
        ):
            locked.require_cleanup()

    def test_cleanup_is_forbidden_inside_the_uncommitted_submission_transaction(self):
        self.running()
        self.lifecycle.begin_submission("ses_1")
        with self.lifecycle.lock_session("ses_1") as locked:
            locked.persist_frozen_submission(self.submissions, "key", [])
            with self.assertRaises(LifecycleConflict):
                locked.mark_sandbox_deleted("abc123")
        with self.lifecycle.lock_session("ses_1") as locked:
            self.assertEqual(locked.require_cleanup().status, "submitted")

    def test_cleanup_is_separate_retryable_and_keeps_historical_binding(self):
        self.commit_answer()
        self.assertEqual(len(self.lifecycle.list_cleanup_candidates()), 1)
        with self.lifecycle.lock_session("ses_1") as locked:
            locked.mark_cleanup_failed("SANDBOX_CLEANUP_FAILED")
        self.assertEqual(self.lifecycle.get_session("ses_1").status, "submitted")
        self.assertEqual(len(self.lifecycle.list_cleanup_candidates()), 1)
        self.now += 10
        with self.lifecycle.lock_session("ses_1") as locked:
            locked.require_cleanup()
            locked.mark_sandbox_deleted("abc123")
        cleaned = self.lifecycle.get_session("ses_1")
        self.assertEqual(
            (cleaned.sandbox_id, cleaned.sandbox_deleted_at), ("abc123", self.now)
        )
        self.assertEqual(self.lifecycle.list_cleanup_candidates(), [])
        with (
            self.assertRaises(LifecycleConflict),
            self.lifecycle.lock_session("ses_1") as locked,
        ):
            locked.bind_sandbox("new-empty-environment", image_ref=IMAGE)

    def test_cleanup_before_commit_or_for_wrong_container_is_rejected(self):
        self.running()
        with (
            self.assertRaises(LifecycleConflict),
            self.lifecycle.lock_session("ses_1") as locked,
        ):
            locked.mark_sandbox_deleted("abc123")
        self.lifecycle.begin_submission("ses_1")
        with self.lifecycle.lock_session("ses_1") as locked:
            locked.persist_frozen_submission(self.submissions, "key", [])
        with (
            self.assertRaises(LifecycleConflict),
            self.lifecycle.lock_session("ses_1") as locked,
        ):
            locked.mark_sandbox_deleted("wrong-container")

    def test_handles_cannot_be_used_after_context_end(self):
        with self.lifecycle.lock_session("ses_1") as locked:
            pass
        with self.assertRaises(RuntimeError):
            _ = locked.view

    def test_unknown_session_and_invalid_failure_code_are_explicit(self):
        self.assertIsNone(self.lifecycle.get_session("missing"))
        with (
            self.assertRaises(LifecycleNotFound),
            self.lifecycle.lock_session("missing"),
        ):
            pass
        self.lifecycle.prepare_sandbox("ses_1", IMAGE)
        with (
            self.assertRaises(ValueError),
            self.lifecycle.lock_session("ses_1") as locked,
        ):
            locked.mark_provision_failed("private key or raw traceback")

    def test_database_enforces_canonical_states_complete_plans_and_cleanup_order(self):
        for changes in (
            {"status": "invented"},
            {"task_version_id": " "},
            {"sandbox_image_ref": IMAGE},
            {"lifecycle_error_code": "ERROR_WITHOUT_TIME"},
            {"sandbox_deleted_at": 1200},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaises(IntegrityError),
                session_scope(self.factory) as session,
            ):
                row = session.get(ExamSession, "ses_1")
                for field, value in changes.items():
                    setattr(row, field, value)


if __name__ == "__main__":
    unittest.main()

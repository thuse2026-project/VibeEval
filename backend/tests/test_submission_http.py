"""HTTP ownership and receipt guarantees with real PostgreSQL and a sandbox double."""

import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import test_db_postgres as database_tests
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError

from app.admin.accounts import hash_password
from app.db.lifecycle import LifecycleConflict, SessionLifecycle
from app.db.models import ExamSession, Snapshot, Submission
from app.exams.postgres import PostgresEntryStore
from app.exams.store import EntryStore
from app.main import create_app
from app.submissions.coordinator import FrozenWorkspace
from app.submissions.service import SubmissionService
from app.submissions.storage import SnapshotFile, SnapshotStorageError, SnapshotStore

BASE = "/api/v1/sessions"
HEADERS = {"X-VibeEval-Request": "1"}
IMAGE = "registry.example/problem@sha256:" + "a" * 64


class SandboxDouble:
    def __init__(self):
        self.events = []
        self.freeze_error = None
        self.delete_error = None
        self.on_freeze = lambda view: None
        self.on_delete = lambda view: None
        self.files = [SnapshotFile("src/main.py", "print('server answer')\n")]
        self.output = "1 passed\n"
        self.export_sandbox = None

    def freeze_and_export(self, view):
        self.events.append("freeze")
        self.on_freeze(view)
        if self.freeze_error:
            raise self.freeze_error
        return FrozenWorkspace(
            view.session_id,
            self.export_sandbox or view.sandbox_id,
            self.files,
            self.output,
        )

    def delete_sandbox(self, view):
        self.events.append("delete")
        self.on_delete(view)
        if self.delete_error:
            raise self.delete_error


class SubmissionAuthOrderTests(unittest.TestCase):
    def test_anonymous_requests_do_not_resolve_database_or_sandbox(self):
        with (
            patch(
                "app.exams.postgres.get_session_factory",
                side_effect=AssertionError("Database access before authentication"),
            ),
            TestClient(create_app({}, submission_sandbox=object())) as client,
        ):
            url = BASE + "/ses_unknown/submit"
            response = client.post(url, headers={"Idempotency-Key": "key"})
            self.assertEqual(response.status_code, 403)
            response = client.post(url, headers=HEADERS | {"Idempotency-Key": "key"})
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.headers["cache-control"], "no-store")

    def test_legacy_sqlite_cannot_be_used_for_production_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "entry.sqlite3"
            store = EntryStore(path)
            token, _ = store.invite("s1", "考生", "e1", "考试", 60, 24)
            app = create_app({}, entry_db=path, submission_sandbox=SandboxDouble())
            with TestClient(app, headers=HEADERS) as client:
                client.get(BASE + "/current")
                sid = client.post(BASE + "/verify-token", json={"token": token}).json()[
                    "session_id"
                ]
                response = client.post(
                    BASE + f"/{sid}/submit", headers={"Idempotency-Key": "key"}
                )
                self.assertEqual(response.status_code, 503)
                self.assertEqual(
                    response.json()["detail"]["code"], "SUBMISSION_UNAVAILABLE"
                )
                self.assertEqual(app.state.submission_sandbox.events, [])
                self.assertEqual(
                    client.get(BASE + "/current").json()["status"], "admitted"
                )


@unittest.skipUnless(
    os.environ.get("VIBEEVAL_RUN_DB_TESTS") == "1",
    "Set VIBEEVAL_RUN_DB_TESTS=1 for HTTP submission tests",
)
class SubmissionHttpTests(unittest.TestCase):
    setUpClass = classmethod(database_tests.PostgreSQLTests.setUpClass.__func__)
    drop_test_database = classmethod(
        database_tests.PostgreSQLTests.drop_test_database.__func__
    )

    def setUp(self):
        database_tests.PostgreSQLTests.setUp(self)
        self.entry = PostgresEntryStore(self.factory)
        token, _ = self.entry.invite("s1", "考生", "e1", "测试场次", 60, 24)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.snapshots = SnapshotStore(Path(directory.name) / "snapshots")
        self.service = SubmissionService(self.snapshots, self.factory)
        self.lifecycle = SessionLifecycle(self.factory)
        self.sandbox = SandboxDouble()
        self.accounts = {
            "examiner": {
                "name": "测试考官",
                "password_hash": hash_password("test-password-123"),
            }
        }
        self.app = self.new_app(self.sandbox)
        self.client = TestClient(self.app, headers=HEADERS)
        self.addCleanup(self.client.close)
        self.client.get(BASE + "/current")
        response = self.client.post(BASE + "/verify-token", json={"token": token})
        self.assertEqual(response.status_code, 200)
        self.sid = response.json()["session_id"]
        self.url = BASE + f"/{self.sid}/submit"
        self.lifecycle.prepare_sandbox(
            self.sid, IMAGE, task_version_id="python-debug-01:v1"
        )
        with self.lifecycle.lock_session(self.sid) as locked:
            locked.bind_sandbox("http-container", image_ref=IMAGE)
            locked.mark_ready()
        self.started = self.lifecycle.start_exam(self.sid)

    def new_app(self, adapter):
        return create_app(
            self.accounts,
            entry_store=self.entry,
            submission_sandbox=adapter,
            snapshot_store=self.snapshots,
        )

    def submit(self, key="key-1", **kwargs):
        return self.client.post(self.url, headers={"Idempotency-Key": key}, **kwargs)

    def assert_unsubmitted(self, status):
        view = self.lifecycle.get_session(self.sid)
        self.assertEqual(view.status, status)
        self.assertIsNone(view.submitted_at)
        self.assertIsNone(view.sandbox_deleted_at)
        self.assertEqual(view.sandbox_id, "http-container")
        self.assertEqual(view.deadline_at, self.started.deadline_at)
        self.assertIsNone(self.service.get_session_receipt(self.sid))
        with self.factory() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Submission)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(Snapshot)), 0)

    def test_authenticated_submit_commits_before_cleanup_and_response(self):
        def check_freeze(view):
            self.assertEqual(self.lifecycle.get_session(self.sid).status, "submitting")
            self.assertIsNone(self.service.get_session_receipt(self.sid))

        def check_cleanup(view):
            self.assertEqual(view.status, "submitted")
            self.assertIsNotNone(self.service.get_session_receipt(self.sid))

        self.sandbox.on_freeze = check_freeze
        self.sandbox.on_delete = check_cleanup
        response = self.submit()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        receipt = response.json()
        self.assertEqual(receipt, self.service.get_session_receipt(self.sid))
        self.assertEqual(receipt["status"], "frozen")
        self.assertEqual(
            set(receipt),
            {"submission_id", "session_id", "snapshot_id", "submitted_at", "status"},
        )
        self.assertGreaterEqual(receipt["submitted_at"], self.started.started_at)
        self.assertEqual(self.sandbox.events, ["freeze", "delete"])
        state = self.client.get(BASE + f"/{self.sid}/status").json()
        self.assertEqual(state["status"], "submitted")
        self.assertEqual(state["deadline_at"], self.started.deadline_at)
        with self.assertRaises(LifecycleConflict), self.lifecycle.write_guard(self.sid):
            self.fail("A successful submission must reject candidate writes")

    def test_candidate_cannot_supply_snapshot_or_sandbox_identity(self):
        response = self.submit(
            json={
                "files": [{"path": "forged.py", "content": "forged"}],
                "session_id": "someone-else",
                "sandbox_id": "someone-elses-container",
            }
        )
        self.assertEqual(response.status_code, 202)
        detail = self.service.get_submission(response.json()["submission_id"])
        self.assertEqual(
            detail["files"],
            [{"path": "src/main.py", "content": "print('server answer')\n"}],
        )

    def test_replay_with_same_or_new_key_returns_original_snapshot(self):
        receipt = self.submit().json()
        self.sandbox.files = [SnapshotFile("changed.py", "changed after submit")]
        for key in ("key-1", "new-key"):
            response = self.submit(key)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json(), receipt)
        self.assertEqual(self.sandbox.events, ["freeze", "delete"])

    def test_lost_response_can_be_recovered_after_restart_without_adapter(self):
        receipt = self.submit().json()
        with TestClient(
            self.new_app(None), headers=HEADERS, cookies=self.client.cookies
        ) as restarted:
            response = restarted.post(self.url, headers={"Idempotency-Key": "retry"})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json(), receipt)
        self.assertEqual(self.sandbox.events, ["freeze", "delete"])

    def test_missing_or_incomplete_adapter_does_not_fence_session(self):
        for adapter in (None, object()):
            self.app.state.submission_sandbox = adapter
            response = self.submit()
            self.assertEqual(response.status_code, 503)
            self.assertEqual(
                response.json()["detail"]["code"], "SUBMISSION_UNAVAILABLE"
            )
            self.assert_unsubmitted("in_progress")
        self.assertEqual(self.sandbox.events, [])

    def test_missing_and_invalid_keys_do_not_change_exam(self):
        for key in (None, "", "bad key", "bad/key", "a" * 129):
            headers = {} if key is None else {"Idempotency-Key": key}
            response = self.client.post(self.url, headers=headers)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assert_unsubmitted("in_progress")
        self.assertEqual(self.sandbox.events, [])

    def test_browser_header_is_required_even_with_valid_cookie(self):
        with TestClient(self.app, cookies=self.client.cookies) as without_header:
            response = without_header.post(self.url, headers={"Idempotency-Key": "key"})
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.sandbox.events, [])
        self.assert_unsubmitted("in_progress")

    def test_other_candidate_and_arbitrary_session_ids_are_rejected(self):
        other_token, _ = self.entry.invite("s2", "另一考生", "e1", "测试场次", 60, 24)
        with TestClient(self.app, headers=HEADERS) as outsider:
            outsider.get(BASE + "/current")
            self.assertEqual(
                outsider.post(
                    BASE + "/verify-token", json={"token": other_token}
                ).status_code,
                200,
            )
            self.assertEqual(
                outsider.post(self.url, headers={"Idempotency-Key": "key"}).status_code,
                404,
            )
        self.assertEqual(
            self.client.post(
                BASE + "/ses_unknown/submit", headers={"Idempotency-Key": "key"}
            ).status_code,
            404,
        )
        self.assertEqual(self.sandbox.events, [])
        self.assert_unsubmitted("in_progress")

    def test_unstarted_exam_returns_conflict_without_freezing(self):
        with self.factory.begin() as db:
            row = db.get(ExamSession, self.sid)
            row.status, row.started_at, row.deadline_at = "ready", None, None
        response = self.submit()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "SUBMISSION_NOT_READY")
        self.assertEqual(self.sandbox.events, [])
        self.assertIsNone(self.service.get_session_receipt(self.sid))

    def test_freeze_failure_returns_safe_retryable_error_and_keeps_write_fence(self):
        self.sandbox.freeze_error = RuntimeError("private engine credentials")
        response = self.submit()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["detail"]["code"], "SANDBOX_FREEZE_EXPORT_FAILED"
        )
        self.assertNotIn("private", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assert_unsubmitted("submission_failed")
        with self.assertRaises(LifecycleConflict), self.lifecycle.write_guard(self.sid):
            self.fail("A failed submission must remain fenced")
        self.sandbox.freeze_error = None
        self.assertEqual(self.submit("retry-key").status_code, 202)
        self.assertEqual(self.sandbox.events, ["freeze", "freeze", "delete"])

    def test_wrong_container_export_cannot_produce_a_success_response(self):
        self.sandbox.export_sandbox = "another-container"
        response = self.submit()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "SANDBOX_EXPORT_MISMATCH")
        self.assert_unsubmitted("submission_failed")
        self.assertEqual(self.sandbox.events, ["freeze"])

    def test_snapshot_failure_never_returns_a_success_receipt(self):
        with patch.object(
            self.snapshots, "write", side_effect=SnapshotStorageError("private path")
        ):
            response = self.submit()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "SNAPSHOT_PERSIST_FAILED")
        self.assertNotIn("private", response.text)
        self.assert_unsubmitted("submission_failed")
        self.assertEqual(self.sandbox.events, ["freeze"])

    def test_final_commit_failure_rolls_back_before_http_acknowledgement(self):
        fail = True

        def fail_commit(db):
            nonlocal fail
            row = db.get(ExamSession, self.sid)
            if fail and row is not None and row.status == "submitted":
                fail = False
                raise RuntimeError("private connection failure")

        event.listen(self.factory, "before_commit", fail_commit)
        self.addCleanup(event.remove, self.factory, "before_commit", fail_commit)
        response = self.submit()
        self.assertEqual(response.status_code, 503)
        self.assert_unsubmitted("submission_failed")
        self.assertEqual(self.sandbox.events, ["freeze"])
        self.assertEqual(self.submit("retry-key").status_code, 202)

    def test_cleanup_failure_still_returns_committed_receipt(self):
        self.sandbox.delete_error = RuntimeError("private engine credentials")
        with self.assertLogs("app.submissions.coordinator", level="WARNING") as logs:
            response = self.submit()
        self.assertEqual(response.status_code, 202)
        self.assertNotIn("private", response.text + " ".join(logs.output))
        self.assertEqual(response.json(), self.service.get_session_receipt(self.sid))
        pending = self.lifecycle.list_cleanup_candidates()
        self.assertEqual([row.session_id for row in pending], [self.sid])
        self.assertEqual(pending[0].status, "submitted")
        self.assertEqual(pending[0].lifecycle_error_code, "SANDBOX_CLEANUP_FAILED")

    def test_database_failure_is_safe_503_before_freeze(self):
        with patch(
            "app.submissions.router.SubmissionService.get_session_receipt",
            side_effect=OperationalError(
                "private SQL", {}, Exception("private password")
            ),
        ):
            response = self.submit()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.sandbox.events, [])
        self.assert_unsubmitted("in_progress")

    def test_concurrent_request_gets_retry_hint_then_original_receipt(self):
        freezing, resume = threading.Event(), threading.Event()

        def wait_for_competitor(view):
            freezing.set()
            if not resume.wait(10):
                raise TimeoutError("Test failed to release sandbox export")

        self.sandbox.on_freeze = wait_for_competitor
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(self.submit)
            try:
                self.assertTrue(freezing.wait(10))
                with TestClient(
                    self.app, headers=HEADERS, cookies=self.client.cookies
                ) as competitor:
                    second = competitor.post(
                        self.url, headers={"Idempotency-Key": "second-key"}
                    )
                self.assertEqual(second.status_code, 409)
                self.assertEqual(second.headers["retry-after"], "1")
                self.assertEqual(second.json()["detail"]["code"], "SESSION_BUSY")
                self.assertEqual(self.sandbox.events, ["freeze"])
            finally:
                resume.set()
            response = first.result(timeout=10)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.submit("second-key").json(), response.json())
        self.assertEqual(self.sandbox.events, ["freeze", "delete"])

    def test_inflight_save_returns_busy_and_does_not_start_freeze(self):
        with self.lifecycle.write_guard(self.sid):
            response = self.submit()
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.headers["retry-after"], "1")
            self.assertEqual(self.sandbox.events, [])
        self.assert_unsubmitted("in_progress")
        self.assertEqual(self.submit().status_code, 202)

    def test_examiner_can_read_the_http_submission_from_the_shared_snapshot_store(self):
        receipt = self.submit().json()
        with TestClient(self.app, headers=HEADERS) as examiner:
            self.assertEqual(
                examiner.post(
                    "/api/v1/admin/login",
                    json={"username": "examiner", "password": "test-password-123"},
                ).status_code,
                200,
            )
            overview = examiner.get("/api/v1/admin/overview")
            self.assertEqual(overview.status_code, 200)
            self.assertEqual(overview.json()["summary"]["submitted"], 1)
            detail = examiner.get(
                "/api/v1/admin/submissions/" + receipt["submission_id"]
            )
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.json()["snapshot_id"], receipt["snapshot_id"])
            self.assertEqual(detail.json()["public_test_output"], "1 passed\n")
            self.assertEqual(detail.json()["source"], "live")
            self.assertEqual(detail.json()["files"][0]["path"], "src/main.py")

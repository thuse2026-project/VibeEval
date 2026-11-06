"""Actual PostgreSQL admission, HTTP recovery and shared lifecycle integration."""

import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import test_db_postgres as database_tests
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.admin.accounts import hash_password
from app.db.lifecycle import SessionLifecycle
from app.db.models import ExamSession, Invitation
from app.exams.postgres import PostgresEntryStore
from app.exams.store import RECOVERY_SECONDS, EntryError, digest
from app.main import create_app
from app.submissions.service import SubmissionService
from app.submissions.storage import SnapshotFile, SnapshotStore


class EntryDatabaseFailureTests(unittest.TestCase):
    def test_database_failure_returns_safe_503_without_sqlite_fallback(self):
        factory = Mock()
        factory.begin.side_effect = OperationalError(
            "secret SQL", {}, Exception("secret credential")
        )
        with patch(
            "app.exams.store.EntryStore.connect",
            side_effect=AssertionError("SQLite fallback"),
        ):
            with TestClient(
                create_app({}, entry_store=PostgresEntryStore(factory)),
                headers={"X-VibeEval-Request": "1"},
            ) as client:
                self.assertEqual(
                    client.get("/api/v1/sessions/current").status_code, 401
                )
                response = client.post(
                    "/api/v1/sessions/verify-token", json={"token": "tok_test"}
                )
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("secret", response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")

    def test_unauthenticated_admin_does_not_initialize_database_reader(self):
        with patch(
            "app.exams.postgres.get_session_factory",
            side_effect=AssertionError("DB before auth"),
        ):
            with TestClient(create_app({})) as client:
                self.assertEqual(client.get("/api/v1/admin/overview").status_code, 401)


@unittest.skipUnless(
    os.environ.get("VIBEEVAL_RUN_DB_TESTS") == "1", "Requires isolated PostgreSQL"
)
class EntryPostgresTests(unittest.TestCase):
    setUpClass = classmethod(database_tests.PostgreSQLTests.setUpClass.__func__)
    drop_test_database = classmethod(
        database_tests.PostgreSQLTests.drop_test_database.__func__
    )

    def setUp(self):
        database_tests.PostgreSQLTests.setUp(self)
        self.store = PostgresEntryStore(self.factory)
        self.token, self.expires = self.store.invite(
            "s1", "考生", "e1", "测试场次", 60, 24
        )

    def app(self):
        return create_app({}, entry_store=self.store)

    def test_http_admission_cookie_recovery_and_authorization(self):
        with TestClient(self.app()) as client:
            headers = {"X-VibeEval-Request": "1"}
            self.assertEqual(
                client.post(
                    "/api/v1/sessions/verify-token",
                    headers=headers,
                    json={"token": self.token},
                ).status_code,
                428,
            )
            self.assertEqual(
                client.get("/api/v1/sessions/current", headers=headers).status_code, 401
            )
            response = client.post(
                "/api/v1/sessions/verify-token",
                headers=headers,
                json={"token": self.token},
            )
            self.assertEqual(response.status_code, 200)
            sid = response.json()["session_id"]
            self.assertNotIn("hash", response.text)
            self.assertIsNone(response.json()["started_at"])
            self.assertEqual(
                client.get(f"/api/v1/sessions/{sid}/status").status_code, 403
            )
            self.assertEqual(
                client.get(
                    "/api/v1/sessions/other/status", headers=headers
                ).status_code,
                404,
            )
            with TestClient(
                self.app(), cookies=client.cookies, headers=headers
            ) as restarted:
                self.assertEqual(
                    restarted.get("/api/v1/sessions/current").json(), response.json()
                )
            with TestClient(self.app(), headers=headers) as outsider:
                self.assertEqual(
                    outsider.get(f"/api/v1/sessions/{sid}/status").status_code, 404
                )

    def test_shared_lifecycle_and_deadline_are_observed_without_reset(self):
        row = self.store.admit(self.token, "browser")
        lifecycle = SessionLifecycle(factory=self.factory)
        # The sandbox owner writes these states; admission only reads them.
        with self.factory.begin() as db:
            record = db.get(ExamSession, row["session_id"])
            record.status = "ready"
            record.task_version_id = "python-debug-01:v1"
        self.assertEqual(self.store.current("browser")["status"], "ready")
        # Exercise shared start operation with a valid bound sandbox.
        with self.factory.begin() as db:
            record = db.get(ExamSession, row["session_id"])
            record.sandbox_id = "test-container"
        started = lifecycle.start_exam(row["session_id"])
        again = lifecycle.start_exam(row["session_id"])
        self.assertEqual(started.deadline_at, again.deadline_at)
        current = self.store.current("browser")
        self.assertEqual(current["status"], "in_progress")
        self.assertEqual(current["deadline_at"], started.deadline_at)
        self.assertEqual(current["task_version_id"], "python-debug-01:v1")
        self.assertEqual(self.store.admit(self.token, "browser"), current)
        self.assertEqual(self.store.list_entries()[0]["status"], "in_progress")

    def test_concurrent_same_browser_same_token_is_idempotent(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(
                pool.map(lambda _: self.store.admit(self.token, "same"), range(6))
            )
        self.assertEqual(len({r["session_id"] for r in results}), 1)
        with self.factory() as db:
            self.assertEqual(
                db.scalar(select(func.count()).select_from(ExamSession)), 1
            )
            record = db.scalar(select(ExamSession))
            self.assertEqual(record.browser_hash, digest("same"))

    def claim(self, token, browser):
        try:
            return self.store.admit(token, browser)["session_id"]
        except EntryError as error:
            return error.status

    def test_concurrent_different_browsers_cannot_consume_one_token(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(lambda b: self.claim(self.token, b), ["one", "two"])
            )
        self.assertEqual(sum(isinstance(x, str) for x in results), 1)
        self.assertIn(409, results)

    def test_concurrent_one_browser_cannot_consume_two_tokens(self):
        other, _ = self.store.invite("s2", "二", "e1", "测试场次", 60, 24)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(lambda t: self.claim(t, "same"), [self.token, other])
            )
        self.assertEqual(sum(isinstance(x, str) for x in results), 1)
        self.assertIn(409, results)

    def test_expiry_retry_and_recovery_deadline(self):
        self.assertEqual(self.claim("invalid", "browser"), 401)
        with patch("app.exams.postgres.time.time", return_value=self.expires):
            self.assertEqual(self.claim(self.token, "browser"), 410)
        admitted = self.store.admit(self.token, "browser")
        with patch("app.exams.postgres.time.time", return_value=self.expires + 1):
            self.assertEqual(self.store.admit(self.token, "browser"), admitted)
        with patch(
            "app.exams.postgres.time.time",
            return_value=admitted["admitted_at"] + RECOVERY_SECONDS,
        ):
            with self.assertRaises(EntryError) as error:
                self.store.current("browser")
            self.assertEqual(error.exception.status, 410)

    def test_failed_transaction_does_not_consume_invitation(self):
        with patch.object(
            self.store, "public", side_effect=RuntimeError("before commit")
        ):
            with self.assertRaises(RuntimeError):
                self.store.admit(self.token, "browser")
        with self.factory() as db:
            self.assertEqual(
                db.scalar(select(func.count()).select_from(ExamSession)), 0
            )
        self.assertIsNotNone(self.store.admit(self.token, "browser"))

    def test_duplicate_invitation_and_input_validation(self):
        with self.assertRaises(ValueError):
            self.store.invite("s1", "考生", "e1", "测试场次", 60, 24)
        with self.assertRaises(ValueError):
            self.store.invite("s2", "", "e1", "测试场次", 60, 24)
        with self.factory() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Invitation)), 1)

    def test_real_examiner_reader_uses_same_database_snapshot(self):
        row = self.store.admit(self.token, "browser")
        now = row["admitted_at"]
        with self.factory.begin() as db:
            record = db.get(ExamSession, row["session_id"])
            record.status = "submitting"
            record.started_at = now
            record.deadline_at = now + 3600
        with tempfile.TemporaryDirectory() as directory:
            service = SubmissionService(
                store=SnapshotStore(root=directory), factory=self.factory
            )
            receipt = service.persist_frozen_submission(
                row["session_id"],
                "test-key",
                [SnapshotFile("answer.py", "print(1)")],
                public_test_output="test output",
            )
            app = create_app(
                {
                    "examiner": {
                        "name": "考官",
                        "password_hash": hash_password("test-password-123"),
                    }
                },
                entry_store=self.store,
            )
            # Use production reader wiring; only redirect its snapshot filesystem.
            with (
                patch(
                    "app.submissions.service.SnapshotStore", return_value=service.store
                ),
                TestClient(app, headers={"X-VibeEval-Request": "1"}) as client,
            ):
                self.assertEqual(
                    client.post(
                        "/api/v1/admin/login",
                        json={"username": "examiner", "password": "test-password-123"},
                    ).status_code,
                    200,
                )
                data = client.get("/api/v1/admin/overview").json()
                self.assertEqual(data["summary"]["submitted"], 1)
                self.assertEqual(
                    data["items"][0]["submission_id"], receipt["submission_id"]
                )
                detail = client.get(
                    "/api/v1/admin/submissions/" + receipt["submission_id"]
                ).json()
                self.assertEqual(detail["files"][0]["content"], "print(1)")
                self.assertEqual(detail["public_test_output"], "test output")

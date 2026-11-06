"""Preparation/start HTTP contract; real PostgreSQL is explicitly opt-in."""

import os
import unittest
from unittest.mock import Mock, patch

import test_db_postgres as database_tests
from fastapi.testclient import TestClient

from app.exams.postgres import PostgresEntryStore
from app.main import create_app
from app.workspace.router import router

HEADERS = {"X-VibeEval-Request": "1"}
BASE = "/api/v1/sessions"


class PreparationAuthTests(unittest.TestCase):
    def test_anonymous_requests_never_reach_sandbox(self):
        runtime = Mock()
        app = create_app({}, submission_sandbox=runtime)
        app.include_router(router)
        with TestClient(app) as client:
            for operation in ("prepare", "start"):
                url = f"{BASE}/unknown/{operation}"
                self.assertEqual(client.post(url).status_code, 403)
                self.assertEqual(client.post(url, headers=HEADERS).status_code, 404)
        runtime.ensure_bound.assert_not_called()
        runtime.check_ready.assert_not_called()


@unittest.skipUnless(
    os.environ.get("VIBEEVAL_RUN_DB_TESTS") == "1", "requires PostgreSQL"
)
class PreparationDatabaseTests(unittest.TestCase):
    setUpClass = classmethod(database_tests.PostgreSQLTests.setUpClass.__func__)
    drop_test_database = classmethod(
        database_tests.PostgreSQLTests.drop_test_database.__func__
    )

    def test_prepare_start_replay_and_cross_browser(self):
        import uuid

        entry = PostgresEntryStore(factory=self.factory)
        token, _ = entry.invite(
            uuid.uuid4().hex, "考生", uuid.uuid4().hex, "考试", 60, 24
        )
        runtime = Mock()
        runtime.ensure_bound.return_value = "a" * 64
        runtime.check_ready.return_value = True
        app = create_app({}, entry_store=entry, submission_sandbox=runtime)
        app.include_router(router)
        config = {
            "SANDBOX_IMAGE_REF": "sha256:" + "b" * 64,
            "SANDBOX_BACKEND_REF": "test-node",
            "SANDBOX_TASK_VERSION": "booking:v1",
        }
        with patch.dict(os.environ, config), TestClient(app, headers=HEADERS) as client:
            client.get(BASE + "/current")
            response = client.post(BASE + "/verify-token", json={"token": token})
            self.assertEqual(response.status_code, 200)
            sid = response.json()["session_id"]
            path = BASE + "/" + sid
            self.assertEqual(client.post(path + "/start").status_code, 409)
            ready = client.post(path + "/prepare")
            self.assertEqual(ready.status_code, 200, ready.text)
            self.assertEqual(ready.json()["status"], "ready")
            self.assertIsNone(ready.json()["started_at"])
            self.assertEqual(client.post(path + "/prepare").status_code, 200)
            runtime.ensure_bound.assert_called_once()
            started = client.post(path + "/start")
            self.assertEqual(started.status_code, 200, started.text)
            self.assertEqual(started.json()["status"], "in_progress")
            self.assertEqual(client.post(path + "/start").json(), started.json())
            self.assertEqual(client.get(BASE + "/current").json()["session_id"], sid)
            with TestClient(app, headers=HEADERS) as stranger:
                self.assertEqual(stranger.post(path + "/prepare").status_code, 404)
                self.assertEqual(stranger.post(path + "/start").status_code, 404)

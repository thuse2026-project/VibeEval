"""HTTP guardrails run without a database or model provider."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.agent_integration.store import StoredStateInvalid
from app.exams.router import require_candidate_session
from app.main import create_app

HEADERS = {"X-VibeEval-Request": "1"}
PREFIX = "/api/v1/sessions/owned/agent/conversations"


class AgentIntegrationHttpTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Mock()
        self.app = create_app(
            {},
            entry_db=Path(directory.name) / "entry.sqlite3",
            agent_enabled=True,
            agent_store=self.store,
        )

    def test_unauthorized_request_never_touches_agent_store(self):
        with TestClient(self.app, headers=HEADERS) as client:
            self.assertEqual(client.get(PREFIX).status_code, 404)
            self.assertEqual(client.post(PREFIX, json={}).status_code, 404)
        self.assertEqual(self.store.mock_calls, [])

    def test_source_header_is_required_even_with_owned_session(self):
        self.app.dependency_overrides[require_candidate_session] = lambda: {
            "session_id": "owned"
        }
        with TestClient(self.app) as client:
            self.assertEqual(client.get(PREFIX).status_code, 403)
            self.assertEqual(client.post(PREFIX, json={}).status_code, 403)
        self.assertEqual(self.store.mock_calls, [])

    def test_legacy_sqlite_entry_does_not_silently_become_agent_persistence(self):
        self.app.state.agent_store = None
        self.app.dependency_overrides[require_candidate_session] = lambda: {
            "session_id": "owned"
        }
        with TestClient(self.app, headers=HEADERS) as client:
            response = client.get(PREFIX)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "AGENT_STORAGE_UNAVAILABLE")

    def test_database_and_corrupt_state_errors_are_not_exposed(self):
        self.app.dependency_overrides[require_candidate_session] = lambda: {
            "session_id": "owned"
        }
        for error in (
            OperationalError("private SQL", {}, Exception("private credential")),
            StoredStateInvalid("private snapshot"),
        ):
            self.store.describe.side_effect = error
            with (
                self.subTest(error=type(error).__name__),
                TestClient(self.app, headers=HEADERS) as client,
            ):
                response = client.get(PREFIX + "/conversation")
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("private", response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")

    def test_disabled_interface_never_initializes_agent_repository(self):
        self.app.state.agent_enabled = False
        self.app.dependency_overrides[require_candidate_session] = lambda: {
            "session_id": "owned"
        }
        with TestClient(self.app, headers=HEADERS) as client:
            self.assertEqual(client.get(PREFIX).status_code, 403)
        self.assertEqual(self.store.mock_calls, [])

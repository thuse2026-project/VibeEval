"""Real browser-bound entry and v2 Cookie exchange, with external adapters."""

import tempfile
import unittest
from pathlib import Path

from exam_flow_support import MemoryFlowStore, Questions, Resources, definition
from fastapi.testclient import TestClient

from app.exam_flow import ExamFlowService
from app.exams.router import COOKIE_NAME as ENTRY_COOKIE
from app.exams.store import EntryStore
from app.main import create_app

HEADERS = {"X-VibeEval-Request": "1"}
ENTRY = "/api/v1/sessions"


class FlowHttpTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "entry.sqlite3"
        self.entry = EntryStore(path)
        self.flow = ExamFlowService(
            MemoryFlowStore(), Questions(), Resources(), clock=lambda: 1000
        )
        self.app = create_app(
            {}, entry_store=self.entry, entry_db=path, exam_flow=self.flow
        )
        self.client = TestClient(self.app, headers=HEADERS)
        self.addCleanup(self.client.close)
        self.client.get(ENTRY + "/current")
        token, _ = self.entry.invite("candidate", "Candidate", "exam", "Exam", 60, 24)
        admitted = self.client.post(
            ENTRY + "/verify-token", json={"token": token}
        ).json()
        self.sid = admitted["session_id"]
        self.flow.attach(self.sid, definition())
        self.exchange = ENTRY + "/" + self.sid + "/exam-access"

    def command(self, path, key, version=None, **extra):
        if version is None:
            version = self.flow.current(self.sid)["state_version"]
        return self.client.post(
            path, json={"request_key": key, "expected_state_version": version, **extra}
        )

    def begin(self):
        self.client.post(self.exchange)
        value = self.flow.current(self.sid)
        prefix = "/api/v2/attempts/" + value["attempt_id"]
        self.assertEqual(self.command(prefix + "/prepare", "prepare").status_code, 200)
        self.assertEqual(self.command(prefix + "/start", "start").status_code, 200)
        return self.flow.current(self.sid)

    def test_cookie_path_exchange_source_header_and_private_cache(self):
        self.assertEqual(self.client.get("/api/v2/attempts/current").status_code, 401)
        response = self.client.post(self.exchange)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Path=/api/v2", response.headers["set-cookie"])
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=strict", response.headers["set-cookie"])
        self.assertEqual(self.client.get("/api/v2/attempts/current").status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(
            self.client.post(
                self.exchange, headers={"X-VibeEval-Request": "bad"}
            ).status_code,
            403,
        )
        fresh = TestClient(self.app, headers=HEADERS)
        self.addCleanup(fresh.close)
        self.assertEqual(fresh.post(self.exchange).status_code, 404)
        self.assertEqual(fresh.get("/api/v2/attempts/current").status_code, 401)

    def test_legacy_mutations_and_agent_routes_cannot_bypass_flow(self):
        status = self.client.get(ENTRY + "/" + self.sid + "/status")
        self.assertEqual(status.status_code, 409)
        self.assertEqual(status.json()["detail"]["code"], "EXAM_MODE_MISMATCH")
        agent = self.client.get(ENTRY + "/" + self.sid + "/agent/conversations")
        self.assertEqual(agent.status_code, 409)

    def test_phase_workflow_questions_drafts_submit_and_events(self):
        initial = self.begin()
        phase = initial["current_phase"]["id"]
        self.assertEqual(
            self.client.get(f"/api/v2/phases/{phase}/questions").status_code, 403
        )
        response = self.command(f"/api/v2/phases/{phase}/finish", "finish")
        self.assertEqual(response.status_code, 200)
        phase = response.json()["current_phase"]["id"]
        path = f"/api/v2/phases/{phase}/answers/item"
        self.assertEqual(
            self.client.put(
                path, json={"expected_revision": 0, "value": "answer"}
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.put(
                path, json={"expected_revision": 0, "value": "stale"}
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.get(f"/api/v2/phases/{phase}/questions").json()["items"][0][
                "id"
            ],
            "item",
        )
        stage = initial["current_stage"]["id"]
        receipt = self.command(f"/api/v2/stages/{stage}/submit", "submit").json()
        self.assertEqual(
            self.client.get(f"/api/v2/stages/{stage}/submission").json(), receipt
        )
        replay = self.command(f"/api/v2/stages/{stage}/submit", "submit", version=0)
        self.assertEqual(replay.json(), receipt)
        events = self.client.get(
            f"/api/v2/attempts/{initial['attempt_id']}/events"
        ).json()
        self.assertTrue(any(e["kind"] == "stage.submitted" for e in events["items"]))
        self.assertEqual(
            self.client.get(f"/api/v2/phases/{phase}/answers").status_code, 409
        )

    def test_ownership_and_candidate_config_injection_are_rejected(self):
        self.begin()
        self.assertEqual(
            self.command("/api/v2/attempts/not-owned/start", "evil").status_code, 404
        )
        stage = self.flow.current(self.sid)["stages"][1]["id"]
        self.assertEqual(
            self.command(f"/api/v2/stages/{stage}/start", "future").status_code, 409
        )
        self.assertEqual(self.client.get("/api/v2/stages/not-owned").status_code, 404)
        self.assertEqual(
            self.client.post(
                "/api/v2/attempts", json=definition().model_dump(mode="json")
            ).status_code,
            404,
        )
        phase = self.flow.current(self.sid)["current_phase"]["id"]
        response = self.command(f"/api/v2/phases/{phase}/finish", "bad", role="admin")
        self.assertEqual(response.status_code, 422)

    def test_disabled_flow_and_invalid_cookie_fail_without_implicit_adapter(self):
        self.app.state.exam_flow = None
        response = self.client.post(self.exchange)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "EXAM_FLOW_NOT_CONFIGURED")
        fresh = TestClient(self.app, headers=HEADERS)
        self.addCleanup(fresh.close)
        fresh.cookies.set(
            "vibeeval_exam_flow", self.client.cookies.get(ENTRY_COOKIE), path="/api/v2"
        )
        self.assertEqual(fresh.get("/api/v2/attempts/current").status_code, 503)

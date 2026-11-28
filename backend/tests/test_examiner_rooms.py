import os
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.admin import rooms
from app.admin.accounts import hash_password
from app.db.session import get_session_factory
from app.exam_flow import ExamDefinition, ExamFlowService, PostgresFlowStore
from app.exams.postgres import PostgresEntryStore
from app.main import create_app


class FixtureResources:
    def prepare(self, context, definition, operation_id):
        pass

    def activate_phase(self, context, definition, operation_id):
        pass

    def close_phase(self, context, operation_id):
        pass

    def seal(self, context, definition, operation_id):
        return {
            "workspace_id": context.workspace_id,
            "reference": "fixture",
            "sha256": "a" * 64,
        }


class FixtureQuestions:
    def questions(self, context, reference):
        return []

    def validate_answer(self, context, question, value):
        raise ValueError("No questions in this fixture")


@unittest.skipUnless(
    os.environ.get("VIBEEVAL_TEST_EXAMINER_ROOMS") == "1",
    "requires isolated migrated PostgreSQL",
)
class ExaminerRoomTests(unittest.TestCase):
    def test_create_admit_finish_and_lock(self):
        factory = get_session_factory()
        flow = ExamFlowService(
            PostgresFlowStore(factory), FixtureQuestions(), FixtureResources()
        )
        app = create_app(
            accounts={
                "examiner": {
                    "name": "Examiner",
                    "password_hash": hash_password("fixture-password"),
                }
            },
            entry_store=PostgresEntryStore(factory),
            exam_flow=flow,
            agent_enabled=False,
        )
        app.state.examiner_exam_definition = ExamDefinition.model_validate(
            {
                "key": "fixture",
                "version": "v1",
                "title": "Fixture test",
                "stages": [
                    {
                        "key": "one",
                        "title": "Code",
                        "task_version": "task",
                        "resource_ref": "fixture",
                        "submission_kind": "code",
                        "phases": [
                            {"key": "work", "title": "Code", "duration_seconds": 60}
                        ],
                    }
                ],
            }
        )
        header = {"X-VibeEval-Request": "1"}
        endpoint = "/api/v1/admin/r2/rooms"
        entry = "/api/v1/sessions"
        with patch.dict(os.environ, {"VIBEEVAL_ROOM_TOKEN_KEY": "f" * 64}):
            with (
                TestClient(app, headers=header) as admin,
                TestClient(app, headers=header) as candidate,
            ):
                self.assertEqual(candidate.get(endpoint).status_code, 401)
                result = admin.post(
                    "/api/v1/admin/login",
                    json={"username": "examiner", "password": "fixture-password"},
                )
                self.assertEqual(result.status_code, 200, result.text)
                listing = admin.get(endpoint)
                self.assertEqual(listing.status_code, 200, listing.text)
                origin = listing.json()["meta"]["current_template"]
                body = {
                    "candidate_name": "Fixture",
                    "admission_expires_at": int(time.time()) + 120,
                    "template_version": origin["version"],
                    "request_key": uuid4().hex,
                }
                result = admin.post(endpoint, json=body)
                self.assertEqual(result.status_code, 200, result.text)
                created = result.json()
                self.assertEqual(admin.post(endpoint, json=body).json(), created)
                self.assertEqual(
                    admin.post(
                        endpoint, json={**body, "candidate_name": "Other"}
                    ).status_code,
                    409,
                )
                path = endpoint + "/" + created["room"]["id"]
                self.assertEqual(
                    admin.post(path + "/token").json()["token"], created["token"]
                )
                self.assertNotIn(created["token"], admin.get(endpoint).text)
                candidate.get(entry + "/current")
                app.state.examiner_exam_definition = (
                    app.state.examiner_exam_definition.model_copy(
                        update={"version": "v2"}
                    )
                )
                result = candidate.post(
                    entry + "/verify-token", json={"token": created["token"]}
                )
                self.assertEqual(result.status_code, 200, result.text)
                sid = result.json()["session_id"]
                self.assertEqual(flow.store.load(sid).definition.version, "v1")
                self.assertEqual(admin.post(path + "/token").status_code, 409)
                state = flow.current(sid)
                state = flow.prepare(sid, state["state_version"], "prepare")
                stage = state["current_stage"]["id"]
                state = flow.start(sid, stage, state["state_version"], "start")
                self.assertEqual(admin.get(path).json()["exam_status"], "in_progress")
                review_path = path + "/stages/" + stage
                self.assertEqual(admin.get(review_path).status_code, 409)
                receipt = flow.submit(sid, stage, state["state_version"], "submit")
                self.assertEqual(admin.get(path).json()["exam_status"], "completed")
                self.assertEqual(
                    admin.get(review_path + "/timeline").json()["items"], []
                )
                report = {
                    "state": "scored",
                    "revision": "a" * 64,
                    "subjective": 20,
                    "maximum": 30,
                    "objective": {"score": 35, "cap": 50},
                }
                review = {
                    "action": "adjust",
                    "delta": 3,
                    "reason": "Evidence checked",
                    "score_revision": report["revision"],
                    "request_key": "review",
                }
                with patch.object(rooms, "scores", return_value=report):
                    self.assertEqual(
                        admin.post(
                            review_path + "/review", json={**review, "reason": " "}
                        ).status_code,
                        422,
                    )
                    result = admin.post(review_path + "/review", json=review)
                    self.assertEqual(result.status_code, 200, result.text)
                    self.assertEqual(result.json()["score"]["final"], 50)
                    self.assertEqual(result.json()["score"]["raw"], 58)
                    self.assertEqual(
                        admin.post(review_path + "/review", json=review).json(),
                        result.json(),
                    )
                    self.assertEqual(
                        admin.post(
                            review_path + "/review",
                            json={**review, "request_key": "another"},
                        ).status_code,
                        409,
                    )
                    self.assertEqual(
                        admin.get(review_path).json()["decision"], result.json()
                    )
                with self.assertRaisesRegex(Exception, "该题复核已锁定"):
                    with rooms.grade_write(
                        type("Resources", (), {"factory": factory})(),
                        created["room"]["id"],
                        receipt["id"],
                    ):
                        self.fail("locked")

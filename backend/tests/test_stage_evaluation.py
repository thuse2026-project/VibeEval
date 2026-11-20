import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.admin.accounts import hash_password
from app.main import create_app


class StageEvaluationTests(unittest.TestCase):
    def test_item_evidence_novelty_and_test_observations_are_separate_from_scores(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            app = create_app(
                accounts={
                    "teacher": {
                        "name": "Teacher",
                        "password_hash": hash_password("fixture-password"),
                    }
                },
                entry_db=folder / "entry.sqlite",
            )
            submission = "submission_" + "b" * 32
            endpoint = "/api/v1/admin/r2/submissions/" + submission
            metadata = {
                "submission_id": submission,
                "task_version": "task",
                "image": "sha256:" + "a" * 64,
            }
            exported = {
                "receipt": {"id": submission},
                "content": {"artifact": {"sha256": "snapshot"}},
                "files": [{"path": "tests/test_candidate.py", "content": "# empty"}],
            }
            resources = SimpleNamespace(snapshots=SimpleNamespace(root=folder))
            rule = {
                "fingerprint": "v2",
                "rule_version": "v2",
                "baseline": {"tests/test_candidate.py": "# empty"},
                "criteria": [],
                "manual": [
                    {
                        "key": "tests",
                        "name": "新增测试",
                        "max": 15,
                        "category": "tests",
                    },
                    {
                        "key": "quality",
                        "name": "质量",
                        "max": 15,
                        "category": "quality",
                    },
                ],
                "review_evidence_required": True,
            }
            observation = {
                "status": "completed",
                "test_sha256": hashlib.sha256(b"# empty").hexdigest(),
                "rows": [],
                "versions": {
                    v: {"status": "completed", "tests": [], "output": ""}
                    for v in ("original", "submitted")
                },
                "notice": "Untrusted evidence only",
            }
            with (
                TestClient(app) as client,
                patch(
                    "app.admin.stage_evaluation.context",
                    return_value=(metadata, exported, resources),
                ),
                patch("app.admin.stage_evaluation.rule_for", return_value=rule),
                patch(
                    "app.admin.stage_evaluation.compare_tests", return_value=observation
                ) as observer,
            ):
                headers = {"X-VibeEval-Request": "1"}
                client.post(
                    "/api/v1/admin/login",
                    json={"username": "teacher", "password": "fixture-password"},
                    headers=headers,
                )
                body = {
                    "expected_revision": 0,
                    "scores": {"tests": 5, "quality": 5},
                    "note": "逐项核查",
                }
                self.assertEqual(
                    client.post(
                        endpoint + "/review", json=body, headers=headers
                    ).status_code,
                    422,
                )
                body["evidence"] = {
                    "tests": "tests/test_candidate.py 新的边界",
                    "quality": "业务文件及注释",
                }
                self.assertEqual(
                    client.post(
                        endpoint + "/review", json=body, headers=headers
                    ).status_code,
                    422,
                )
                body["novel_tests_confirmed"] = True
                saved = client.post(endpoint + "/review", json=body, headers=headers)
                self.assertEqual(saved.status_code, 200, saved.text)
                self.assertEqual(
                    client.post(endpoint + "/candidate-tests", json={}).status_code, 403
                )
                response = client.post(
                    endpoint + "/candidate-tests", json={}, headers=headers
                )
                self.assertEqual(response.status_code, 200, response.text)
                data = client.get(endpoint).json()
                self.assertEqual(data["review"], saved.json())
                self.assertIsNone(data["total_score"])
                self.assertEqual(data["candidate_tests"]["status"], "completed")
                observer.return_value = {**observation, "status": "incomplete"}
                self.assertEqual(
                    client.post(
                        endpoint + "/candidate-tests", json={}, headers=headers
                    ).status_code,
                    200,
                )
                data = client.get(endpoint).json()
                self.assertEqual(data["candidate_tests"]["status"], "completed")
                self.assertEqual(
                    data["candidate_tests_last_attempt"]["status"], "incomplete"
                )
                self.assertEqual(data["review"], saved.json())

    def test_examiner_scores_persist_and_conflicts_or_failures_do_not_erase_results(
        self,
    ):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            app = create_app(
                accounts={
                    "teacher": {
                        "name": "Teacher",
                        "password_hash": hash_password("fixture-password"),
                    }
                },
                entry_db=folder / "entry.sqlite",
            )
            submission = "submission_" + "a" * 32
            endpoint = "/api/v1/admin/r2/submissions/" + submission
            metadata = {
                "submission_id": submission,
                "task_version": "task",
                "image": "sha256:" + "a" * 64,
                "title": "题目",
            }
            exported = {
                "receipt": {"id": submission},
                "content": {
                    "artifact": {"sha256": "snapshot-a"},
                    "summary": {"value": "说明"},
                },
                "files": [{"path": "a.py", "content": "answer"}],
            }
            resources = SimpleNamespace(snapshots=SimpleNamespace(root=folder))
            rule = {
                "fingerprint": "rule-a",
                "rule_version": "v1",
                "baseline": {"a.py": "starter"},
                "criteria": [
                    {"key": "behavior", "name": "功能", "max": 70, "core": True}
                ],
                "manual": [{"key": "quality", "name": "质量", "max": 30}],
                "failure_cap": 50,
            }
            objective = {
                "status": "completed",
                "score": 70,
                "max": 70,
                "groups": [
                    {
                        "key": "behavior",
                        "name": "功能",
                        "max": 70,
                        "passed": True,
                        "score": 70,
                    }
                ],
                "cap": None,
                "output": "trusted",
            }
            with (
                TestClient(app) as client,
                patch(
                    "app.admin.stage_evaluation.context",
                    return_value=(metadata, exported, resources),
                ),
                patch("app.admin.stage_evaluation.rule_for", return_value=rule),
                patch(
                    "app.admin.stage_evaluation.evaluate", return_value=objective
                ) as evaluator,
            ):
                self.assertEqual(client.get(endpoint).status_code, 401)
                headers = {"X-VibeEval-Request": "1"}
                self.assertEqual(
                    client.post(
                        "/api/v1/admin/login",
                        json={"username": "teacher", "password": "fixture-password"},
                        headers=headers,
                    ).status_code,
                    200,
                )
                self.assertIsNone(client.get(endpoint).json()["total_score"])
                self.assertEqual(
                    client.post(endpoint + "/evaluate", json={}).status_code, 403
                )
                review = {
                    "expected_revision": 0,
                    "scores": {"quality": 0},
                    "note": "未新增测试，显式给零分",
                }
                self.assertEqual(
                    client.post(
                        endpoint + "/review", json=review, headers=headers
                    ).status_code,
                    200,
                )
                self.assertIsNone(client.get(endpoint).json()["total_score"])
                self.assertEqual(
                    client.post(
                        endpoint + "/evaluate", json={}, headers=headers
                    ).status_code,
                    200,
                )
                self.assertEqual(client.get(endpoint).json()["total_score"], 70)
                self.assertEqual(
                    client.post(
                        endpoint + "/review", json=review, headers=headers
                    ).status_code,
                    409,
                )
                review.update(
                    expected_revision=1, scores={"quality": 30}, note="质量复核完成"
                )
                self.assertEqual(
                    client.post(
                        endpoint + "/review", json=review, headers=headers
                    ).status_code,
                    200,
                )
                self.assertEqual(client.get(endpoint).json()["total_score"], 100)
                self.assertEqual(
                    client.post(
                        endpoint + "/review",
                        json={
                            **review,
                            "expected_revision": 2,
                            "scores": {"quality": 31},
                        },
                        headers=headers,
                    ).status_code,
                    422,
                )
                evaluator.return_value = {
                    "status": "infrastructure_failed",
                    "score": None,
                    "groups": [],
                    "cap": None,
                }
                self.assertEqual(
                    client.post(
                        endpoint + "/evaluate", json={}, headers=headers
                    ).status_code,
                    200,
                )
                data = client.get(endpoint).json()
                self.assertEqual(data["total_score"], 100)
                self.assertEqual(
                    data["last_attempt"]["status"], "infrastructure_failed"
                )
                client.post("/api/v1/admin/logout", json={}, headers=headers)
                self.assertEqual(client.get(endpoint).status_code, 401)

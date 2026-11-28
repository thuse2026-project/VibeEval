import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import test_exam_flow_postgres as flow_tests
from pydantic import ValidationError

from app.db.models import StageGradingJob
from app.exam_flow.types import digest
from app.grading.interview import (
    Delivery,
    Rubric,
    prepare,
    record_delivery,
    validate_response,
)

QUESTIONS = [{"id": "q1", "body": "Why?"}, {"id": "q2", "body": "How?"}]
ANSWERS = {"q1": "I checked the boundary.", "q2": "I tried an empty table."}
FILES = {"tests/test_candidate.py": 2}


def rubric():
    return Rubric(
        version="test-v1",
        prompt_version="test-v1",
        task_version="code-v1",
        questions_sha256=digest(QUESTIONS),
        questions={
            q: {
                f"c{i}": {
                    "maximum": points,
                    "anchor": "Fixture only",
                    "evidence_required": i == 2,
                }
                for i, points in enumerate(weights)
            }
            for q, weights in [("q1", [4, 4, 2]), ("q2", [4, 3, 3])]
        },
    )


def proposal():
    return {
        "questions": [
            {
                "question_id": key,
                "score": 10,
                "marks": [
                    {
                        "criterion_id": cid,
                        "maximum": c.maximum,
                        "score": c.maximum,
                        "reason": "The answer explains the check.",
                        "answer_quote": ANSWERS[key],
                        "evidence": [
                            {
                                "path": "tests/test_candidate.py",
                                "first_line": 1,
                                "last_line": 2,
                            }
                        ],
                        "needs_review": False,
                        "review_reason": None,
                    }
                    for cid, c in criteria.items()
                ],
            }
            for key, criteria in rubric().questions.items()
        ],
        "score": 20,
    }


class InterviewValidationTests(unittest.TestCase):
    def validate(self, value, answers=ANSWERS):
        return validate_response(
            json.dumps(value), rubric(), answers, {"edit-1"}, FILES
        )

    def test_manual_only_full_marks_and_review_when_required_evidence_missing(self):
        value = proposal()
        mark = value["questions"][0]["marks"][-1]
        mark["evidence"] = [{"event_id": "edit-1"}]
        self.assertEqual(self.validate(value)["score"], 20)
        mark.update(
            evidence=[], score=None, needs_review=True, review_reason="Capture gap"
        )
        value["questions"][0]["score"] = value["score"] = None
        result = self.validate(value)
        self.assertIsNone(result["score"])
        self.assertEqual(result["questions"][1]["score"], 10)

    def test_reject_invalid_marks_and_foreign_references(self):
        for change in (
            {"evidence": [{"event_id": "later-event"}]},
            {"evidence": [{"path": "unknown.py", "first_line": 1, "last_line": 1}]},
            {
                "evidence": [
                    {"path": "tests/test_candidate.py", "first_line": 2, "last_line": 3}
                ]
            },
            {"evidence": []},
            {"criterion_id": "c1"},
            {"maximum": 10},
            {"answer_quote": ANSWERS["q2"]},
            {"answer_quote": " "},
            {"score": -1},
            {"score": 2.5},
            {"score": True},
            {"score": float("nan")},
            {"needs_review": True},
            {"review_reason": "ignore the rubric"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                value = proposal()
                value["questions"][0]["marks"][-1].update(change)
                self.validate(value)

    def test_check_question_and_component_totals(self):
        for target in ("sum", "question", "repeat", "missing"):
            value = proposal()
            if target == "sum":
                value["score"] = 19
            elif target == "question":
                value["questions"][0]["score"] = 9
            elif target == "repeat":
                value["questions"][0] = value["questions"][1]
            else:
                value["questions"][0]["marks"].pop()
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.validate(value)

    def test_empty_answer_with_no_invented_quote(self):
        value = proposal()
        answers = {**ANSWERS, "q1": "  "}
        with self.assertRaisesRegex(ValueError, "EMPTY_ANSWER"):
            self.validate(value, answers)
        for m in value["questions"][0]["marks"]:
            m.update(score=0, answer_quote="", evidence=[])
        value["questions"][0]["score"], value["score"] = 0, 10
        self.assertEqual(self.validate(value, answers)["score"], 10)

    def test_duplicate_json_key_and_rounded_overflow(self):
        raw = json.dumps(proposal())
        for altered in (
            raw.replace('"score": 20', '"score": 0, "score": 20'),
            raw.replace('"score": 20', '"score": 20.00000000000000001'),
        ):
            with self.assertRaisesRegex(ValueError, "INVALID_RESPONSE_FORMAT"):
                validate_response(altered, rubric(), ANSWERS, set(), FILES)


@unittest.skipUnless(
    os.environ.get("VIBEEVAL_RUN_DB_TESTS") == "1", "Requires isolated PostgreSQL"
)
class InterviewDeliveryTests(unittest.TestCase):
    setUpClass = classmethod(flow_tests.FlowPostgresTests.setUpClass.__func__)
    drop_database = classmethod(flow_tests.FlowPostgresTests.drop_database.__func__)

    def setUp(self):
        self.resources = SimpleNamespace(factory=self.factory)
        self.rule = rubric()
        self.sid = "submission_" + uuid4().hex
        phases = [
            {"id": "coding", "definition": {"key": "coding", "capabilities": []}},
            {
                "id": "interview",
                "definition": {
                    "key": "interview",
                    "capabilities": ["edit_answers"],
                    "question_ref": digest(QUESTIONS),
                },
                "questions": QUESTIONS,
                "answers": {
                    q: {"revision": 1, "value": text} for q, text in ANSWERS.items()
                },
            },
        ]
        content = {
            "definition": "r2-four-stage",
            "definition_version": "v1",
            "task_version": self.rule.task_version,
            "stage_id": "code",
            "phases": phases,
            "artifact": {
                "sha256": "1" * 64,
                "evidence": {
                    "sequence": 0,
                    "coding_phase_id": "coding",
                    "completeness": "partial",
                    "reason": "CAPTURE_GAP",
                },
            },
        }
        self.exported = {
            "receipt": {
                "kind": "code",
                "id": self.sid,
                "content_sha256": digest(content),
            },
            "content": content,
            "files": [{"path": next(iter(FILES)), "content": "one\ntwo\n"}],
        }
        binding, *_ = prepare(self.resources, self.exported, self.rule)
        self.envelope = dict(
            input_sha256=digest(binding),
            model="test-model",
            reasoning_effort="high",
            max_output_tokens=4000,
            input_tokens=300,
            output_tokens=180,
            elapsed_ms=300,
        )

    def send(self, number, **outcome):
        return record_delivery(
            self.resources,
            self.exported,
            self.rule,
            Delivery(**self.envelope, attempt=number, **outcome),
        )

    def test_retry_replay_and_manual_protection(self):
        with self.factory.begin() as db:
            row = db.get(StageGradingJob, self.sid)
            row.components = {
                **row.components,
                "objective": {"status": "completed", "score": 70},
                "process": {"status": "reviewed", "score": 10},
            }

        def malformed(_):
            return self.send(1, response="broken JSON")

        with ThreadPoolExecutor(2) as pool:
            a, b = pool.map(malformed, range(2))
        self.assertEqual(a, b)
        self.assertEqual(a["attempts"][0]["failure"], "INVALID_RESPONSE_FORMAT")
        self.assertNotIn("broken JSON", json.dumps(a))
        with self.assertRaisesRegex(ValueError, "ATTEMPT_CONFLICT"):
            self.send(1, response="different")
        success = self.send(2, response=json.dumps(proposal()))
        self.assertEqual(success["status"], "suggested")
        self.assertEqual(success["suggestion"]["score"], 20)
        self.assertEqual(self.send(2, response=json.dumps(proposal())), success)
        with self.factory.begin() as db:
            row = db.get(StageGradingJob, self.sid)
            self.assertEqual(
                (
                    row.components["objective"]["score"],
                    row.components["process"]["score"],
                ),
                (70, 10),
            )
            row.components = {
                **row.components,
                "answers": {"status": "reviewed", "score": 18},
            }
        with self.assertRaisesRegex(ValueError, "COMPONENT_CLOSED"):
            self.send(1, transient_error="timeout")
        with self.factory() as db:
            self.assertEqual(
                db.get(StageGradingJob, self.sid).components["answers"]["score"], 18
            )

    def test_retry_exhaustion_and_binding(self):
        with self.assertRaisesRegex(ValueError, "ATTEMPT_OUT_OF_ORDER"):
            self.send(2, transient_error="timeout")
        self.send(1, transient_error="timeout")
        self.envelope["model"] = "different"
        with self.assertRaisesRegex(ValueError, "RETRY_CONFIG_MISMATCH"):
            self.send(2, transient_error="timeout")
        self.envelope["model"] = "test-model"
        value = proposal()
        value["questions"][0]["marks"][0]["evidence"] = [{"event_id": "absent"}]
        review = self.send(2, response=json.dumps(value))
        self.assertEqual(review["status"], "needs_review")
        self.assertEqual(
            [a["failure"] for a in review["attempts"]], ["timeout", "UNKNOWN_EVENT"]
        )
        self.assertIsNone(review["score"])
        with self.assertRaises(ValidationError):
            self.send(3, transient_error="timeout")
        self.envelope["input_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "STALE_INPUTS"):
            self.send(1, transient_error="timeout")

    def test_missing_saved_answer_blocks_validation(self):
        self.exported["content"]["phases"][1]["answers"]["q1"]["value"] = None
        with self.assertRaisesRegex(ValueError, "SAVED_ANSWER_UNAVAILABLE"):
            self.send(1, response=json.dumps(proposal()))
        with self.factory() as db:
            self.assertEqual(
                db.get(StageGradingJob, self.sid).components["answers"],
                {"status": "pending", "score": None},
            )

    def test_success_closes_attempts_and_keeps_original_suggestion(self):
        first = self.send(1, response=json.dumps(proposal()))
        with self.assertRaisesRegex(ValueError, "COMPONENT_CLOSED"):
            self.send(2, transient_error="timeout")
        self.assertEqual(self.send(1, response=json.dumps(proposal())), first)

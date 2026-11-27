"""Regression tests for the flat model response and immutable grading evidence."""

import json
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from app.admin import four_stage
from app.db.models import ExamGradePublication
from app.grading.four_worker import propose, validate_text
from app.grading.interview import Rubric


class FourStageMarkTests(unittest.TestCase):
    criteria = [dict(key="R1.trigger", maximum=15, answer_id="findings")]
    answers = {"findings": {"reports": [{"trigger": "先查询，再写入，然后重复查询"}]}}

    def evaluate(self, **override):
        mark = dict(
            key="R1.trigger", score=15, reason="明确列出触发顺序", quote="再写入"
        )
        return validate_text(
            json.dumps({"marks": [{**mark, **override}], "review_note": ""}),
            self.criteria,
            self.answers,
        )

    def test_verbatim_report_field(self):
        self.assertEqual(self.evaluate()["marks"][0]["score"], 15)

    def test_unknown_key_overflow_and_invented_quote(self):
        for override in [dict(key="R1.other"), dict(score=16), dict(quote="伪造引文")]:
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.evaluate(**override)

    def test_interview_with_unverified_citations_never_offers_a_score(self):
        questions = ["q1", "q2"]
        rule = Rubric(
            version="test",
            prompt_version="test",
            task_version="test",
            questions_sha256="a" * 64,
            questions={
                q: {"check": dict(maximum=10, anchor="核对", evidence_required=True)}
                for q in questions
            },
        )
        row = SimpleNamespace(basis={}, components={"answers": {"status": "pending"}})
        db = MagicMock()
        db.get.return_value = row
        resources = SimpleNamespace(
            factory=SimpleNamespace(begin=lambda: nullcontext(db))
        )
        criteria = [
            dict(key=q + ".check", maximum=10, group="answers", answer_id=q)
            for q in questions
        ]
        flat = dict(
            marks=[
                dict(key=c["key"], score=5, quote="测试", reason="核对结果")
                for c in criteria
            ],
            review_note="",
        )
        client = SimpleNamespace(
            complete=AsyncMock(
                return_value={"choices": [{"message": {"content": json.dumps(flat)}}]}
            )
        )
        exported = dict(
            files=[],
            content={
                "artifact": {
                    "evidence": dict(
                        coding_phase_id="phase_test",
                        sequence=0,
                        completeness="complete",
                    )
                }
            },
        )
        loaded = (
            {"submission_id": "test"},
            exported,
            resources,
            {},
            questions,
            {q: "测试" for q in questions},
            criteria,
            rule,
        )
        with (
            patch("app.grading.four_worker.page", return_value=([], None)),
            patch("app.admin.stage_evaluation.rule_for", return_value={"baseline": {}}),
        ):
            for status in ("retryable", "needs_review"):
                result = propose(loaded, client)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["attempts"][-1]["failure"], "EVIDENCE_REQUIRED")
                self.assertIsNone(result["suggestion"])


class EvidenceReviewTests(unittest.TestCase):
    def setUp(self):
        self.key = "process.feedback"
        self.rubric = [
            dict(
                key=self.key,
                maximum=3,
                group="process",
                answer_id="",
                evidence_required=True,
            )
        ]
        self.row = SimpleNamespace(
            basis={"evidence": {"completeness": "partial"}},
            components={"objective": {"score": 49, "cap": 50, "status": "completed"}},
        )
        self.db = MagicMock()
        self.db.get.side_effect = lambda model, *args, **kw: (
            None if model is ExamGradePublication else self.row
        )
        self.db.scalar.return_value = SimpleNamespace(
            state_json={
                "status": "completed",
                "stages": [
                    {"definition": {"key": k, "title": k}, "receipt": {"id": k}}
                    for k in four_stage.STAGES
                ],
            }
        )
        factory = SimpleNamespace(begin=lambda: nullcontext(self.db))
        self.loaded = (
            {"session_id": "test"},
            {"files": [{"path": "tests/test_candidate.py"}]},
            SimpleNamespace(factory=factory),
            {},
            [],
            {},
            self.rubric,
            None,
        )

    def save(self, score, **fields):
        body = four_stage.Review(
            revision=four_stage.revision(self.row, self.rubric),
            scores={self.key: score},
            reason="复核冻结测试",
            gap_reviewed=True,
            **fields,
        )
        with patch.object(four_stage, "load", return_value=self.loaded):
            return four_stage.review(
                None, "test", body, SimpleNamespace(username="test")
            )

    def test_hold_survives_reload_and_verified_manual_source_can_resolve_it(self):
        result = self.save(
            None, evidence_notes={self.key: "运行记录不完整，待核查测试文件"}
        )
        self.assertIsNone(result["final"])
        self.assertFalse(four_stage.exam_state(self.db, "test")["ready"])
        result = self.save(
            3,
            evidence_notes={self.key: "新增的断言覆盖已复现的故障"},
            evidence_refs={self.key: "file:tests/test_candidate.py"},
        )
        self.assertEqual((result["raw"], result["cap"], result["final"]), (52, 50, 50))
        self.assertTrue(four_stage.exam_state(self.db, "test")["ready"])
        self.assertIsNone(self.row.components["review_history"][0]["final"])

    def test_blanket_acknowledgement_or_unknown_material_cannot_close_gap(self):
        for fields in [
            {},
            {"evidence_notes": {self.key: "已复核"}},
            {
                "evidence_notes": {self.key: "已复核"},
                "evidence_refs": {self.key: "file:other.py"},
            },
        ]:
            with self.subTest(fields=fields), self.assertRaises(HTTPException):
                self.save(3, **fields)

"""Regression tests for the flat model response and immutable grading evidence."""

import json
import unittest

from app.grading.four_worker import validate_text


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

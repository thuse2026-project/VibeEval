import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from app.admin.stage_evaluation import atomic_record, records, total_score
from app.grading.datasette import load_rule, trusted_tests

SOURCE = """
import tempfile,unittest
from pathlib import Path
from datasette.app import Datasette
class Behavior(unittest.IsolatedAsyncioTestCase):
    async def test_value(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'data.db';path.write_bytes(b'fixture')
            ds=Datasette([path])
            try:
                response=await ds.client.get('/data/items.json')
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.json()['rows'],[{'value':3}])
            finally:
                ds.close()
"""


class FakeChannel:
    def __init__(self, text):
        self.text = text

    def call(self, operation, app, **kwargs):
        return {"status_code": 200, "text": self.text} if operation == "get" else None


class GradingTests(unittest.TestCase):
    def rule(self):
        return {
            "criteria": [
                {
                    "key": "behavior",
                    "name": "行为",
                    "module": "private.py",
                    "case": "Behavior",
                    "method": "test_value",
                    "max": 70,
                    "core": True,
                }
            ],
            "manual": [{"key": "quality", "name": "质量", "max": 30}],
            "sources": {"private.py": SOURCE},
            "failure_cap": 50,
            "fingerprint": "rule-a",
        }

    def test_assertions_determine_points_not_candidate_claimed_score(self):
        wrong = trusted_tests(
            FakeChannel('{"score":100,"rows":[{"value":99}]}'), self.rule()
        )
        self.assertEqual(wrong["status"], "completed")
        self.assertEqual(wrong["score"], 0)
        self.assertEqual(wrong["cap"], 50)
        correct = trusted_tests(FakeChannel('{"rows":[{"value":3}]}'), self.rule())
        self.assertEqual(correct["score"], 70)
        self.assertIsNone(correct["cap"])

    def test_bundle_hashes_bind_test_sources_and_image(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            (folder / "private.py").write_bytes(SOURCE.encode("utf-8"))
            rule = {
                **self.rule(),
                "images": ["sha256:" + "a" * 64],
                "baseline": {"a.py": "original"},
                "writable": ["a.py"],
                "modules": {"private.py": hashlib.sha256(SOURCE.encode()).hexdigest()},
            }
            rule.pop("sources")
            (folder / "manifest.json").write_text(
                json.dumps({"schema_version": 1, "rules": {"task": rule}}),
                encoding="utf-8",
            )
            loaded = load_rule(folder, "task", rule["images"][0])
            self.assertEqual(loaded["sources"]["private.py"], SOURCE)
            with self.assertRaises(ValueError):
                load_rule(folder, "task", "sha256:" + "b" * 64)
            (folder / "private.py").write_text("changed", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_rule(folder, "task", rule["images"][0])

    def test_host_fixture_errors_are_ungraded_but_bad_candidate_json_is_scored(self):
        for error in (
            "OSError('fixture unavailable')",
            "RuntimeError('broken fixture')",
            "AttributeError('adapter bug')",
        ):
            rule = self.rule()
            rule["sources"] = {
                "private.py": SOURCE.replace(
                    "with tempfile.TemporaryDirectory() as root:",
                    "raise "
                    + error
                    + "\n        with tempfile.TemporaryDirectory() as root:",
                )
            }
            result = trusted_tests(FakeChannel('{"rows":[{"value":3}]}'), rule)
            self.assertEqual(result["status"], "infrastructure_failed")
            self.assertIsNone(result["score"])
        result = trusted_tests(FakeChannel("invalid json"), self.rule())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["score"], 0)
        for malformed in (
            "{}",
            "[]",
            "null",
            '{"facet_results":null}',
            '{"rows":null}',
        ):
            result = trusted_tests(FakeChannel(malformed), self.rule())
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["score"], 0)
        rule = self.rule()
        rule["sources"] = {
            "private.py": SOURCE.replace(
                "with tempfile.TemporaryDirectory() as root:",
                "with self.subTest(case='fixture'):\n            raise OSError('fixture unavailable')\n        with tempfile.TemporaryDirectory() as root:",
            )
        }
        result = trusted_tests(FakeChannel('{"rows":[{"value":3}]}'), rule)
        self.assertEqual(result["status"], "infrastructure_failed")
        self.assertIsNone(result["score"])

    def test_unreviewed_is_not_zero_and_historical_bug_cap_is_applied(self):
        objective = {"status": "completed", "score": 60, "cap": 50}
        self.assertIsNone(total_score(objective, None))
        self.assertIsNone(total_score(None, {"scores": {"quality": 30}}))
        self.assertEqual(total_score(objective, {"scores": {"quality": 30}}), 50)
        self.assertEqual(
            total_score({**objective, "cap": None}, {"scores": {"quality": 0}}), 60
        )

    def test_saved_scores_for_a_different_snapshot_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            atomic_record(
                folder / "review.json",
                {
                    "snapshot_sha256": "another",
                    "rule_sha256": "rule-a",
                    "revision": 1,
                    "scores": {"quality": 30},
                    "note": "checked",
                },
            )
            with self.assertRaises(HTTPException) as failure:
                records(
                    folder,
                    {"content": {"artifact": {"sha256": "current"}}},
                    {"submission_id": "submission-a"},
                    self.rule(),
                )
            self.assertEqual(failure.exception.status_code, 503)

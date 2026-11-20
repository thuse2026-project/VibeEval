import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.grading.candidate_tests import WORKER, compare_tests, possible_copies
from app.grading.rubrics import manual_rubric


class CandidateEvidenceTests(unittest.TestCase):
    def test_worker_distinguishes_assertions_errors_skips_and_empty_suites(self):
        # Only this trusted synthetic fixture runs locally. Real candidate code is
        # exercised separately in runsc, never imported in the host process.
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "test_candidate.py"
            path.write_text(
                """import unittest
class Example(unittest.TestCase):
    def test_pass(self):self.assertEqual(2,2)
    def test_fail(self):self.assertEqual(1,2)
    def test_error(self):raise ValueError('broken fixture')
    @unittest.skip('not exercised')
    def test_skip(self):pass
    def test_sub(self):
        with self.subTest(item=1):self.assertEqual(1,2)
""",
                encoding="utf-8",
            )
            worker = WORKER.replace("['/workspace','/workspace/tests']", repr([root]))
            result = subprocess.run(
                [sys.executable, "-I", "-u", "-c", worker],
                input=json.dumps({"id": "probe", "op": "candidate_tests"}) + "\n",
                text=True,
                capture_output=True,
                timeout=10,
                check=True,
            )
            rows = json.loads(result.stdout)["value"]["tests"]
            statuses = {row["id"].split(".")[-1]: row["status"] for row in rows}
            self.assertEqual(statuses["test_pass"], "passed")
            self.assertEqual(statuses["test_fail"], "failed")
            self.assertEqual(statuses["test_error"], "error")
            self.assertEqual(statuses["test_skip"], "skipped")
            self.assertEqual(statuses["test_sub"], "failed")
            path.write_text("# no tests\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-I", "-u", "-c", worker],
                input=json.dumps({"id": "probe", "op": "candidate_tests"}) + "\n",
                text=True,
                capture_output=True,
                timeout=10,
                check=True,
            )
            self.assertEqual(json.loads(result.stdout)["value"]["tests"], [])
            path.write_text(
                """import unittest
class Many(unittest.TestCase):
    def test_many(self):
        for i in range(205):
            with self.subTest(i=i):self.assertEqual(i,-1)
""",
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, "-I", "-u", "-c", worker],
                input=json.dumps({"id": "probe", "op": "candidate_tests"}) + "\n",
                text=True,
                capture_output=True,
                timeout=10,
                check=True,
            )
            value = json.loads(result.stdout)["value"]
            self.assertTrue(value["tests_truncated"])
            self.assertLessEqual(len(value["tests"]), 200)

    def test_comparison_only_swaps_business_code_and_never_scores(self):
        baseline = {
            "app.py": "bug",
            "tests/test_candidate.py": "# empty",
            "tests/test_public.py": "# public",
        }
        rule = {"baseline": baseline, "writable": ["app.py", "tests/test_candidate.py"]}
        files = [
            {
                "path": p,
                "content": (
                    "fixed"
                    if p == "app.py"
                    else "new test" if p == "tests/test_candidate.py" else c
                ),
            }
            for p, c in baseline.items()
        ]
        observed = []

        def fake(files, *args, **kwargs):
            observed.append({f["path"]: f["content"] for f in files})
            return {
                "status": "completed",
                "output": "",
                "tests": [
                    {
                        "id": "test_candidate.X.test_case",
                        "status": "failed" if len(observed) == 1 else "passed",
                        "message": "",
                    }
                ],
            }

        with patch("app.grading.candidate_tests.evaluate", side_effect=fake):
            result = compare_tests(files, rule, "image", "docker")
        self.assertEqual(observed[0]["app.py"], "bug")
        self.assertEqual(observed[1]["app.py"], "fixed")
        self.assertEqual(
            observed[0]["tests/test_candidate.py"],
            observed[1]["tests/test_candidate.py"],
        )
        self.assertEqual(result["rows"][0]["original"]["status"], "failed")
        self.assertEqual(result["rows"][0]["submitted"]["status"], "passed")
        self.assertNotIn("score", result)

    def test_copy_hint_ignores_names_but_not_new_assertions(self):
        public = "def test_old(self):\n    self.assertEqual(1,2)\n"
        self.assertEqual(
            possible_copies(public.replace("test_old", "test_renamed"), public),
            ["test_renamed"],
        )
        self.assertEqual(possible_copies(public.replace("(1,2)", "(1,3)"), public), [])

    def test_rubric_preserves_weights_and_provides_all_anchors(self):
        for task, test_max in [("column-statistics", 15), ("facet-pagination", 20)]:
            rubric = manual_rubric(task)
            self.assertEqual(sum(c["max"] for c in rubric), 30)
            self.assertEqual(
                sum(c["max"] for c in rubric if c["category"] == "tests"), test_max
            )
            self.assertTrue(
                all(c["full"] and c["partial"] and c["zero"] for c in rubric)
            )

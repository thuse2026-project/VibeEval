import json
import runpy
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.admin.accounts import hash_password
from app.admin.evaluation import (
    RULE,
    TEMPLATE,
    baseline_files,
    changes,
    latest_report,
    validate_result,
)
from app.grading.runner import RPC, CandidateFailure, trusted_tests
from app.main import create_app


class LocalTrustedRPC:
    """Only known repository fixtures run locally in unit tests, never submissions."""

    def __init__(self, service):
        self.service, self.objects = service, {}

    def call(self, key, method, *args):
        if method == "create":
            self.objects[key] = self.service()
            return None
        return getattr(self.objects[key], method)(*args)


class EvaluationTests(unittest.TestCase):
    def test_original_reference_and_subtest_denominator(self):
        baseline = baseline_files()
        original = runpy.run_path(str(TEMPLATE / "src/booking.py"))["BookingService"]
        reference = runpy.run_path(
            str(TEMPLATE.parents[1] / "maintainer/python-debug-01/reference/booking.py")
        )["BookingService"]

        class NoOverlap(original):
            def reserve(self, *args):
                self._bookings.clear()
                return super().reserve(*args)

        for service, passed in ((original, 6), (reference, 8), (NoOverlap, 7)):
            report = trusted_tests(LocalTrustedRPC(service), baseline)
            validate_result(report)
            self.assertEqual(
                (report["passed"], report["score"]), (passed, passed * 7.5)
            )

    def test_diff_is_factual_and_large_files_bounded(self):
        items = changes(
            [
                {"path": "a.py", "content": "new\n"},
                {"path": "b.py", "content": "test\n"},
            ],
            {"a.py": "old\n", "c.py": "removed"},
        )
        self.assertEqual([v["kind"] for v in items], ["modified", "added", "deleted"])
        self.assertIn("-old", items[0]["diff"])
        self.assertIn("+new", items[0]["diff"])
        self.assertEqual(changes([{"path": "a", "content": "same"}], {"a": "same"}), [])
        self.assertTrue(
            changes([{"path": "a", "content": "x" * 50000}], {"a": "old"})[0][
                "truncated"
            ]
        )

    def test_terminal_text_cannot_supply_a_score(self):
        class FakeSocket:
            def settimeout(self, value):
                pass

            def sendall(self, value):
                raw = b"Ran 8 tests\nOK\n"
                self.data = bytes([1, 0, 0, 0]) + struct.pack(">I", len(raw)) + raw

            def recv(self, n):
                value, self.data = self.data[:n], self.data[n:]
                return value

        with self.assertRaises(CandidateFailure):
            RPC(FakeSocket()).call("a", "create")

    def test_failed_retry_retains_success_and_snapshot_binding(self):
        baseline = baseline_files()
        original = runpy.run_path(str(TEMPLATE / "src/booking.py"))["BookingService"]
        report = trusted_tests(LocalTrustedRPC(original), baseline)
        report.update(
            submission_id="sub_1",
            snapshot_id="snp_1",
            rule_version=RULE,
            baseline_sha256="fingerprint",
        )
        with tempfile.TemporaryDirectory() as directory:
            reader = SimpleNamespace(store=SimpleNamespace(root=Path(directory)))
            root = Path(directory) / "evaluations"
            root.mkdir()
            (root / "sub_1-1.json").write_text(json.dumps(report))
            failure = {**report, "status": "infrastructure_failed", "score": None}
            (root / "sub_1-2.json").write_text(json.dumps(failure))
            previous, last = latest_report(
                reader,
                {"submission_id": "sub_1", "snapshot_id": "snp_1"},
                "fingerprint",
            )
            self.assertEqual(previous["score"], 45)
            self.assertEqual(last["status"], "infrastructure_failed")
            self.assertEqual(
                latest_report(
                    reader,
                    {"submission_id": "sub_1", "snapshot_id": "other"},
                    "fingerprint",
                ),
                (None, None),
            )

    def test_authentication_and_browser_header(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                accounts={
                    "examiner": {
                        "name": "test",
                        "password_hash": hash_password("testing-password"),
                    }
                },
                entry_db=Path(directory) / "entry.sqlite",
            )
            with TestClient(app) as client:
                path = "/api/v1/admin/submissions/sub_1/evaluation"
                self.assertEqual(client.get(path).status_code, 401)
                self.assertEqual(
                    client.post(path, headers={"X-VibeEval-Request": "1"}).status_code,
                    401,
                )
                client.post(
                    "/api/v1/admin/login",
                    headers={"X-VibeEval-Request": "1"},
                    json={"username": "examiner", "password": "testing-password"},
                )
                self.assertEqual(client.post(path).status_code, 403)
                self.assertEqual(client.get(path).status_code, 503)

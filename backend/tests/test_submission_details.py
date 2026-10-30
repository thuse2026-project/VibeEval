import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.admin.accounts import hash_password
from app.admin.demo import demo_submission
from app.admin.submissions import get_submission_reader
from app.exams.store import EntryStore
from app.main import create_app

BASE = "/api/v1/admin"
HEADERS = {"X-VibeEval-Request": "1"}


class TestReader:
    """Test double only: production persistence remains the teammate's responsibility."""

    def __init__(self, record):
        self.record = record
        self.records = [record]

    def list_submissions(self):
        return self.records

    def get_submission(self, submission_id):
        return self.record if self.record and self.record.get("submission_id") == submission_id else None


class SubmissionDetailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.accounts = {"examiner": {"name": "测试考官", "password_hash": hash_password("testing-password")}}

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "entry.sqlite3"
        self.app = create_app(self.accounts, entry_db=self.path)
        self.client = TestClient(self.app, headers=HEADERS)
        self.assertEqual(self.client.post(BASE + "/login", json={
            "username": "examiner", "password": "testing-password",
        }).status_code, 200)
        self.record = {
            "submission_id": "sub_from_persistence", "session_id": "ses_from_persistence",
            "candidate_id": "student-1", "candidate_name": "真实读取测试考生",
            "exam_id": "exam-1", "exam_title": "快照读取契约测试",
            "submitted_at": int(time.time()), "admitted_at": None, "started_at": None,
            "snapshot_id": "snapshot-fixed-1", "public_test_output": "2 passed\n",
            "files": [{"path": "src/main.py", "content": "print('fixed snapshot')\n"}],
        }

    def connect_reader(self, record=None):
        reader = TestReader(copy.deepcopy(self.record if record is None else record))
        self.app.dependency_overrides[get_submission_reader] = lambda: reader
        return reader

    def test_examiner_auth_required_for_both_sources(self):
        anonymous = TestClient(self.app)
        for source in ("live", "demo"):
            response = anonymous.get(BASE + f"/submissions/demo-submission-4?source={source}")
            self.assertEqual(response.status_code, 401)
        candidate = TestClient(self.app, headers=HEADERS)
        candidate.get("/api/v1/sessions/current")
        self.assertEqual(candidate.get(BASE + "/submissions/demo-submission-4?source=demo").status_code, 401)

    def test_demo_identity_and_times_match_overview_and_are_stable(self):
        rows = self.client.get(BASE + "/overview?source=demo&status=submitted").json()["items"]
        for row in rows:
            response = self.client.get(BASE + f"/submissions/{row['submission_id']}?source=demo")
            self.assertEqual(response.status_code, 200)
            detail = response.json()
            for field in ("submission_id", "session_id", "candidate_id", "candidate_name", "exam_id", "exam_title", "submitted_at"):
                self.assertEqual(detail[field], row[field])
            self.assertEqual(detail, self.client.get(BASE + f"/submissions/{row['submission_id']}?source=demo").json())
            self.assertEqual(detail["source"], "demo")
            self.assertIn("虚构", detail["public_test_output"])
            self.assertEqual({item["path"] for item in detail["files"]}, {
                "README.md", "requirements.txt", "src/booking.py", "src/main.py", "tests/test_public.py",
            })
            self.assertEqual(response.headers["cache-control"], "no-store")

    def test_source_isolation_and_missing_service_are_explicit(self):
        self.assertEqual(self.client.get(BASE + "/submissions/demo-submission-4").status_code, 503)
        self.assertEqual(self.client.get(BASE + "/submissions/demo-submission-4?source=live").status_code, 503)
        self.assertEqual(self.client.get(BASE + "/submissions/sub_real?source=demo").status_code, 404)
        self.assertEqual(self.client.get(BASE + "/submissions/demo-submission-4?source=unknown").status_code, 422)

    def test_live_reader_and_sensitive_field_projection(self):
        record = {**self.record, "access_token": "never-return-this", "disk_path": "/private/data"}
        self.connect_reader(record)
        response = self.client.get(BASE + "/submissions/sub_from_persistence")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["source"], "live")
        self.assertEqual(response.json()["snapshot_id"], "snapshot-fixed-1")
        self.assertNotIn("never-return-this", response.text)
        self.assertNotIn("disk_path", response.text)
        listed = self.client.get(BASE + "/overview").json()
        self.assertTrue(listed["submission_service_connected"])
        self.assertEqual(listed["summary"], {"total": 1, "not_submitted": 0, "submitted": 1})
        self.assertNotIn("files", listed["items"][0])
        self.assertNotIn("access_token", str(listed))

    def test_submission_merges_existing_admission_without_duplicate(self):
        store = EntryStore(self.path)
        token, _ = store.invite("student-1", "原入场姓名", "exam-1", "原场次名", 60, 24)
        with patch("app.exams.store.time.time", return_value=time.time() - 60):
            admission = store.admit(token, "test-browser")
        self.record["session_id"] = admission["session_id"]
        self.connect_reader()
        result = self.client.get(BASE + "/overview").json()
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["admitted_at"], admission["admitted_at"])
        self.assertEqual(result["items"][0]["submission_id"], self.record["submission_id"])
        self.assertEqual(result["items"][0]["status"], "submitted")

    def test_historical_submission_does_not_write_entry_database(self):
        self.connect_reader()
        self.assertEqual(self.client.get(BASE + "/overview").json()["total"], 1)
        self.app.dependency_overrides.clear()
        self.assertEqual(self.client.get(BASE + "/overview").json()["total"], 0)

    def test_duplicate_list_records_rejected(self):
        reader = self.connect_reader()
        reader.records = [self.record, {**self.record, "submission_id": "another-submission"}]
        self.assertEqual(self.client.get(BASE + "/overview").status_code, 502)
        reader.records = [self.record, {**self.record, "candidate_id": "another-candidate"}]
        self.assertEqual(self.client.get(BASE + "/overview").status_code, 502)

    def test_invalid_detail_fields_rejected_without_leaking_exceptions(self):
        reader = self.connect_reader()
        for files in (
            [{"path": "../secret", "content": "never-read-a-disk-path"}],
            [{"path": "/absolute", "content": "x"}],
            [{"path": "C:\\secret", "content": "x"}],
            [{"path": "src//main.py", "content": "x"}],
            [{"path": "same.py", "content": "a"}, {"path": "same.py", "content": "b"}],
        ):
            with self.subTest(files=files):
                reader.record = {**self.record, "files": files}
                response = self.client.get(BASE + "/submissions/sub_from_persistence")
                self.assertEqual(response.status_code, 502)
                self.assertNotIn("never-read", response.text)

    def test_wrong_submission_identity_is_rejected(self):
        reader = self.connect_reader()
        reader.get_submission = lambda _: {**self.record, "submission_id": "wrong-identity"}
        self.assertEqual(self.client.get(BASE + "/submissions/sub_from_persistence").status_code, 502)

    def test_unavailable_reader_is_retryable_without_secret_leaks(self):
        reader = self.connect_reader()

        def unavailable(*_):
            raise RuntimeError("secret database password")

        reader.get_submission = unavailable
        reader.list_submissions = unavailable
        for path in ("/submissions/sub_from_persistence", "/overview"):
            response = self.client.get(BASE + path)
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("secret", response.text)

    def test_unknown_record_returns_not_found_with_connected_reader(self):
        self.connect_reader()
        self.assertEqual(self.client.get(BASE + "/submissions/sub_missing").status_code, 404)

    def test_empty_files_and_missing_or_empty_output_preserved(self):
        reader = self.connect_reader()
        for files, output in (([], None), ([{"path": "empty.py", "content": ""}], "")):
            reader.record = {**self.record, "files": files, "public_test_output": output}
            response = self.client.get(BASE + "/submissions/sub_from_persistence")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["files"], files)
            self.assertEqual(response.json()["public_test_output"], output)

    def test_code_and_output_are_returned_as_data_not_interpreted(self):
        malicious = "<script>window.injected = true</script>"
        self.connect_reader({**self.record, "files": [{"path": "test.py", "content": malicious}], "public_test_output": malicious})
        response = self.client.get(BASE + "/submissions/sub_from_persistence")
        self.assertEqual(response.json()["files"][0]["content"], malicious)
        self.assertEqual(response.json()["public_test_output"], malicious)
        self.assertIn("application/json", response.headers["content-type"])

    def test_no_submission_write_routes_added(self):
        for method in ("post", "put", "delete"):
            self.assertEqual(getattr(self.client, method)(BASE + "/submissions/sub_from_persistence").status_code, 405)
        self.assertEqual(self.client.post("/api/v1/sessions/ses_missing/submit").status_code, 404)

    def test_demo_does_not_call_a_connected_but_broken_real_reader(self):
        reader = self.connect_reader()
        reader.list_submissions = lambda: (_ for _ in ()).throw(RuntimeError("broken"))
        reader.get_submission = reader.list_submissions
        self.assertEqual(self.client.get(BASE + "/overview?source=demo").status_code, 200)
        self.assertEqual(self.client.get(BASE + "/submissions/demo-submission-4?source=demo").status_code, 200)


if __name__ == "__main__":
    unittest.main()

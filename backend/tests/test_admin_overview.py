import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.admin.accounts import hash_password
from app.exams.store import EntryStore, RECOVERY_SECONDS
from app.main import create_app

URL = "/api/v1/admin/overview"
HEADERS = {"X-VibeEval-Request": "1"}


class AdminOverviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.accounts = {"examiner": {"name": "测试考官", "password_hash": hash_password("testing-password")}}

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "entry.sqlite3"
        self.store = EntryStore(self.path)
        self.app = create_app(self.accounts, entry_db=self.path)
        self.client = TestClient(self.app, headers=HEADERS)
        response = self.client.post("/api/v1/admin/login", json={"username": "examiner", "password": "testing-password"})
        self.assertEqual(response.status_code, 200)

    def invite(self, candidate="s1", exam="e1"):
        return self.store.invite(candidate, f"考生{candidate}", exam, f"考核{exam}", 60, 1)[0]

    def test_authentication_required_for_live_and_demo(self):
        anonymous = TestClient(self.app)
        self.assertEqual(anonymous.get(URL).status_code, 401)
        self.assertEqual(anonymous.get(URL + "?source=demo").status_code, 401)
        candidate = TestClient(self.app, headers=HEADERS)
        candidate.get("/api/v1/sessions/current")
        candidate.post("/api/v1/sessions/verify-token", json={"token": self.invite()})
        self.assertEqual(candidate.get(URL).status_code, 401)

    def test_empty_live_never_uses_demo_data(self):
        response = self.client.get(URL)
        data = response.json()
        self.assertEqual(data["source"], "live")
        self.assertEqual(data["items"], [])
        self.assertEqual(data["exams"], [])
        self.assertEqual(data["summary"], {"total": 0, "not_submitted": 0, "submitted": 0})
        self.assertFalse(data["submission_service_connected"])
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_real_admission_updates_one_row_without_claiming_started_or_submitted(self):
        token = self.invite()
        invited = self.client.get(URL).json()
        self.assertEqual(invited["total"], 1)
        self.assertEqual(invited["items"][0]["status"], "invited")
        self.assertIsNone(invited["items"][0]["admitted_at"])
        candidate = TestClient(self.app, headers=HEADERS)
        candidate.get("/api/v1/sessions/current")
        self.assertEqual(candidate.post("/api/v1/sessions/verify-token", json={"token": token}).status_code, 200)
        admitted = self.client.get(URL).json()
        self.assertEqual(admitted["total"], 1)
        row = admitted["items"][0]
        self.assertEqual(row["status"], "admitted")
        self.assertIsNotNone(row["admitted_at"])
        for key in ("started_at", "submitted_at", "submission_id"):
            self.assertIsNone(row[key])
        self.assertNotIn(token, str(admitted))
        self.assertNotIn("hash", str(admitted))

    def test_real_expiration_statuses(self):
        token = self.invite()
        self.invite("s2")
        self.store.admit(token, "browser-one")
        now = int(time.time())
        with patch("app.admin.overview.time.time", return_value=now + 3601):
            rows = self.client.get(URL).json()["items"]
        self.assertEqual({row["status"] for row in rows}, {"admitted", "invitation_expired"})
        # Keep the examiner session alive in this time-shifted check; only overview's provider is called.
        from app.admin.overview import read_live_records
        with patch("app.admin.overview.time.time", return_value=now + RECOVERY_SECONDS + 1):
            rows = read_live_records(self.store)
        self.assertEqual({row["status"] for row in rows}, {"recovery_expired", "invitation_expired"})

    def test_demo_filter_statistics_pagination_and_isolation(self):
        demo = self.client.get(URL + "?source=demo").json()
        self.assertEqual(demo["total"], 12)
        self.assertEqual(len(demo["items"]), 10)
        self.assertEqual(demo["summary"], {"total": 12, "not_submitted": 9, "submitted": 3})
        second = self.client.get(URL + "?source=demo&page=2").json()
        self.assertEqual(len(second["items"]), 2)
        filtered = self.client.get(URL + "?source=demo&exam_id=demo-exam-A&status=submitted").json()
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["summary"], {"total": 6, "not_submitted": 5, "submitted": 1})
        self.assertEqual(filtered["items"][0]["status"], "submitted")
        self.assertIsNotNone(filtered["items"][0]["submission_id"])
        self.assertEqual(self.client.get(URL).json()["items"], [])

    def test_real_filters_and_deterministic_page_order(self):
        for index in range(15):
            self.invite(f"s{index:02d}", "e1" if index < 12 else "e2")
        first = self.client.get(URL).json()
        second = self.client.get(URL + "?page=2").json()
        self.assertEqual(first["total"], 15)
        self.assertEqual(len(second["items"]), 5)
        keys = lambda data: {(item["exam_id"], item["candidate_id"]) for item in data["items"]}
        self.assertFalse(keys(first) & keys(second))
        filtered = self.client.get(URL + "?exam_id=e2").json()
        self.assertEqual(filtered["summary"]["total"], 3)
        self.assertEqual(filtered["total"], 3)
        empty = self.client.get(URL + "?status=submitted").json()
        self.assertEqual(empty["items"], [])
        self.assertEqual(empty["summary"]["total"], 15)
        clamped = self.client.get(URL + "?page=999").json()
        self.assertEqual(clamped["page"], 2)

    def test_read_provider_supports_submission_service_contract(self):
        self.invite()
        record = self.client.get(URL).json()["items"][0]
        record.update({"status": "submitted", "session_id": "ses_from_service",
                       "admitted_at": 100, "started_at": 200,
                       "submitted_at": 300, "submission_id": "sub_from_service"})
        with patch("app.admin.overview.read_live_records", return_value=[record]):
            result = self.client.get(URL + "?status=submitted").json()
        self.assertEqual(result["summary"]["submitted"], 1)
        self.assertEqual(result["items"][0]["submission_id"], "sub_from_service")

    def test_recent_activity_order_and_cross_exam_participation_count(self):
        older = self.invite("same", "e1")
        newer = self.invite("same", "e2")
        now = int(time.time())
        with patch("app.exams.store.time.time", return_value=now - 60):
            self.store.admit(older, "old-browser")
        self.store.admit(newer, "new-browser")
        result = self.client.get(URL).json()
        self.assertEqual(result["summary"]["total"], 2)
        self.assertEqual(result["items"][0]["exam_id"], "e2")

    def test_invalid_query_and_expired_examiner_session(self):
        for query in ("source=unknown", "status=unknown", "page=0", "page_size=101"):
            self.assertEqual(self.client.get(URL + "?" + query).status_code, 422)
        now = time.time()
        with patch("app.admin.router.time.time", return_value=now + 10801):
            self.assertEqual(self.client.get(URL).status_code, 401)


if __name__ == "__main__":
    unittest.main()

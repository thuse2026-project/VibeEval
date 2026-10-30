import io
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.exams.provision import main
from app.exams.router import COOKIE_NAME
from app.exams.store import EntryStore, RECOVERY_SECONDS
from app.main import create_app

HEADERS = {"X-VibeEval-Request": "1"}
BASE = "/api/v1/sessions"


class CandidateEntryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "entry.sqlite3"
        self.store = EntryStore(self.path)
        self.app = create_app({}, entry_db=self.path)
        self.client = self.new_browser()
        self.token, self.expires = self.invite()

    def new_browser(self, app=None):
        client = TestClient(app or self.app, headers=HEADERS)
        initialized = client.get(BASE + "/current")
        self.assertEqual(initialized.status_code, 401)
        return client

    def invite(self, candidate="student-01", valid_hours=24):
        return self.store.invite(candidate, "测试考生", "exam-01", "入场联调考核", 60, valid_hours)

    def admit(self, client=None, token=None):
        return (client or self.client).post(BASE + "/verify-token", json={"token": token or self.token})

    def test_bootstrap_and_success_do_not_start_exam(self):
        response = self.admit()
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "admitted")
        self.assertEqual(data["candidate"]["name"], "测试考生")
        self.assertEqual(data["exam_info"]["duration_minutes"], 60)
        self.assertIsNone(data["started_at"])
        self.assertIsNone(data["deadline_at"])
        self.assertEqual(self.client.get(BASE + "/current").json(), data)
        self.assertEqual(self.client.get(BASE + "/" + data["session_id"] + "/status").json(), data)
        public_text = response.text
        self.assertNotIn(self.token, public_text)
        self.assertNotIn("hash", public_text)
        self.assertNotIn(self.token.encode(), self.path.read_bytes())
        self.assertNotIn(self.client.cookies.get(COOKIE_NAME).encode(), self.path.read_bytes())

    def test_same_browser_retry_after_lost_response(self):
        first = self.admit().json()
        second = self.admit().json()
        self.assertEqual(first, second)
        self.assertEqual(self.client.get(BASE + "/current").json(), first)

    def test_cookie_initialized_before_claim_and_never_rotated(self):
        original = self.client.cookies.get(COOKIE_NAME)
        before = self.client.get(BASE + "/current")
        self.assertNotIn("set-cookie", before.headers)
        self.admit()
        after = self.client.get(BASE + "/current")
        self.assertNotIn("set-cookie", after.headers)
        self.assertEqual(self.client.cookies.get(COOKIE_NAME), original)
        fresh = TestClient(self.app, headers=HEADERS)
        response = fresh.get(BASE + "/current")
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=strict", response.headers["set-cookie"])
        self.assertIn("Max-Age=2592000", response.headers["set-cookie"])

    def test_missing_cookie_does_not_consume_token(self):
        fresh = TestClient(self.app, headers=HEADERS)
        self.assertEqual(self.admit(fresh).status_code, 428)
        self.assertEqual(self.admit().status_code, 200)

    def test_invalid_and_expired_invitations(self):
        self.assertEqual(self.admit(token="invalid").status_code, 401)
        with patch("app.exams.store.time.time", return_value=self.expires):
            self.assertEqual(self.admit().status_code, 410)
        self.assertEqual(self.client.get(BASE + "/current").status_code, 401)
        self.assertEqual(self.client.post(BASE + "/verify-token", json={"token": ""}).status_code, 422)

    def test_expiry_and_restart_preserve_original_session(self):
        first = self.admit().json()
        with patch("app.exams.store.time.time", return_value=self.expires + 1):
            self.assertEqual(self.client.get(BASE + "/current").json(), first)
            self.assertEqual(self.admit().json(), first)
        restarted = TestClient(create_app({}, entry_db=self.path), headers=HEADERS)
        restarted.cookies.update(self.client.cookies)
        self.assertEqual(restarted.get(BASE + "/current").json(), first)

    def test_other_browser_and_session_id_do_not_grant_access(self):
        session = self.admit().json()
        other = self.new_browser()
        self.assertEqual(self.admit(other).status_code, 409)
        self.assertEqual(other.get(BASE + "/" + session["session_id"] + "/status").status_code, 404)
        self.assertEqual(other.get(BASE + "/current").status_code, 401)
        self.assertEqual(self.client.get(BASE + "/ses_not_mine/status").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/admin/me").status_code, 401)

    def test_lost_cookie_never_reactivates_used_invitation(self):
        self.admit()
        self.client.cookies.clear()
        self.client.get(BASE + "/current")
        self.assertEqual(self.admit().status_code, 409)

    def test_recovery_window_enforced_by_server_and_never_reactivates_invitation(self):
        first = self.admit().json()
        with patch("app.exams.store.time.time", return_value=first["admitted_at"] + RECOVERY_SECONDS):
            # Explicitly resend the secret even after the browser's Cookie timer expires.
            headers = {"Cookie": f"{COOKIE_NAME}={self.client.cookies.get(COOKIE_NAME)}"}
            self.assertEqual(self.client.get(BASE + "/current", headers=headers).status_code, 410)
            self.assertEqual(self.client.get(BASE + "/" + first["session_id"] + "/status", headers=headers).status_code, 410)
            self.assertEqual(self.client.post(BASE + "/verify-token", json={"token": self.token}, headers=headers).status_code, 410)
            other = self.new_browser()
            self.assertEqual(self.admit(other).status_code, 409)

    def test_switching_invitation_does_not_lose_original_or_consume_new(self):
        first = self.admit().json()
        second_token, _ = self.invite("student-02")
        self.assertEqual(self.admit(token=second_token).status_code, 409)
        self.assertEqual(self.client.get(BASE + "/current").json(), first)
        self.assertEqual(self.admit(self.new_browser(), second_token).status_code, 200)

    def test_concurrent_different_browsers_claim_only_once(self):
        other = self.new_browser()
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(self.admit, [self.client, other]))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])

    def test_concurrent_same_browser_retries_return_same_session(self):
        other = TestClient(self.app, headers=HEADERS)
        other.cookies.update(self.client.cookies)
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(self.admit, [self.client, other]))
        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.assertEqual(responses[0].json(), responses[1].json())

    def test_cross_origin_requests_cannot_initialize_or_consume(self):
        other = TestClient(self.app)
        response = other.get(BASE + "/current")
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(other.post(BASE + "/verify-token", json={"token": self.token}).status_code, 403)
        self.assertEqual(self.admit().status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_rate_limit_can_recover(self):
        for _ in range(10):
            self.assertEqual(self.admit(token="wrong").status_code, 401)
        self.assertEqual(self.admit().status_code, 429)
        import time
        with patch("app.exams.router.time.time", return_value=time.time() + 61):
            self.assertEqual(self.admit().status_code, 200)

    def test_provision_command_and_duplicate_guard(self):
        arguments = ["provision", "--candidate-id", "student-02", "--name", "李同学",
                     "--exam-id", "exam-01", "--title", "入场测试"]
        output = io.StringIO()
        with patch.dict("os.environ", {"ENTRY_DB_FILE": str(self.path)}), patch("sys.argv", arguments), redirect_stdout(output):
            main()
        issued_token = next(line for line in output.getvalue().splitlines() if line.startswith("tok_"))
        self.assertEqual(self.admit(token=issued_token).json()["candidate"]["name"], "李同学")
        with self.assertRaises(ValueError):
            self.invite("student-02")
        with self.assertRaises(ValueError):
            self.store.invite("s", " ", "e", "title", 60, 24)


if __name__ == "__main__":
    unittest.main()

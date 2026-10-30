import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.admin.accounts import hash_password, load_accounts, main, verify_password
from app.admin.router import COOKIE_NAME, SESSION_SECONDS, token_key
from app.main import create_app

HEADERS = {"X-VibeEval-Request": "1"}


class ExaminerLoginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.account = {"examiner": {"name": "测试考官", "password_hash": hash_password("testing-password")}}

    def setUp(self):
        self.app = create_app(self.account)
        self.client = TestClient(self.app)

    def login(self, password="testing-password"):
        return self.client.post("/api/v1/admin/login", json={"username": "examiner", "password": password}, headers=HEADERS)

    def test_hash_and_local_account_storage(self):
        encoded = self.account["examiner"]["password_hash"]
        self.assertNotIn("testing-password", encoded)
        self.assertTrue(verify_password("testing-password", encoded))
        self.assertFalse(verify_password("wrong", encoded))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "accounts.json"
            with patch.dict("os.environ", {"ADMIN_ACCOUNTS_FILE": str(path)}):
                self.assertEqual(load_accounts(), {})
                path.write_text(json.dumps(self.account), encoding="utf-8")
                self.assertEqual(load_accounts(), self.account)

    def test_provision_command_creates_and_updates_account(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "accounts.json"
            with patch.dict("os.environ", {"ADMIN_ACCOUNTS_FILE": str(path)}), patch(
                "sys.argv", ["accounts", "examiner", "--name", "陈智恒"]
            ), patch("getpass.getpass", side_effect=["local-test-password"] * 2):
                main()
                saved = load_accounts()["examiner"]
                self.assertEqual(saved["name"], "陈智恒")
                self.assertTrue(verify_password("local-test-password", saved["password_hash"]))
            with patch.dict("os.environ", {"ADMIN_ACCOUNTS_FILE": str(path)}), patch(
                "sys.argv", ["accounts", "examiner", "--name", "新名字"]
            ), patch("getpass.getpass", side_effect=["updated-password"] * 2):
                main()
                saved = load_accounts()["examiner"]
                self.assertEqual(saved["name"], "新名字")
                self.assertFalse(verify_password("local-test-password", saved["password_hash"]))
                self.assertTrue(verify_password("updated-password", saved["password_hash"]))

    def test_invalid_credentials_and_unauthorized_access(self):
        self.assertEqual(self.client.get("/api/v1/admin/me").status_code, 401)
        self.assertEqual(self.login("incorrect").status_code, 401)
        unknown = self.client.post("/api/v1/admin/login", json={"username": "missing", "password": "incorrect"}, headers=HEADERS)
        self.assertEqual(unknown.json(), {"detail": "账号或密码错误"})
        invalid = self.client.post("/api/v1/admin/login", json={"username": "", "password": ""}, headers=HEADERS)
        self.assertEqual(invalid.status_code, 422)

    def test_refresh_and_exact_three_hour_expiry(self):
        start = 2_000_000_000
        with patch("app.admin.router.time.time", return_value=start):
            response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "测试考官")
        self.assertEqual(response.json()["expires_at"], start + SESSION_SECONDS)
        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.assertIn("Max-Age=10800", cookie)
        for elapsed in (1, 3600, SESSION_SECONDS - 1):
            with patch("app.admin.router.time.time", return_value=start + elapsed):
                refreshed = self.client.get("/api/v1/admin/me")
            self.assertEqual(refreshed.status_code, 200)
            self.assertEqual(refreshed.json()["expires_at"], start + SESSION_SECONDS)
            self.assertNotIn("set-cookie", refreshed.headers)
        with patch("app.admin.router.time.time", return_value=start + SESSION_SECONDS):
            self.assertEqual(self.client.get("/api/v1/admin/me").status_code, 401)

    def test_logout_revokes_old_cookie_and_is_idempotent(self):
        self.login()
        token = self.client.cookies.get(COOKIE_NAME)
        response = self.client.post("/api/v1/admin/logout", json={}, headers=HEADERS)
        self.assertEqual(response.status_code, 204)
        self.assertNotIn(token_key(token), self.app.state.auth.sessions)
        self.assertEqual(self.client.get("/api/v1/admin/me", headers={"Cookie": f"{COOKIE_NAME}={token}"}).status_code, 401)
        self.assertEqual(self.client.post("/api/v1/admin/logout", json={}, headers=HEADERS).status_code, 204)

    def test_forged_cookie_and_cross_origin_form_rejected(self):
        self.client.cookies.set(COOKIE_NAME, "forged")
        self.assertEqual(self.client.get("/api/v1/admin/me").status_code, 401)
        response = self.client.post("/api/v1/admin/login", json={"username": "examiner", "password": "testing-password"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.post("/api/v1/admin/logout", json={}).status_code, 403)

    def test_rate_limit_and_retry(self):
        start = 2_000_000_000
        with patch("app.admin.router.time.time", return_value=start):
            for _ in range(5):
                self.assertEqual(self.login("incorrect").status_code, 401)
            self.assertEqual(self.login().status_code, 429)
        with patch("app.admin.router.time.time", return_value=start + 61):
            self.assertEqual(self.login().status_code, 200)

    def test_no_accounts_and_restart_invalidate_session(self):
        empty = TestClient(create_app({}))
        self.assertEqual(empty.post("/api/v1/admin/login", json={"username": "any", "password": "any"}, headers=HEADERS).status_code, 503)
        self.login()
        token = self.client.cookies.get(COOKIE_NAME)
        restarted = TestClient(create_app(self.account))
        self.assertEqual(restarted.get("/api/v1/admin/me", headers={"Cookie": f"{COOKIE_NAME}={token}"}).status_code, 401)

    def test_new_login_rotates_session_and_disables_caching(self):
        self.login()
        previous = self.client.cookies.get(COOKIE_NAME)
        response = self.login()
        self.assertNotEqual(previous, self.client.cookies.get(COOKIE_NAME))
        self.assertNotIn(token_key(previous), self.app.state.auth.sessions)
        self.assertEqual(response.headers["cache-control"], "no-store")


if __name__ == "__main__":
    unittest.main()

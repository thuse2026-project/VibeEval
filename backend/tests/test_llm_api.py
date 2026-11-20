import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.admin.accounts import hash_password
from app.llm.client import LLMClient
from app.llm.config import LLMSettings
from app.llm.router import get_client, get_settings
from app.main import create_app

HEADERS = {"X-VibeEval-Request": "1"}
PREFIX = "/api/v1/admin/llm"
SECRET = "test-provider-secret-never-return"


class LLMApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.accounts = {
            "examiner": {
                "name": "API 测试考官",
                "password_hash": hash_password("testing-password"),
            }
        }

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.app = create_app(
            accounts=self.accounts, entry_db=Path(directory.name) / "entry.sqlite3"
        )
        self.settings = LLMSettings(
            base_url="https://provider.test/api/v1",
            api_key=SECRET,
            model="test/model",
            max_output_tokens=512,
        )
        self.requests = []
        self.reply = {
            "id": "completion-001",
            "model": "test/model",
            "choices": [{"message": {"content": "你好"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            "provider_debug": SECRET,
        }
        self.status = 200
        self.failure = None
        self.raw_reply = None

        def transport(request):
            self.requests.append(request)
            if self.failure:
                raise self.failure
            if self.raw_reply is not None:
                return httpx.Response(self.status, content=self.raw_reply)
            return httpx.Response(self.status, json=self.reply)

        self.app.dependency_overrides[get_settings] = lambda: self.settings
        self.app.dependency_overrides[get_client] = lambda: LLMClient(
            self.settings, transport=httpx.MockTransport(transport)
        )
        self.client = self.enterContext(TestClient(self.app))

    def login(self):
        response = self.client.post(
            "/api/v1/admin/login",
            json={"username": "examiner", "password": "testing-password"},
            headers=HEADERS,
        )
        self.assertEqual(response.status_code, 200)

    def chat(self, **kwargs):
        return self.client.post(
            PREFIX + "/chat",
            json={"messages": [{"role": "user", "content": "你好"}], **kwargs},
            headers=HEADERS,
        )

    def test_all_routes_require_examiner_cookie_before_provider_request(self):
        self.client.cookies.set("vibeeval_candidate", "candidate-cookie")
        for path in ("/config", "/models"):
            self.assertEqual(self.client.get(PREFIX + path).status_code, 401)
        self.assertEqual(self.chat().status_code, 401)
        self.assertEqual(self.requests, [])

    def test_config_is_private_and_does_not_return_key(self):
        self.login()
        response = self.client.get(PREFIX + "/config")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["configured"])
        self.assertEqual(response.json()["model"], "test/model")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn(SECRET, response.text)
        self.assertNotIn(SECRET, repr(self.settings))
        self.assertEqual(self.requests, [])

    def test_models_preserve_base_path_and_filter_provider_fields(self):
        self.login()
        self.reply = {
            "data": [
                {
                    "id": "test/model",
                    "name": "Test",
                    "context_length": 8192,
                    "debug": SECRET,
                }
            ]
        }
        response = self.client.get(PREFIX + "/models")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["id"], "test/model")
        self.assertNotIn(SECRET, response.text)
        request = self.requests[0]
        self.assertEqual(str(request.url), "https://provider.test/api/v1/models")
        self.assertEqual(request.headers["authorization"], "Bearer " + SECRET)

    def test_chat_sends_context_and_default_limits_without_tools(self):
        self.login()
        messages = [
            {"role": "system", "content": "简短回答"},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好"},
            {"role": "user", "content": "继续"},
        ]
        response = self.chat(messages=messages)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], "你好")
        self.assertEqual(response.json()["usage"]["total_tokens"], 6)
        self.assertNotIn(SECRET, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(
            str(self.requests[0].url),
            "https://provider.test/api/v1/chat/completions",
        )
        body = json.loads(self.requests[0].content)
        self.assertEqual(
            body,
            {
                "messages": messages,
                "model": "test/model",
                "max_tokens": 512,
                "stream": False,
            },
        )

    def test_chat_can_select_model_and_optional_parameters(self):
        self.login()
        response = self.chat(model="another/model", max_tokens=32, temperature=0)
        self.assertEqual(response.status_code, 200)
        body = json.loads(self.requests[0].content)
        self.assertEqual(body["model"], "another/model")
        self.assertEqual(body["max_tokens"], 32)
        self.assertEqual(body["temperature"], 0)

    def test_chat_requires_browser_header(self):
        self.login()
        response = self.client.post(
            PREFIX + "/chat", json={"messages": [{"role": "user", "content": "Hi"}]}
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.requests, [])

    def test_input_bounds_and_tools_are_rejected_before_provider_request(self):
        self.login()
        cases = [
            {"messages": []},
            {"messages": [{"role": "tool", "content": "output"}]},
            {"messages": [{"role": "user", "content": " "}]},
            {"messages": [{"role": "user", "content": "a" * 16001}]},
            {"messages": [{"role": "user", "content": "a" * 16000}] * 5},
            {"messages": [{"role": "user", "content": "a"}] * 41},
            {"model": " "},
            {"max_tokens": 513},
            {"max_tokens": 0},
            {"temperature": 3},
            {"tools": [{"type": "function"}]},
            {"stream": True},
        ]
        for body in cases:
            with self.subTest(body_keys=list(body)):
                self.assertEqual(self.chat(**body).status_code, 422)
        self.assertEqual(self.requests, [])

    def test_unconfigured_key_and_model_do_not_send_requests(self):
        self.login()
        self.settings = replace(self.settings, api_key="")
        self.assertFalse(self.client.get(PREFIX + "/config").json()["configured"])
        self.assertEqual(self.client.get(PREFIX + "/models").status_code, 503)
        self.assertEqual(self.chat().status_code, 503)
        self.settings = replace(self.settings, api_key=SECRET, model="")
        self.assertEqual(self.chat().status_code, 503)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.chat(model="chosen/model").status_code, 200)

    def test_provider_failures_are_sanitized_and_never_retried(self):
        self.login()
        self.reply = {"error": {"message": SECRET}}
        for upstream, expected in (
            (401, 502),
            (403, 502),
            (402, 503),
            (404, 502),
            (429, 429),
            (500, 502),
            (302, 502),
        ):
            with self.subTest(status=upstream):
                self.status = upstream
                self.requests.clear()
                response = self.chat()
                self.assertEqual(response.status_code, expected)
                self.assertNotIn(SECRET, response.text)
                self.assertEqual(len(self.requests), 1)

    def test_errors_in_success_payload_are_not_treated_as_text(self):
        self.login()
        self.reply = {"error": {"code": 429, "message": SECRET}}
        response = self.chat()
        self.assertEqual(response.status_code, 429)
        self.assertNotIn(SECRET, response.text)

    def test_network_and_timeout_errors_hide_transport_details(self):
        self.login()
        for failure, expected in (
            (httpx.ConnectError(SECRET), 502),
            (httpx.ReadTimeout(SECRET), 504),
            (TimeoutError(SECRET), 504),
        ):
            with self.subTest(error=type(failure).__name__):
                self.failure = failure
                response = self.chat()
                self.assertEqual(response.status_code, expected)
                self.assertNotIn(SECRET, response.text)

    def test_invalid_chat_responses_do_not_succeed(self):
        self.login()
        for reply in (
            [],
            {},
            {"choices": []},
            {"choices": [{"message": {"content": None}}]},
            {"choices": [{"message": {"content": " "}}]},
        ):
            with self.subTest(reply=reply):
                self.reply = reply
                self.assertEqual(self.chat().status_code, 502)
        self.raw_reply = b"not json"
        self.assertEqual(self.chat().status_code, 502)

    def test_invalid_model_lists_do_not_succeed(self):
        self.login()
        for reply in ({}, {"data": {}}, {"data": [None]}, {"data": [{}]}):
            with self.subTest(reply=reply):
                self.reply = reply
                self.assertEqual(self.client.get(PREFIX + "/models").status_code, 502)

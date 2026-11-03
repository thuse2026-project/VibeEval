import asyncio
import json
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.db.lifecycle import LifecycleConflict
from app.main import create_app
from app.workspace.terminal import parse_message, relay, router


class ProtocolTests(unittest.TestCase):
    def test_control_characters_are_not_changed(self):
        self.assertEqual(
            parse_message(json.dumps({"type": "input", "data": "\x03"})),
            ("input", b"\x03"),
        )

    def test_invalid_frames(self):
        for message in [
            [],
            {},
            {"type": "resize", "cols": True, "rows": 20},
            {"type": "resize", "cols": 301, "rows": 20},
            {"type": "input", "data": "x" * 8193},
        ]:
            with self.subTest(message=str(message)[:70]), self.assertRaises(ValueError):
                parse_message(json.dumps(message))


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_incremental_unicode_output(self):
        terminal = Mock()
        terminal.read.side_effect = [b"\xe4", b"\xb8\xad", b""]

        class Socket:
            messages = []

            async def send_json(self, value):
                self.messages.append(value)

            async def receive_text(self):
                await asyncio.Event().wait()

        socket = Socket()
        await relay(socket, terminal, lambda operation: operation(None))
        self.assertEqual(socket.messages, [{"type": "output", "data": "中"}])


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Mock()
        self.app = create_app({}, submission_sandbox=self.runtime)
        self.app.include_router(router)
        self.terminal = Mock()
        self.terminal.read.side_effect = lambda: (
            __import__("time").sleep(0.01) or None
        )
        self.app.state.terminal_factory = Mock()
        self.app.state.terminal_factory.open.return_value = self.terminal
        self.status = "in_progress"
        self.guards = 0

    @contextmanager
    def guard(self, session_id):
        self.guards += 1
        if self.status != "in_progress":
            raise LifecycleConflict("not writable")
        yield SimpleNamespace(session_id=session_id)

    def test_origin_rejected_before_open(self):
        with patch.dict(
            "os.environ", {"SANDBOX_TERMINAL_ORIGINS": "http://testserver"}
        ):
            with TestClient(self.app) as client:
                for origin in (None, "http://evil.example", "null"):
                    headers = {"origin": origin} if origin else {}
                    with self.assertRaises(WebSocketDisconnect):
                        with client.websocket_connect(
                            "/api/v1/sessions/ses/terminal", headers=headers
                        ):
                            pass
        self.app.state.terminal_factory.open.assert_not_called()

    def test_no_cookie_rejected(self):
        with patch.dict(
            "os.environ", {"SANDBOX_TERMINAL_ORIGINS": "http://testserver"}
        ):
            with TestClient(self.app) as client:
                with self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect(
                        "/api/v1/sessions/ses/terminal",
                        headers={"origin": "http://testserver"},
                    ):
                        pass
        self.app.state.terminal_factory.open.assert_not_called()

    def test_input_resize_and_submission_fence(self):
        self.app.state.entry = SimpleNamespace(factory=Mock())
        lifecycle = Mock()
        lifecycle.write_guard.side_effect = self.guard
        with (
            patch.dict("os.environ", {"SANDBOX_TERMINAL_ORIGINS": "http://testserver"}),
            patch(
                "app.workspace.terminal.require_candidate_session",
                return_value={"session_id": "ses"},
            ),
            patch("app.workspace.terminal.SessionLifecycle", return_value=lifecycle),
        ):
            with TestClient(self.app) as client:
                with client.websocket_connect(
                    "/api/v1/sessions/ses/terminal",
                    headers={"origin": "http://testserver"},
                ) as ws:
                    ws.send_json({"type": "input", "data": "\x03"})
                    ws.send_json({"type": "resize", "cols": 80, "rows": 24})
                    # Synchronize with the worker without relying on a fixed sleep.
                    import time

                    end = time.monotonic() + 3
                    while not self.terminal.resize.called and time.monotonic() < end:
                        time.sleep(0.01)
                    self.terminal.write.assert_called_once_with(b"\x03")
                    self.terminal.resize.assert_called_once_with(80, 24)
                    self.status = "submitting"
                    ws.send_json({"type": "input", "data": "touch forbidden\r"})
                    with self.assertRaises(WebSocketDisconnect):
                        ws.receive_json()
        self.terminal.write.assert_called_once()
        self.terminal.close.assert_called_once()
        self.assertGreaterEqual(self.guards, 4)

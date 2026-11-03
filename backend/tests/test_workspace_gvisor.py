"""Opt-in real PostgreSQL + gVisor + HTTP Cookie + WebSocket acceptance."""

import os
import queue
import threading
import time
import unittest
import uuid
from dataclasses import replace
from unittest.mock import patch

import test_db_postgres as database_tests
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from workspace_network import live_client

from app.db.lifecycle import SessionLifecycle
from app.exams.postgres import PostgresEntryStore
from app.main import create_app
from app.sandbox.bound_adapter import BoundSandboxAdapter
from app.sandbox.runtime import SandboxRuntime
from app.sandbox.terminal import DockerTerminalFactory
from app.workspace.router import router
from app.workspace.terminal import router as terminal_router


@unittest.skipUnless(
    os.environ.get("VIBEEVAL_RUN_WORKSPACE_TESTS") == "1",
    "requires isolated PostgreSQL and real Docker/runsc",
)
class WorkspaceRealTests(unittest.TestCase):
    setUpClass = classmethod(database_tests.PostgreSQLTests.setUpClass.__func__)
    drop_test_database = classmethod(
        database_tests.PostgreSQLTests.drop_test_database.__func__
    )

    def test_cookie_pty_read_and_freeze(self):
        import docker

        image = os.environ["SANDBOX_IMAGE_REF"]
        runtime = SandboxRuntime(backend_ref="workspace-test")
        entry = PostgresEntryStore(factory=self.factory)
        lifecycle = SessionLifecycle(self.factory)
        token, _ = entry.invite(
            uuid.uuid4().hex, "test", uuid.uuid4().hex, "test", 60, 24
        )
        app = create_app({}, entry_store=entry, submission_sandbox=runtime)
        app.state.terminal_factory = DockerTerminalFactory(runtime.settings)
        app.include_router(router)
        app.include_router(terminal_router)
        docker_client = docker.DockerClient(base_url=runtime.settings[0], timeout=20)
        view = None
        config = {
            "SANDBOX_IMAGE_REF": image,
            "SANDBOX_BACKEND_REF": "workspace-test",
            "SANDBOX_TASK_VERSION": "python-debug-01:f898a77",
            "SANDBOX_TERMINAL_ORIGINS": "http://testserver",
        }
        try:
            with (
                patch.dict(os.environ, config),
                live_client(app) as client,
            ):
                client.get("/api/v1/sessions/current")
                admission = client.post(
                    "/api/v1/sessions/verify-token", json={"token": token}
                )
                self.assertEqual(admission.status_code, 200, admission.text)
                sid = admission.json()["session_id"]
                path = "/api/v1/sessions/" + sid
                prepared = client.post(path + "/prepare")
                view = lifecycle.get_session(sid)
                self.assertEqual(prepared.status_code, 200, prepared.text)
                with self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect(
                        path + "/terminal", headers={"origin": "http://testserver"}
                    ):
                        pass
                self.assertEqual(client.post(path + "/start").status_code, 200)
                view = lifecycle.get_session(sid)
                with TestClient(app) as stranger:
                    with self.assertRaises(WebSocketDisconnect):
                        with stranger.websocket_connect(
                            path + "/terminal", headers={"origin": "http://testserver"}
                        ):
                            pass
                self.assertEqual(runtime.read_file(view, "tests/test_candidate.py"), "")
                # Disconnect and reopen keep the same sandbox and exam deadline.
                with client.websocket_connect(
                    path + "/terminal", headers={"origin": "http://testserver"}
                ):
                    pass
                self.assertEqual(lifecycle.get_session(sid), view)
                with client.websocket_connect(
                    path + "/terminal", headers={"origin": "http://testserver"}
                ) as ws:
                    received = queue.Queue()

                    def receiver():
                        try:
                            while True:
                                received.put(ws.receive_json()["data"])
                        except WebSocketDisconnect:
                            received.put(None)

                    thread = threading.Thread(target=receiver, daemon=True)
                    thread.start()
                    output = ""

                    def expect(text):
                        nonlocal output
                        end = time.monotonic() + 10
                        while text not in output:
                            part = received.get(
                                timeout=max(0.01, end - time.monotonic())
                            )
                            self.assertIsNotNone(part, output)
                            output += part

                    ws.send_json({"type": "resize", "cols": 91, "rows": 27})
                    ws.send_json({"type": "input", "data": "stty size\r"})
                    expect("27 91")
                    ws.send_json({"type": "input", "data": "sleep 30\r"})
                    time.sleep(0.3)
                    ws.send_json({"type": "input", "data": "\x03"})
                    ws.send_json(
                        {
                            "type": "input",
                            "data": "printf '\\nCTRL_OK\\n'; printf '# terminal edit\\n' > tests/test_candidate.py\r",
                        }
                    )
                    expect("\r\nCTRL_OK\r\n")
                    # The prompt after this marker orders file reads after the write.
                    ws.send_json(
                        {"type": "input", "data": "printf '\\nWRITE_DONE\\n'\r"}
                    )
                    expect("\r\nWRITE_DONE\r\n")
                    self.assertEqual(
                        runtime.read_file(view, "tests/test_candidate.py"),
                        "# terminal edit\n",
                    )
                    ws.send_json(
                        {
                            "type": "input",
                            "data": "(while :; do printf '# tick\\n' >> src/booking.py; sleep 0.05; done) &\r",
                        }
                    )
                    ws.send_json(
                        {"type": "input", "data": "printf '\\nBACKGROUND_READY\\n'\r"}
                    )
                    expect("\r\nBACKGROUND_READY\r\n")
                    lifecycle.begin_submission(sid)
                    ws.send_json(
                        {
                            "type": "input",
                            "data": "echo forbidden > tests/test_candidate.py\r",
                        }
                    )
                    while received.get(timeout=5) is not None:
                        pass
                    thread.join(timeout=2)
                frozen_view = lifecycle.get_session(sid)
                frozen = runtime.freeze_and_export(frozen_view)
                candidate = next(
                    f for f in frozen.files if f.path == "tests/test_candidate.py"
                )
                self.assertEqual(candidate.content, "# terminal edit\n")
                time.sleep(0.2)
                self.assertEqual(
                    runtime.freeze_and_export(frozen_view).files, frozen.files
                )
                self.assertEqual(
                    docker_client.containers.get(view.sandbox_id).status, "paused"
                )
                # Direct adapter also rejects a different session's claimed binding.
                with self.assertRaises(ValueError):
                    BoundSandboxAdapter(docker_client, "workspace-test")._container(
                        replace(view, session_id="other")
                    )
        finally:
            if view and view.sandbox_resource_key:
                try:
                    docker_client.containers.get(view.sandbox_resource_key).remove(
                        force=True
                    )
                except docker.errors.NotFound:
                    pass
                try:
                    docker_client.volumes.get(
                        view.sandbox_resource_key + "-data"
                    ).remove()
                except docker.errors.NotFound:
                    pass
            docker_client.close()

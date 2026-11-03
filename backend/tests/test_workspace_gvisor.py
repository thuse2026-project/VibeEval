"""Opt-in real PostgreSQL + gVisor + HTTP Cookie + WebSocket acceptance."""

import os
import queue
import threading
import time
import unittest
import uuid
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace
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

    def test_atomic_save_limits_failure_recovery_and_export(self):
        import docker

        from app.sandbox.safe_save import WRITE_SCRIPT, remove_writer

        client = docker.DockerClient(base_url="unix:///var/run/docker.sock", timeout=20)
        adapter = BoundSandboxAdapter(client, "save-fault-test")
        view = SimpleNamespace(
            session_id="save-" + uuid.uuid4().hex,
            status="provisioning",
            sandbox_id=None,
            sandbox_resource_key="save-" + uuid.uuid4().hex,
            sandbox_backend_ref="save-fault-test",
            sandbox_image_ref=os.environ["SANDBOX_IMAGE_REF"],
            deadline_at=int(time.time()) + 600,
        )
        path = "tests/test_candidate.py"
        try:
            view.sandbox_id = adapter.ensure_bound(view)
            view.status = "in_progress"
            large = "#" + "x" * (1024 * 1024 - 2) + "\n"
            self.assertTrue(
                adapter.save_file(view, path, sha256(b"").hexdigest(), large)["saved"]
            )
            self.assertEqual(
                sha256(adapter.read_file(view, path).encode()).hexdigest(),
                sha256(large.encode()).hexdigest(),
            )
            adapter.check_ready(view)
            # Deliberately terminate only the fixed helper command before rename.
            injected = WRITE_SCRIPT.replace(
                "os.replace(stage,name", "os._exit(77)\nos.replace(stage,name"
            )
            with (
                patch("app.sandbox.safe_save.WRITE_SCRIPT", injected),
                self.assertRaises(ValueError),
            ):
                adapter.save_file(
                    view, path, sha256(large.encode()).hexdigest(), "# partial\n"
                )
            self.assertEqual(client.containers.get(view.sandbox_id).status, "paused")
            self.assertEqual(
                sha256(adapter.read_file(view, path).encode()).hexdigest(),
                sha256(large.encode()).hexdigest(),
            )
            self.assertTrue(
                adapter.save_file(
                    view, path, sha256(large.encode()).hexdigest(), "# recovered\n"
                )["saved"]
            )
            self.assertEqual(client.containers.get(view.sandbox_id).status, "running")
            adapter.check_ready(view)
            cleanup_calls = 0

            def interrupted_cleanup(*args):
                nonlocal cleanup_calls
                cleanup_calls += 1
                if cleanup_calls == 2:
                    raise RuntimeError("injected cleanup interruption")
                return remove_writer(*args)

            with (
                patch("app.sandbox.safe_save.WRITE_SCRIPT", injected),
                patch(
                    "app.sandbox.safe_save.remove_writer",
                    side_effect=interrupted_cleanup,
                ),
                self.assertRaises(RuntimeError),
            ):
                adapter.save_file(
                    view,
                    path,
                    sha256(b"# recovered\n").hexdigest(),
                    "# uncommitted stage\n",
                )
            self.assertEqual(
                client.containers.get(view.sandbox_resource_key + "-writer").status,
                "running",
            )
            view.status = "submitting"
            frozen = adapter.freeze_and_export(view)
            with self.assertRaises(docker.errors.NotFound):
                client.containers.get(view.sandbox_resource_key + "-writer")
            self.assertEqual(len(frozen.files), 6)
            self.assertEqual(
                next(f.content for f in frozen.files if f.path == path), "# recovered\n"
            )
        finally:
            remove_writer(client, view)
            try:
                client.containers.get(view.sandbox_resource_key).remove(
                    force=True, v=False
                )
            except docker.errors.NotFound:
                pass
            try:
                client.volumes.get(view.sandbox_resource_key + "-data").remove()
            except docker.errors.NotFound:
                pass
            client.close()

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
                listed = client.get(path + "/files")
                self.assertEqual(listed.status_code, 200, listed.text)
                self.assertEqual(len(listed.json()["files"]), 6)
                initial = client.get(
                    path + "/file", params={"path": "tests/test_candidate.py"}
                )
                self.assertEqual(initial.json()["version"], 1)
                saved = client.put(
                    path + "/file",
                    json={
                        "path": "tests/test_candidate.py",
                        "expected_version": 1,
                        "content": "# saved through HTTP\n",
                    },
                )
                self.assertEqual(saved.status_code, 200, saved.text)
                self.assertEqual(saved.json()["new_version"], 2)
                stale = client.put(
                    path + "/file",
                    json={
                        "path": "tests/test_candidate.py",
                        "expected_version": 1,
                        "content": "lost update",
                    },
                )
                self.assertEqual(stale.status_code, 409, stale.text)
                self.assertEqual(
                    stale.json()["detail"]["code"], "FILE_VERSION_CONFLICT"
                )
                readonly = client.put(
                    path + "/file",
                    json={
                        "path": "README.md",
                        "expected_version": 1,
                        "content": "forbidden",
                    },
                )
                self.assertEqual(readonly.status_code, 403)
                # No-op saves retain the durable version.
                replay = client.put(
                    path + "/file",
                    json={
                        "path": "tests/test_candidate.py",
                        "expected_version": 2,
                        "content": "# saved through HTTP\n",
                    },
                )
                self.assertEqual(replay.json()["new_version"], 2)
                with TestClient(app) as stranger:
                    with self.assertRaises(WebSocketDisconnect):
                        with stranger.websocket_connect(
                            path + "/terminal", headers={"origin": "http://testserver"}
                        ):
                            pass
                self.assertEqual(
                    runtime.read_file(view, "tests/test_candidate.py"),
                    "# saved through HTTP\n",
                )
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
                    ws.send_json(
                        {"type": "input", "data": "cat tests/test_candidate.py\r"}
                    )
                    expect("# saved through HTTP\r\n")
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
                    shell_conflict = client.put(
                        path + "/file",
                        json={
                            "path": "tests/test_candidate.py",
                            "expected_version": 2,
                            "content": "overwrite shell",
                        },
                    )
                    self.assertEqual(
                        shell_conflict.status_code, 409, shell_conflict.text
                    )
                    self.assertEqual(
                        shell_conflict.json()["detail"]["current_version"], 3
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
                late = client.put(
                    path + "/file",
                    json={
                        "path": "tests/test_candidate.py",
                        "expected_version": 3,
                        "content": "after submission",
                    },
                )
                self.assertEqual(late.status_code, 409)
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

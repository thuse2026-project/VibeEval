"""Opt-in acceptance: candidate commands run only in owned Linux/runsc containers."""

import base64
import hashlib
import os
import re
import time
import unittest
import uuid
from dataclasses import replace

import docker
from docker.errors import NotFound
from test_sandbox_execution import session_view

from app.sandbox.bound_adapter import BoundSandboxAdapter
from app.sandbox.execution import stop_bound_executions


@unittest.skipUnless(
    os.environ.get("VIBEEVAL_RUN_GVISOR_TESTS") == "1",
    "Set VIBEEVAL_RUN_GVISOR_TESTS=1 on an isolated Linux/runsc test host",
)
class GvisorExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = os.environ.get("VIBEEVAL_GVISOR_IMAGE_REF", "")
        cls.socket = os.environ.get("SANDBOX_DOCKER_URL", "unix:///var/run/docker.sock")
        if os.name != "posix" or not cls.socket.startswith("unix://"):
            raise RuntimeError("Acceptance requires a local Linux Docker Unix socket")
        if not re.fullmatch(r"(?:[^\s@]+@)?sha256:[0-9a-f]{64}", cls.image):
            raise RuntimeError("Use a prebuilt immutable VIBEEVAL_GVISOR_IMAGE_REF")

    def setUp(self):
        self.client = docker.DockerClient(base_url=self.socket, timeout=15)
        self.addCleanup(self.client.close)
        if "runsc" not in self.client.info().get("Runtimes", {}):
            self.fail("runsc is required; acceptance never falls back to runc")
        self.client.images.get(self.image)  # No automatic image pull.
        identity = uuid.uuid4().hex
        self.view = session_view(
            session_id=identity,
            sandbox_resource_key="vbe-execution-test-" + identity,
            sandbox_id=None,
            sandbox_image_ref=self.image,
            sandbox_backend_ref="execution-runsc-acceptance",
            status="provisioning",
            deadline_at=int(time.time()) + 90,
        )
        self.adapter = BoundSandboxAdapter(self.client, self.view.sandbox_backend_ref)
        self.addCleanup(self.cleanup_owned)
        handle = self.adapter.ensure_bound(self.view)
        self.view = replace(self.view, sandbox_id=handle, status="in_progress")
        self.adapter.check_ready(self.view)

    def cleanup_owned(self):
        if self.view.sandbox_id:
            stop_bound_executions(self.adapter, self.view, remove=True)
        try:
            obj = self.client.containers.get(self.view.sandbox_resource_key)
        except NotFound:
            obj = None
        if obj is not None:
            if obj.labels.get("vibeeval.session") != self.view.session_id:
                raise RuntimeError("Refusing to remove a foreign container")
            obj.remove(force=True, v=False)
        try:
            volume = self.client.volumes.get(self.view.sandbox_resource_key + "-data")
        except NotFound:
            return
        if (
            volume.attrs.get("Labels", {}).get("vibeeval.session")
            != self.view.session_id
        ):
            raise RuntimeError("Refusing to remove a foreign volume")
        volume.remove()

    def start(self, code, *, timeout=15, digests=None):
        self.config = {
            "operation_id": uuid.uuid4().hex,
            "request_sha256": "a" * 64,
            "argv": ["python", "-c", code],
            "cwd": ".",
            "timeout_seconds": timeout,
            "max_output_bytes": 1024,
            "deadline_at": self.view.deadline_at,
            "expected_digests": digests or {},
        }
        self.handle = self.adapter.prepare_execution(self.view, self.config)["handle"]
        self.adapter.start_execution(self.view, self.config, self.handle)

    def finished(self):
        until = time.monotonic() + 20
        while time.monotonic() < until:
            value = self.adapter.inspect_execution(self.view, self.config, self.handle)
            if value["processes_stopped"]:
                return value
            time.sleep(0.1)
        self.fail("owned execution did not stop within the acceptance bound")

    def test_uid_private_receipt_and_bounded_capture_share_only_workspace(self):
        before = self.adapter.read_file(self.view, "src/booking.py")
        self.start(
            "import os,sys; from pathlib import Path; assert os.getuid()==1000; "
            "assert not os.access('/control/progress.json',os.R_OK); "
            "assert not os.access('/workspace/tests/test_public.py',os.W_OK); "
            "p=Path('/workspace/src/booking.py'); "
            "p.write_text(p.read_text()+'\\n# execution acceptance\\n'); "
            "print('x'*2000); print('stderr acceptance',file=sys.stderr)"
        )
        value = self.finished()
        self.assertEqual(value["status"], "succeeded", value)
        self.assertEqual(value["exit_code"], 0)
        self.assertTrue(value["stdout_truncated"])
        self.assertEqual(len(base64.b64decode(value["stdout_b64"])), 1024)
        self.assertIn(b"stderr acceptance", base64.b64decode(value["stderr_b64"]))
        after = self.adapter.read_file(self.view, "src/booking.py")
        self.assertNotEqual(before, after)
        self.client.containers.get(self.view.sandbox_id).reload()
        self.assertEqual(
            self.client.containers.get(self.view.sandbox_id).status, "running"
        )

    def test_timeout_ends_entire_helper_even_with_detached_child(self):
        self.start(
            "import subprocess,time; "
            "subprocess.Popen(['python','-c','import time;time.sleep(60)'],start_new_session=True); "
            "print('started',flush=True); time.sleep(60)",
            timeout=1,
        )
        value = self.finished()
        self.assertEqual(value["status"], "timed_out", value)
        self.assertTrue(value["processes_stopped"])
        self.assertEqual(self.client.containers.get(self.handle).status, "exited")
        self.assertEqual(
            self.client.containers.get(self.view.sandbox_id).status, "running"
        )

    def test_cancel_preserves_original_and_retains_output_prefix(self):
        self.start("import time; print('before cancel',flush=True); time.sleep(60)")
        until = time.monotonic() + 8
        while time.monotonic() < until:
            value = self.adapter.inspect_execution(self.view, self.config, self.handle)
            if value.get("stdout_b64"):
                break
            time.sleep(0.1)
        else:
            self.fail("supervisor did not publish a live output prefix")
        value = self.adapter.cancel_execution(self.view, self.config, self.handle)
        self.assertEqual(value["status"], "cancelled", value)
        self.assertTrue(value["processes_stopped"])
        self.assertIn(b"before cancel", base64.b64decode(value["stdout_b64"]))
        self.assertEqual(
            self.client.containers.get(self.view.sandbox_id).status, "running"
        )

    def test_supervisor_rejects_changed_file_before_child_launch(self):
        self.start("print('must not execute')", digests={"src/booking.py": "0" * 64})
        value = self.finished()
        self.assertEqual(value["status"], "conflict", value)
        self.assertEqual(value["reason_code"], "SOURCE_VERSION_CHANGED")
        self.assertIsNone(value["command_started_at"])
        self.assertEqual(value["stdout_b64"], "")

    def start_edit(self, content, *, stale=False):
        before = self.adapter.read_file(self.view, "src/booking.py")
        before_sha = hashlib.sha256(before.encode()).hexdigest()
        self.config = {
            "operation_id": uuid.uuid4().hex,
            "request_sha256": "a" * 64,
            "kind": "file_edit",
            "timeout_seconds": 10,
            "max_output_bytes": 1024,
            "deadline_at": self.view.deadline_at,
            "expected_digests": {"src/booking.py": "0" * 64 if stale else before_sha},
            "edit": {
                "path": "src/booking.py",
                "before_sha256": before_sha,
                "after_sha256": hashlib.sha256(content.encode()).hexdigest(),
                "content_b64": base64.b64encode(content.encode()).decode(),
            },
        }
        self.handle = self.adapter.prepare_execution(self.view, self.config)["handle"]
        self.adapter.start_execution(self.view, self.config, self.handle)
        return before

    def test_atomic_file_edit_preserves_task_permissions_and_reports_owned_pause(self):
        text = "# exact agent replacement\r\nvalue = '中文🙂'\r\n"
        self.start_edit(text)
        value = self.finished()
        self.assertEqual(value["status"], "succeeded", value)
        self.assertTrue(value["pause_owned"])
        self.assertEqual(
            value["file_change"]["after_sha256"],
            hashlib.sha256(text.encode()).hexdigest(),
        )
        self.assertEqual(self.adapter.read_file(self.view, "src/booking.py"), text)
        self.assertEqual(
            self.client.containers.get(self.view.sandbox_id).status, "paused"
        )
        self.adapter.resume_workspace(self.view)
        self.adapter.check_ready(self.view)

    def test_large_file_edit_uses_chunked_config_without_partial_write(self):
        text = "# large exact replacement\n" * 16000
        self.start_edit(text)
        value = self.finished()
        self.assertEqual(value["status"], "succeeded", value)
        self.assertEqual(self.adapter.read_file(self.view, "src/booking.py"), text)
        self.adapter.resume_workspace(self.view)

    def test_stale_edit_rejects_before_replace_and_can_restore_original(self):
        before = self.start_edit("must not replace", stale=True)
        value = self.finished()
        self.assertEqual(value["status"], "conflict", value)
        self.assertTrue(value["pause_owned"])
        self.assertEqual(self.adapter.read_file(self.view, "src/booking.py"), before)
        self.adapter.resume_workspace(self.view)


if __name__ == "__main__":
    unittest.main()

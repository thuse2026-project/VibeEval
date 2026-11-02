"""Shared HTTP/DB/sandbox contract; real gVisor runs are explicitly opt-in."""

import io
import os
import re
import tarfile
import tempfile
import time
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

import docker
import test_db_postgres as database_tests
from docker.errors import NotFound
from fastapi.testclient import TestClient

from app.admin.accounts import hash_password
from app.db.lifecycle import SessionLifecycle
from app.exams.postgres import PostgresEntryStore
from app.main import create_app
from app.sandbox.bound_adapter import BoundSandboxAdapter, prepare_bound
from app.sandbox.runtime import SandboxRuntime
from app.submissions.coordinator import SandboxCleanupFailed, SubmissionCoordinator
from app.submissions.service import SubmissionService
from app.submissions.storage import SnapshotStorageError, SnapshotStore

HEADERS = {"X-VibeEval-Request": "1"}
MARKER = "# sandbox submission integration"
IMAGE = "sha256:" + "a" * 64
BACKEND = "docker-gvisor:integration-tests"


class DockerDouble:
    """Only replace Docker I/O; use the actual binding and export implementations."""

    def __init__(self):
        self.volumes = SimpleNamespace(create=self.volume_create, get=self.volume_get)
        self.images = SimpleNamespace(get=lambda ref: SimpleNamespace(id=IMAGE))
        self.containers = SimpleNamespace(get=self.get, create=self.create)
        self.container = None
        self.volume = None
        self.creates = 0

    def volume_create(self, name, labels):
        self.volume = SimpleNamespace(name=name, attrs={"Labels": labels})
        return self.volume

    def volume_get(self, name):
        if self.volume is None or self.volume.name != name:
            raise NotFound("test volume missing")
        return self.volume

    def get(self, ref):
        if self.container is None or self.container.deleted:
            raise NotFound("test container missing")
        if ref not in (self.container.id, self.container.name):
            raise NotFound("test container missing")
        return self.container

    def create(self, image, command, **kwargs):
        self.creates += 1
        self.container = ContainerDouble(image, kwargs)
        return self.container


class ContainerDouble:
    def __init__(self, image, settings):
        self.id = "b" * 64
        self.name = settings["name"]
        self.status = "created"
        self.deleted = False
        self.attrs = {
            "Image": image,
            "Config": {"Labels": settings["labels"]},
            "HostConfig": {"Runtime": settings["runtime"]},
        }
        self.on_pause = lambda: None
        self.on_remove = lambda: None

    def start(self):
        self.status = "running"

    def reload(self):
        pass

    def pause(self):
        self.on_pause()
        self.status = "paused"

    def rename(self, name):
        self.name = name

    def get_archive(self, path):
        if path != "/workspace" or self.status != "paused":
            raise AssertionError("Export must read the paused workspace")
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as archive:
            for name, text in {
                "workspace/src/booking.py": MARKER + "\n",
                "workspace/tests/test_candidate.py": "",
            }.items():
                content = text.encode()
                entry = tarfile.TarInfo(name)
                entry.size = len(content)
                archive.addfile(entry, io.BytesIO(content))
        return iter([data.getvalue()]), {}

    def remove(self, *, force, v):
        if not force or v:
            raise AssertionError("Cleanup must preserve the workspace volume")
        self.on_remove()
        self.deleted = True


class SubmissionRoundTrip:
    """Run the same receipt and failure assertions with either Docker driver."""

    def setUp(self):
        database_tests.PostgreSQLTests.setUp(self)
        self.driver = self.make_driver()
        entry = PostgresEntryStore(self.factory)
        token, _ = entry.invite("s1", "集成考生", "e1", "交卷联调", 60, 24)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.snapshots = SnapshotStore(directory.name)
        self.service = SubmissionService(self.snapshots, self.factory)
        self.lifecycle = SessionLifecycle(self.factory)
        self.bound = BoundSandboxAdapter(self.driver, self.backend_ref)
        self.sandbox = self.make_submission_adapter()
        self.app = create_app(
            {
                "examiner": {
                    "name": "联调考官",
                    "password_hash": hash_password("integration-password-123"),
                }
            },
            entry_store=entry,
            submission_sandbox=self.sandbox,
            snapshot_store=self.snapshots,
        )
        self.client = TestClient(self.app, headers=HEADERS)
        self.addCleanup(self.client.close)
        self.client.get("/api/v1/sessions/current")
        admitted = self.client.post(
            "/api/v1/sessions/verify-token", json={"token": token}
        )
        self.assertEqual(admitted.status_code, 200)
        self.sid = admitted.json()["session_id"]
        self.url = f"/api/v1/sessions/{self.sid}/submit"
        view = self.lifecycle.prepare_sandbox(
            self.sid,
            self.image_ref,
            backend_ref=self.backend_ref,
            task_version_id="python-debug-01:integration",
        )
        self.resource_key = view.sandbox_resource_key
        self.addCleanup(self.cleanup_test_resources)
        bound = prepare_bound(
            self.lifecycle,
            self.bound,
            self.sid,
            self.image_ref,
            "python-debug-01:integration",
        )
        self.container = self.driver.containers.get(bound.sandbox_id)
        self.check_health_and_write_answer()
        with self.lifecycle.lock_session(self.sid) as locked:
            locked.mark_ready()
        self.started = self.lifecycle.start_exam(self.sid)

    def make_submission_adapter(self):
        return self.bound

    def submit(self, key="integration-key"):
        return self.client.post(self.url, headers={"Idempotency-Key": key})

    def finish_cleanup(self):
        coordinator = SubmissionCoordinator(
            self.sandbox, lifecycle=self.lifecycle, submissions=self.service
        )
        receipt = self.service.get_session_receipt(self.sid)
        for attempt in range(3):
            try:
                coordinator.cleanup(self.sid)
                return
            except SandboxCleanupFailed:
                # A Docker delete accepted before timeout can still be finishing.
                self.assertEqual(receipt, self.service.get_session_receipt(self.sid))
                self.assertEqual(
                    [
                        view.session_id
                        for view in self.lifecycle.list_cleanup_candidates()
                    ],
                    [self.sid],
                )
                if attempt == 2:
                    raise
                time.sleep(1)

    def test_round_trip_is_fixed_and_visible_to_examiner(self):
        response = self.submit()
        self.assertEqual(response.status_code, 202, response.text)
        receipt = response.json()
        self.assertEqual(receipt, self.service.get_session_receipt(self.sid))
        self.assertEqual(self.submit("another-key").json(), receipt)
        self.finish_cleanup()
        self.assertEqual(
            self.lifecycle.get_session(self.sid).deadline_at, self.started.deadline_at
        )
        self.assertIsNotNone(self.lifecycle.get_session(self.sid).sandbox_deleted_at)
        with self.assertRaises(NotFound):
            self.driver.containers.get(self.container.id)
        self.driver.volumes.get(self.resource_key + "-data")
        self.assertEqual(
            self.client.post(
                "/api/v1/admin/login",
                json={"username": "examiner", "password": "integration-password-123"},
            ).status_code,
            200,
        )
        detail = self.client.get(
            "/api/v1/admin/submissions/" + receipt["submission_id"]
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        files = {entry["path"]: entry["content"] for entry in detail.json()["files"]}
        self.assertEqual(set(files), self.expected_paths)
        self.assertIn(MARKER, files["src/booking.py"])
        self.assertEqual(files["tests/test_candidate.py"], "")
        self.assertIsNone(detail.json()["public_test_output"])

    def test_snapshot_failure_keeps_pause_and_original_binding_for_retry(self):
        original = self.lifecycle.get_session(self.sid)
        with patch.object(
            self.snapshots,
            "write",
            side_effect=SnapshotStorageError("injected disk failure"),
        ):
            response = self.submit()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIsNone(self.service.get_session_receipt(self.sid))
        self.container.reload()
        self.assertEqual(self.container.status, "paused")
        failed = self.lifecycle.get_session(self.sid)
        self.assertEqual(failed.status, "submission_failed")
        self.assertEqual(
            (failed.sandbox_id, failed.deadline_at),
            (original.sandbox_id, original.deadline_at),
        )
        self.assertEqual(self.submit("retry").status_code, 202)
        self.finish_cleanup()

    def test_cleanup_failure_keeps_receipt_and_retries_deletion(self):
        with (
            patch.object(
                self.sandbox,
                "delete_sandbox",
                side_effect=TimeoutError("injected cleanup timeout"),
            ),
            self.assertLogs("app.submissions.coordinator", level="WARNING"),
        ):
            response = self.submit()
        self.assertEqual(response.status_code, 202, response.text)
        receipt = response.json()
        self.assertEqual(
            [view.session_id for view in self.lifecycle.list_cleanup_candidates()],
            [self.sid],
        )
        self.container.reload()
        self.assertEqual(self.container.status, "paused")
        self.finish_cleanup()
        self.finish_cleanup()
        self.assertEqual(self.lifecycle.list_cleanup_candidates(), [])
        self.assertEqual(receipt, self.service.get_session_receipt(self.sid))
        self.driver.volumes.get(self.resource_key + "-data")

    def test_wrong_resource_identity_is_rejected_before_export(self):
        self.container.rename(self.resource_key + "-wrong")
        try:
            response = self.submit()
            self.assertEqual(response.status_code, 503, response.text)
            self.assertIsNone(self.service.get_session_receipt(self.sid))
            self.assertEqual(
                self.lifecycle.get_session(self.sid).status, "submission_failed"
            )
        finally:
            self.container.rename(self.resource_key)
        self.assertEqual(self.submit("retry").status_code, 202)
        self.finish_cleanup()


@unittest.skipUnless(
    os.environ.get("VIBEEVAL_RUN_DB_TESTS") == "1", "Requires PostgreSQL"
)
class DockerBridgeIntegrationTests(SubmissionRoundTrip, unittest.TestCase):
    setUpClass = classmethod(database_tests.PostgreSQLTests.setUpClass.__func__)
    drop_test_database = classmethod(
        database_tests.PostgreSQLTests.drop_test_database.__func__
    )
    image_ref = IMAGE
    backend_ref = BACKEND
    expected_paths = {"src/booking.py", "tests/test_candidate.py"}

    def make_driver(self):
        return DockerDouble()

    def check_health_and_write_answer(self):
        self.assertEqual(self.container.attrs["HostConfig"]["Runtime"], "runsc")
        self.assertEqual(self.driver.creates, 1)
        self.container.on_pause = lambda: self.assertEqual(
            self.lifecycle.get_session(self.sid).status, "submitting"
        )
        self.container.on_remove = lambda: self.assertIsNotNone(
            self.service.get_session_receipt(self.sid)
        )

    def cleanup_test_resources(self):
        pass


@unittest.skipUnless(
    os.environ.get("VIBEEVAL_RUN_GVISOR_TESTS") == "1",
    "Set VIBEEVAL_RUN_GVISOR_TESTS=1 on an isolated Linux/runsc test host",
)
class GvisorSubmissionIntegrationTests(SubmissionRoundTrip, unittest.TestCase):
    expected_paths = {
        "README.md",
        "requirements.txt",
        "src/main.py",
        "src/booking.py",
        "tests/test_public.py",
        "tests/test_candidate.py",
    }
    drop_test_database = classmethod(
        database_tests.PostgreSQLTests.drop_test_database.__func__
    )

    @classmethod
    def setUpClass(cls):
        cls.image_ref = os.environ.get("VIBEEVAL_GVISOR_IMAGE_REF", "")
        cls.backend_ref = os.environ.get(
            "SANDBOX_BACKEND_REF", "docker-gvisor:integration-tests"
        )
        cls.docker_url = os.environ.get(
            "SANDBOX_DOCKER_URL", "unix:///var/run/docker.sock"
        )
        if os.environ.get("VIBEEVAL_RUN_DB_TESTS") != "1" or os.name != "posix":
            raise RuntimeError(
                "Real gVisor acceptance requires Linux and enabled PostgreSQL tests"
            )
        if re.fullmatch(r"(?:[^\s@]+@)?sha256:[0-9a-f]{64}", cls.image_ref) is None:
            raise RuntimeError(
                "VIBEEVAL_GVISOR_IMAGE_REF must be a prebuilt immutable trusted image"
            )
        if not cls.docker_url.startswith("unix://"):
            raise RuntimeError("A local test Docker Unix socket is required")
        with closing(
            docker.DockerClient(base_url=cls.docker_url, timeout=20)
        ) as client:
            if "runsc" not in client.info().get("Runtimes", {}):
                raise RuntimeError(
                    "Docker has no runsc runtime; acceptance cannot use runc"
                )
            client.images.get(cls.image_ref)
        database_tests.PostgreSQLTests.setUpClass.__func__(cls)

    def make_driver(self):
        client = docker.DockerClient(base_url=self.docker_url, timeout=20)
        self.addCleanup(client.close)
        return client

    def make_submission_adapter(self):
        return SandboxRuntime(backend_ref=self.backend_ref, docker_url=self.docker_url)

    def check_health_and_write_answer(self):
        self.container.reload()
        self.assertEqual(self.container.attrs["HostConfig"]["Runtime"], "runsc")
        result = self.container.exec_run(
            [
                "python",
                "-c",
                "import os; from pathlib import Path; assert os.getuid() == 1000; "
                "p = Path('/workspace/src/booking.py'); "
                f"p.write_text(p.read_text() + '\\n{MARKER}\\n'); "
                "assert Path('/workspace/tests/test_public.py').is_file()",
            ]
        )
        self.assertEqual(result.exit_code, 0, result.output.decode(errors="replace"))

    def cleanup_test_resources(self):
        # This probe owns a random session and only removes its labelled artifacts.
        try:
            obj = self.driver.containers.get(self.resource_key)
        except NotFound:
            obj = None
        if obj is not None:
            if obj.labels.get("vibeeval.session") != self.sid:
                raise RuntimeError("Refusing to clean a foreign test container")
            obj.remove(force=True, v=False)
        try:
            volume = self.driver.volumes.get(self.resource_key + "-data")
        except NotFound:
            return
        if volume.attrs.get("Labels", {}).get("vibeeval.session") != self.sid:
            raise RuntimeError("Refusing to clean a foreign test volume")
        volume.remove()

import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app.db.lifecycle import LifecycleConflict
from app.exams.router import require_candidate_session
from app.main import create_app
from app.workspace.files import ALLOWED_FILES
from app.workspace.router import router
from app.workspace.service import RevisionStoreUnavailable, WorkspaceReader


class WorkspaceReaderTests(unittest.TestCase):
    def setUp(self):
        self.locked = Mock()
        self.locked.require_writable.return_value = "view"
        self.locked.observe_file_revision.return_value = 2
        self.in_lock = False
        self.sandbox = Mock()
        self.sandbox.read_file.side_effect = self.content
        self.sandbox.read_files.side_effect = lambda view: {
            path: self.content(view, path) for path in ALLOWED_FILES
        }
        self.service = WorkspaceReader(
            SimpleNamespace(lock_session=self.lock), self.sandbox
        )

    @contextmanager
    def lock(self, sid):
        self.in_lock = True
        try:
            yield self.locked
        finally:
            self.in_lock = False

    def content(self, view, path):
        self.assertTrue(self.in_lock)
        return "example"

    def test_read_uses_same_lock_and_persistent_revision(self):
        self.assertEqual(
            self.service.read("ses", "src/booking.py"),
            {"path": "src/booking.py", "content": "example", "version": 2},
        )
        self.assertFalse(self.in_lock)
        self.locked.observe_file_revision.assert_called_once()

    def test_tree_uses_release_manifest_permissions(self):
        files = self.service.tree("ses")["files"]
        self.assertEqual(len(files), 6)
        self.assertEqual(
            {f["path"] for f in files if not f["readonly"]},
            {"src/booking.py", "tests/test_candidate.py"},
        )

    def test_missing_revision_backend_never_reads_sandbox(self):
        self.locked = SimpleNamespace(require_writable=lambda: "view")
        with self.assertRaises(RevisionStoreUnavailable):
            self.service.read("ses", "src/booking.py")
        self.sandbox.read_file.assert_not_called()

    def test_inactive_session_rejected_before_read(self):
        self.locked.require_writable.side_effect = LifecycleConflict()
        with self.assertRaises(LifecycleConflict):
            self.service.tree("ses")
        self.sandbox.read_files.assert_not_called()


class WorkspaceReadHttpTests(unittest.TestCase):
    def test_authentication_and_revision_unavailable(self):
        runtime = Mock()
        app = create_app(
            {}, entry_store=SimpleNamespace(factory=Mock()), submission_sandbox=runtime
        )
        app.include_router(router)
        app.dependency_overrides[require_candidate_session] = lambda: {
            "session_id": "ses"
        }
        locked = SimpleNamespace(require_writable=lambda: "view")

        @contextmanager
        def lock(sid):
            yield locked

        with (
            patch(
                "app.workspace.router.SessionLifecycle",
                return_value=SimpleNamespace(lock_session=lock),
            ),
            TestClient(app) as client,
        ):
            for url in (
                "/api/v1/sessions/ses/files",
                "/api/v1/sessions/ses/file?path=src/booking.py",
            ):
                self.assertEqual(client.get(url).status_code, 403)
                response = client.get(url, headers={"X-VibeEval-Request": "1"})
                self.assertEqual(response.status_code, 503)
                self.assertEqual(
                    response.json()["detail"]["code"], "FILE_REVISIONS_UNAVAILABLE"
                )
            self.assertEqual(
                client.get(
                    "/api/v1/sessions/ses/file?path=../secret",
                    headers={"X-VibeEval-Request": "1"},
                ).status_code,
                422,
            )
        runtime.read_file.assert_not_called()
        runtime.read_files.assert_not_called()

import hashlib
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

from docker.errors import NotFound

from app.sandbox.safe_save import remove_writer, save_file
from app.workspace.files import ReadOnlyFile, observe_revision
from app.workspace.service import SaveConflict, WorkspaceReader


class SaveServiceTests(unittest.TestCase):
    def setUp(self):
        self.revision = None
        self.sandbox = Mock()
        self.sandbox.read_file.return_value = "old"
        self.sandbox.save_file.return_value = {"saved": True}
        self.committed = False

        def observe(path, digest):
            from app.workspace.files import FileRevision

            if self.revision is None:
                self.revision = FileRevision(1, digest)
            elif self.revision.digest != digest:
                self.revision = FileRevision(self.revision.version + 1, digest)
            return self.revision.version

        @contextmanager
        def lock(sid):
            yield SimpleNamespace(
                require_writable=lambda: "view", observe_file_revision=observe
            )
            self.committed = True

        self.service = WorkspaceReader(SimpleNamespace(lock_session=lock), self.sandbox)

    def test_save_returns_committed_new_version(self):
        result = self.service.save("ses", "src/booking.py", 1, "new")
        self.assertEqual(result["new_version"], 2)
        self.assertTrue(self.committed)

    def test_observed_shell_edit_rejects_stale_version(self):
        self.revision = observe_revision(None, "earlier")
        with self.assertRaises(SaveConflict) as error:
            self.service.save("ses", "src/booking.py", 1, "new")
        self.assertEqual(error.exception.version, 2)
        self.assertTrue(self.committed)
        self.sandbox.save_file.assert_not_called()

    def test_shell_edit_between_read_and_pause_is_conflict(self):
        self.sandbox.save_file.return_value = {"saved": False, "content": "shell edit"}
        with self.assertRaises(SaveConflict) as error:
            self.service.save("ses", "src/booking.py", 1, "new")
        self.assertEqual(error.exception.version, 2)
        self.assertTrue(self.committed)

    def test_failure_never_returns_success_or_commits_revision(self):
        self.sandbox.save_file.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.service.save("ses", "src/booking.py", 1, "new")
        self.assertFalse(self.committed)

    def test_readonly_and_invalid_versions_do_not_access_sandbox(self):
        with self.assertRaises(ReadOnlyFile):
            self.service.save("ses", "README.md", 1, "new")
        with self.assertRaises(ValueError):
            self.service.save("ses", "src/booking.py", True, "new")
        self.sandbox.read_file.assert_not_called()


class AtomicSaveTests(unittest.TestCase):
    def setUp(self):
        self.view = SimpleNamespace(
            deadline_at=4102444800,
            status="in_progress",
            session_id="ses",
            sandbox_id="id",
            sandbox_resource_key="sandbox",
            sandbox_image_ref="image",
        )
        self.adapter = Mock()
        self.original = self.adapter._container.return_value
        self.original.status = "running"
        self.original.pause.side_effect = lambda: setattr(
            self.original, "status", "paused"
        )
        self.original.attrs = {
            "Image": "image-id",
            "Mounts": [
                {"Destination": "/workspace", "Type": "volume", "Name": "sandbox-data"}
            ],
        }
        self.adapter.read_file.side_effect = ["old", "new"]
        volume = self.adapter.client.volumes.get.return_value
        volume.name = "sandbox-data"
        volume.attrs = {
            "Labels": {"vibeeval.session": "ses", "vibeeval.image": "image-id"}
        }
        self.writer = self.adapter.client.containers.create.return_value
        self.writer.exec_run.return_value = SimpleNamespace(exit_code=0)
        self.digest = hashlib.sha256(b"old").hexdigest()

    def test_pauses_before_write_removes_writer_before_resume(self):
        with patch("app.sandbox.safe_save.remove_writer") as cleanup:
            self.assertEqual(
                save_file(
                    self.adapter, self.view, "src/booking.py", self.digest, "new"
                ),
                {"saved": True},
            )
        self.original.pause.assert_called_once()
        self.original.unpause.assert_called_once()
        self.assertEqual(cleanup.call_count, 2)
        kwargs = self.adapter.client.containers.create.call_args.kwargs
        self.assertEqual(kwargs["runtime"], "runsc")
        self.assertEqual(kwargs["cap_add"], ["CHOWN"])
        self.assertEqual(kwargs["network_mode"], "none")

    def test_writer_failure_keeps_original_paused(self):
        self.writer.exec_run.return_value = SimpleNamespace(exit_code=1)
        with (
            patch("app.sandbox.safe_save.remove_writer") as cleanup,
            self.assertRaises(ValueError),
        ):
            save_file(self.adapter, self.view, "src/booking.py", self.digest, "new")
        self.original.unpause.assert_not_called()
        self.assertEqual(cleanup.call_count, 2)

    def test_conflict_preserves_content_and_creates_no_writer(self):
        self.adapter.read_file.side_effect = ["shell edit"]
        with patch("app.sandbox.safe_save.remove_writer"):
            result = save_file(
                self.adapter, self.view, "src/booking.py", self.digest, "new"
            )
        self.assertEqual(result, {"saved": False, "content": "shell edit"})
        self.adapter.client.containers.create.assert_not_called()

    def test_cleanup_failure_never_resumes(self):
        with (
            patch(
                "app.sandbox.safe_save.remove_writer",
                side_effect=[None, RuntimeError()],
            ),
            self.assertRaises(RuntimeError),
        ):
            save_file(self.adapter, self.view, "src/booking.py", self.digest, "new")
        self.original.unpause.assert_not_called()

    def test_foreign_writer_is_not_removed(self):
        client = Mock()
        client.containers.get.return_value.attrs = {"Config": {"Labels": {}}}
        with self.assertRaises(ValueError):
            remove_writer(client, self.view)
        client.containers.get.return_value.remove.assert_not_called()

    def test_owned_writer_removed_and_absence_verified(self):
        client, writer = Mock(), Mock()
        writer.name = "sandbox-writer"
        writer.attrs = {
            "Image": "image-id",
            "Config": {
                "Labels": {
                    "vibeeval.session": "ses",
                    "vibeeval.role": "workspace-writer",
                    "vibeeval.original": "id",
                }
            },
        }
        client.images.get.return_value.id = "image-id"
        client.containers.get.side_effect = [writer, NotFound("gone")]
        remove_writer(client, self.view)
        writer.remove.assert_called_once_with(force=True, v=False)

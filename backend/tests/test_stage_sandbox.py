"""Docker boundary tests with explicit doubles; not runsc acceptance evidence."""

import io
import tarfile
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from docker.errors import NotFound

from app.db.lifecycle import SessionView
from app.stage_resources.sandbox import StageSandboxAdapter, StageSandboxRuntime
from app.stage_resources.types import ResourceSpec, WorkspaceView
from app.workspace.files import InvalidFilePath, ReadOnlyFile

IMAGE = "sha256:" + "a" * 64


def view(**changes):
    values = dict.fromkeys(SessionView.__dataclass_fields__)
    values.update(
        session_id="workspace",
        status="in_progress",
        task_version_id="v1",
        admitted_at=1,
        started_at=1,
        deadline_at=2000000000,
        sandbox_resource_key="vbe-stage-fixture",
        sandbox_image_ref=IMAGE,
        sandbox_backend_ref="fixture",
        allowed_files=("solution.py", "README.md"),
        writable_files=("solution.py",),
        read_only=False,
    )
    values.update(changes)
    return WorkspaceView(**values)


def archive(files, *, symbolic=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as target:
        for name, content in files.items():
            entry = tarfile.TarInfo(name)
            data = content.encode()
            entry.size = len(data)
            if name == symbolic:
                entry.type, entry.linkname, entry.size = tarfile.SYMTYPE, "/outside", 0
            target.addfile(entry, None if name == symbolic else io.BytesIO(data))
    return iter([output.getvalue()]), {}


class StageSandboxTests(unittest.TestCase):
    def setUp(self):
        self.events, self.objects, self.volumes = [], {}, {}
        self.client = Mock()
        self.client.images.get.return_value = SimpleNamespace(id=IMAGE)
        self.client.volumes.create.side_effect = self.volume
        self.client.volumes.get.side_effect = lambda name: self.volumes[name]
        self.client.containers.get.side_effect = self.get
        self.client.containers.create.side_effect = self.create
        self.adapter, self.view = StageSandboxAdapter(self.client, "fixture"), view()

    def volume(self, name, labels):
        return self.volumes.setdefault(
            name, SimpleNamespace(name=name, attrs={"Labels": labels})
        )

    def get(self, identity):
        for obj in self.objects.values():
            if identity in (obj.id, obj.name) and not obj.deleted:
                return obj
        raise NotFound("fixture absent")

    def create(self, image, command, **config):
        obj = SimpleNamespace(
            id="container-" + str(len(self.objects)),
            name=config["name"],
            status="created",
            deleted=False,
        )
        obj.attrs = {
            "Image": image,
            "Config": {"Labels": config["labels"], "User": config["user"]},
            "HostConfig": {
                "Runtime": config["runtime"],
                "NetworkMode": config["network_mode"],
                "ReadonlyRootfs": config["read_only"],
                "CapDrop": config["cap_drop"],
                "CapAdd": config.get("cap_add"),
                "SecurityOpt": config.get("security_opt", []),
            },
            "Mounts": [
                {
                    "Name": name,
                    "Destination": mount["bind"],
                    "RW": mount["mode"] == "rw",
                }
                for name, mount in config["volumes"].items()
            ],
        }

        def start():
            obj.status = "running"

        def pause():
            self.events.append("pause")
            obj.status = "paused"

        def remove(**kwargs):
            self.events.append("remove")
            obj.deleted = True

        obj.start, obj.pause, obj.remove = start, pause, remove
        obj.reload = lambda: None
        obj.exec_run = Mock(return_value=SimpleNamespace(exit_code=0))
        obj.get_archive = Mock(
            return_value=archive(
                {
                    "workspace/solution.py": "answer",
                    "workspace/README.md": "task",
                    "workspace/cache.bin": "ignored",
                }
            )
        )
        self.objects[obj.id] = obj
        return obj

    def prepared(self, **changes):
        self.view = view(**changes)
        identity = self.adapter.ensure_bound(self.view)
        self.view = replace(self.view, sandbox_id=identity)
        return self.get(identity)

    def test_preparation_uses_runsc_and_read_only_mount(self):
        obj = self.prepared(read_only=True)
        self.assertEqual(obj.attrs["HostConfig"]["Runtime"], "runsc")
        self.assertEqual(obj.attrs["HostConfig"]["NetworkMode"], "none")
        self.assertFalse(obj.attrs["Mounts"][0]["RW"])
        self.assertTrue(self.adapter.check_ready(self.view))
        self.assertEqual(self.adapter.ensure_bound(self.view), obj.id)
        self.assertEqual(len(self.objects), 1)

    def test_close_pauses_disarms_helpers_and_removes_old_pid_namespace(self):
        obj = self.prepared()
        with (
            patch(
                "app.stage_resources.sandbox.stop_bound_executions",
                side_effect=lambda *a, **kw: self.events.append("helpers"),
            ),
            patch(
                "app.stage_resources.sandbox.remove_writer",
                side_effect=lambda *a: self.events.append("writer"),
            ),
        ):
            self.adapter.close_workspace(self.view)
            self.assertEqual(self.events, ["pause", "helpers", "writer", "remove"])
            self.assertTrue(obj.deleted)
            self.adapter.close_workspace(self.view)
        self.assertEqual(self.events.count("remove"), 1)

    def test_foreign_runtime_or_mount_rejected_before_stop(self):
        obj = self.prepared()
        obj.attrs["HostConfig"]["Runtime"] = "runc"
        with self.assertRaises(ValueError):
            self.adapter.close_workspace(self.view)
        self.assertEqual(self.events, [])
        obj.attrs["HostConfig"]["Runtime"] = "runsc"
        obj.attrs["Mounts"][0]["Name"] = "foreign-volume"
        with self.assertRaises(ValueError):
            self.adapter.close_workspace(self.view)
        self.assertEqual(self.events, [])

    def test_export_only_published_regular_files_and_remove_exporter(self):
        self.volume(
            self.view.sandbox_resource_key + "-data",
            {"vibeeval.session": self.view.session_id, "vibeeval.image": IMAGE},
        )
        files = self.adapter.export_volume(self.view)
        self.assertEqual({f.path for f in files}, set(self.view.allowed_files))
        self.assertTrue(all(obj.deleted for obj in self.objects.values()))

    def test_bad_archive_keeps_exporter_frozen_and_foreign_volume_rejected(self):
        self.volume(
            self.view.sandbox_resource_key + "-data",
            {"vibeeval.session": self.view.session_id, "vibeeval.image": IMAGE},
        )
        original = self.create

        def bad(*args, **kwargs):
            obj = original(*args, **kwargs)
            obj.get_archive.return_value = archive(
                {"workspace/solution.py": "x", "workspace/README.md": "y"},
                symbolic="workspace/solution.py",
            )
            return obj

        self.client.containers.create.side_effect = bad
        with self.assertRaises(ValueError):
            self.adapter.export_volume(self.view)
        exporter = next(iter(self.objects.values()))
        self.assertEqual(exporter.status, "paused")
        self.assertFalse(exporter.deleted)
        self.volumes[self.view.sandbox_resource_key + "-data"].attrs["Labels"] = {
            "owner": "other"
        }
        with self.assertRaises(ValueError):
            self.adapter.export_volume(self.view)

    def test_manifest_checks_precede_runtime_rpc(self):
        runtime = StageSandboxRuntime(backend_ref="fixture")
        runtime._spawn_call = Mock()
        with self.assertRaises(InvalidFilePath):
            runtime.read_file(self.view, "../private")
        with self.assertRaises(ReadOnlyFile):
            runtime.save_file(self.view, "README.md", "a" * 64, "x")
        runtime._spawn_call.assert_not_called()
        runtime.close_workspace(self.view)
        self.assertEqual(runtime._spawn_call.call_args.args[1], "close_workspace")

    def test_registered_root_and_nested_paths_supported(self):
        obj = self.prepared(allowed_files=("nested/lib/code.py", "solution.py"))
        obj.get_archive.return_value = archive({"code.py": "public"})
        self.assertEqual(
            self.adapter.read_file(self.view, "nested/lib/code.py"), "public"
        )
        with self.assertRaises(InvalidFilePath):
            self.adapter.read_file(self.view, "nested/../private")

    def test_registry_requires_immutable_image_and_safe_manifest(self):
        for image, files, writable in (
            ("python:latest", ("a.py",), ()),
            (IMAGE, ("../private",), ()),
            (IMAGE, ("a.py",), ("b.py",)),
        ):
            with self.subTest(image=image, files=files):
                with self.assertRaises(ValueError):
                    ResourceSpec(
                        task_version="v1",
                        image_ref=image,
                        files=files,
                        writable=writable,
                    )

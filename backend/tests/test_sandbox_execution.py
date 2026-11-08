"""Execution fencing and trusted receipts without running candidate code on host."""

import base64
import io
import json
import tarfile
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from docker.errors import NotFound

from app.db.lifecycle import SessionView
from app.sandbox.bound_adapter import BoundSandboxAdapter
from app.sandbox.execution import names, stop_bound_executions

IMAGE = "sha256:" + "a" * 64


def session_view(**values):
    defaults = dict.fromkeys(SessionView.__dataclass_fields__)
    defaults.update(
        session_id="execution-test-session",
        status="in_progress",
        admitted_at=1,
        started_at=1,
        deadline_at=2000000000,
        sandbox_id="original-container",
        sandbox_resource_key="execution-test-resource",
        sandbox_image_ref=IMAGE,
        sandbox_backend_ref="execution-test-backend",
    )
    defaults.update(values)
    return SessionView(**defaults)


def progress_archive(value, *, uid=0, symbolic=False, name="progress.json"):
    data = json.dumps(value).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        entry = tarfile.TarInfo(name)
        entry.uid, entry.mode, entry.size = uid, 0o600, len(data)
        if symbolic:
            entry.type, entry.linkname = tarfile.SYMTYPE, "/workspace/answer"
        archive.addfile(entry, io.BytesIO(data))
    return iter([output.getvalue()]), {}


class ExecutionAdapterTests(unittest.TestCase):
    def setUp(self):
        self.view = session_view()
        self.config = {
            "operation_id": "a" * 32,
            "request_sha256": "b" * 64,
            "argv": ["python", "-c", "print('fixture only')"],
            "cwd": ".",
            "deadline_at": 2000000000,
            "timeout_seconds": 30,
            "max_output_bytes": 1024,
            "expected_digests": {},
        }
        self.client, self.original = Mock(), Mock()
        self.original.id, self.original.name = (
            self.view.sandbox_id,
            self.view.sandbox_resource_key,
        )
        self.original.status = "running"
        self.original.attrs = {
            "Image": IMAGE,
            "Config": {"Labels": {"vibeeval.session": self.view.session_id}},
        }
        self.objects = {self.original.id: self.original}
        self.volumes = {}
        self.volume_create(
            self.view.sandbox_resource_key + "-data",
            {"vibeeval.session": self.view.session_id, "vibeeval.image": IMAGE},
        )
        self.client.images.get.return_value = SimpleNamespace(id=IMAGE)
        self.client.containers.get.side_effect = self.get
        self.client.containers.create.side_effect = self.create
        self.client.volumes.get.side_effect = self.volume_get
        self.client.volumes.create.side_effect = self.volume_create
        self.adapter = BoundSandboxAdapter(self.client, self.view.sandbox_backend_ref)

    def get(self, identity):
        for obj in self.objects.values():
            if identity in (obj.id, obj.name) and not obj.deleted:
                return obj
        raise NotFound("owned fixture container absent")

    def volume_get(self, name):
        if name not in self.volumes:
            raise NotFound("owned fixture volume absent")
        return self.volumes[name]

    def volume_create(self, name, labels):
        if name not in self.volumes:
            volume = SimpleNamespace(name=name, attrs={"Labels": labels})
            volume.remove = Mock(side_effect=lambda: self.volumes.pop(name))
            self.volumes[name] = volume
        return self.volumes[name]

    def create(self, image, command, **kwargs):
        obj = Mock()
        obj.id, obj.name, obj.status, obj.deleted = (
            "helper-ID",
            kwargs["name"],
            "created",
            False,
        )
        obj.attrs = {
            "Image": image,
            "Config": {"Labels": kwargs["labels"], "User": kwargs["user"]},
            "HostConfig": {
                "Runtime": kwargs["runtime"],
                "NetworkMode": kwargs["network_mode"],
                "ReadonlyRootfs": kwargs["read_only"],
                "CapAdd": kwargs["cap_add"],
                "CapDrop": kwargs["cap_drop"],
                "SecurityOpt": kwargs["security_opt"],
                "RestartPolicy": kwargs["restart_policy"],
            },
            "Mounts": [
                {"Destination": settings["bind"], "Name": name}
                for name, settings in kwargs["volumes"].items()
            ],
        }
        obj.start.side_effect = lambda: setattr(obj, "status", "running")
        obj.stop.side_effect = lambda **kw: setattr(obj, "status", "exited")
        obj.remove.side_effect = lambda **kw: setattr(obj, "deleted", True)
        obj.get_archive.side_effect = NotFound("no receipt yet")
        self.objects[obj.id] = obj
        return obj

    def prepare(self):
        # The fixture never invokes Popen, exec_run or the supervisor locally.
        return self.adapter.prepare_execution(self.view, self.config)["handle"]

    def receipt(self, **values):
        return {
            "operation_id": self.config["operation_id"],
            "request_sha256": self.config["request_sha256"],
            "status": "succeeded",
            "output_complete": True,
            "stdout_b64": base64.b64encode(b"output").decode(),
            **values,
        }

    def test_prepare_is_idempotent_create_only_and_keeps_code_in_runsc(self):
        self.original.deleted = False
        handle = self.prepare()
        self.assertEqual(self.prepare(), handle)
        self.client.containers.create.assert_called_once()
        obj = self.get(handle)
        obj.start.assert_not_called()
        self.original.exec_run.assert_not_called()
        args, options = self.client.containers.create.call_args
        self.assertEqual(options["runtime"], "runsc")
        self.assertEqual(options["network_mode"], "none")
        self.assertEqual(options["cap_drop"], ["ALL"])
        self.assertEqual(options["restart_policy"], {"Name": "no"})
        self.assertNotIn("environment", options)
        self.assertEqual(args[1][:3], ["python", "-I", "-c"])
        # Syntax-check transported code without running it on the API host.
        compile(args[1][3], "fixed-container-supervisor", "exec")

    def test_start_once_and_never_restart_exited_or_running_helper(self):
        self.original.deleted = False
        handle = self.prepare()
        obj = self.get(handle)
        self.adapter.start_execution(self.view, self.config, handle)
        for status in ("running", "exited", "dead"):
            obj.status = status
            with self.assertRaisesRegex(ValueError, "never restart"):
                self.adapter.start_execution(self.view, self.config, handle)
        obj.start.assert_called_once()

    def test_frozen_original_blocks_even_fresh_dispatch(self):
        self.original.deleted = False
        handle = self.prepare()
        self.original.status = "paused"
        with self.assertRaisesRegex(ValueError, "frozen"):
            self.adapter.start_execution(self.view, self.config, handle)
        self.get(handle).start.assert_not_called()

    def test_created_helper_is_unknown_until_start_acceptance_is_known(self):
        self.original.deleted = False
        handle = self.prepare()
        value = self.adapter.inspect_execution(self.view, self.config, handle)
        self.assertEqual(value["status"], "unknown")
        self.assertEqual(value["reason_code"], "START_NOT_CONFIRMED")
        self.assertFalse(value["processes_stopped"])
        self.get(handle).start.assert_not_called()

    def test_foreign_identity_or_weakened_capabilities_fail_before_start(self):
        self.original.deleted = False
        handle = self.prepare()
        obj = self.get(handle)
        obj.attrs["HostConfig"]["CapAdd"].append("SYS_ADMIN")
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            self.adapter.start_execution(self.view, self.config, handle)
        obj.start.assert_not_called()
        with self.assertRaises(ValueError):
            names("../another-resource")

    def test_live_helper_cannot_claim_stopped_based_only_on_receipt(self):
        self.original.deleted = False
        handle = self.prepare()
        obj = self.get(handle)
        obj.status = "running"
        obj.get_archive.side_effect = lambda path: progress_archive(self.receipt())
        value = self.adapter.inspect_execution(self.view, self.config, handle)
        self.assertEqual(value["status"], "running")
        self.assertFalse(value["processes_stopped"])
        self.assertFalse(value["output_complete"])
        obj.status = "exited"
        self.assertTrue(
            self.adapter.inspect_execution(self.view, self.config, handle)[
                "processes_stopped"
            ]
        )

    def test_untrusted_malformed_or_over_cap_receipts_return_unknown_with_stop_proof(
        self,
    ):
        self.original.deleted = False
        handle = self.prepare()
        obj = self.get(handle)
        obj.status = "exited"
        receipts = (
            lambda: progress_archive(self.receipt(), uid=1000),
            lambda: progress_archive(self.receipt(), symbolic=True),
            lambda: progress_archive(self.receipt(request_sha256="wrong")),
            lambda: progress_archive(
                self.receipt(stdout_b64=base64.b64encode(b"x" * 1025).decode())
            ),
            lambda: progress_archive([]),
            lambda: (iter([b"not a tar archive"]), {}),
        )
        for receipt in receipts:
            with self.subTest(receipt=receipt):
                obj.get_archive.side_effect = lambda path: receipt()
                value = self.adapter.inspect_execution(self.view, self.config, handle)
                self.assertEqual(value["status"], "unknown")
                self.assertEqual(value["reason_code"], "RECEIPT_INVALID")
                self.assertTrue(value["processes_stopped"])

    def test_cancel_created_disarms_late_start_and_removes_private_volume(self):
        self.original.deleted = False
        handle = self.prepare()
        value = self.adapter.cancel_execution(self.view, self.config, handle)
        self.assertEqual(value["status"], "cancelled")
        self.assertNotIn(names(self.config["operation_id"])[1], self.volumes)
        with self.assertRaises(NotFound):
            self.adapter.start_execution(self.view, self.config, handle)
        self.original.stop.assert_not_called()

    def test_cancel_running_uses_private_marker_and_stops_only_its_helper(self):
        self.original.deleted = False
        handle = self.prepare()
        obj = self.get(handle)
        obj.status = "running"
        value = self.adapter.cancel_execution(self.view, self.config, handle)
        obj.put_archive.assert_called_once()
        obj.stop.assert_called_once_with(timeout=3)
        self.assertTrue(value["processes_stopped"])
        self.original.stop.assert_not_called()

    def test_submission_cleanup_stops_helpers_without_resuming_original(self):
        self.original.deleted = False
        handle = self.prepare()
        obj = self.get(handle)
        obj.status = "running"
        self.client.containers.list.return_value = [obj]
        stop_bound_executions(self.adapter, replace(self.view, status="submitting"))
        obj.stop.assert_called_once()
        self.original.unpause.assert_not_called()
        stop_bound_executions(
            self.adapter, replace(self.view, status="submitted"), remove=True
        )
        self.assertTrue(obj.deleted)
        self.assertNotIn(names(self.config["operation_id"])[1], self.volumes)

    def edit_config(self):
        self.original.deleted = False
        self.config["kind"] = "file_edit"
        self.config["edit"] = {
            "path": "src/booking.py",
            "before_sha256": "c" * 64,
            "after_sha256": "d" * 64,
            "content_b64": base64.b64encode(b"# large file\n" * 16000).decode(),
        }

    def control_fixture(self, obj):
        controls = {}

        def put(path, data):
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
                entry = archive.getmembers()[0]
                controls[entry.name] = json.load(archive.extractfile(entry))

        def get(path):
            name = path.split("/")[-1]
            if name not in controls:
                raise NotFound("no private fixture control yet")
            return progress_archive(controls[name], name=name)

        obj.put_archive.side_effect = put
        obj.get_archive.side_effect = get
        self.original.pause.side_effect = lambda: setattr(
            self.original, "status", "paused"
        )
        self.original.unpause.side_effect = lambda: setattr(
            self.original, "status", "running"
        )

    def test_file_helper_has_only_fixed_write_capability_and_chunked_large_config(self):
        self.edit_config()
        self.prepare()
        args, options = self.client.containers.create.call_args
        self.assertEqual(options["cap_add"], ["CHOWN"])
        chunks = args[1][4:]
        self.assertGreater(len(chunks), 1)
        self.assertLessEqual(max(map(len, chunks)), 32768)
        decoded = json.loads(base64.b64decode("".join(chunks)))
        self.assertEqual(decoded["edit"], self.config["edit"])
        self.original.pause.assert_not_called()

    def test_file_start_persists_pause_ownership_and_never_unpauses_implicitly(self):
        self.edit_config()
        handle = self.prepare()
        obj = self.get(handle)
        self.control_fixture(obj)
        self.adapter.start_execution(self.view, self.config, handle)
        self.assertEqual(self.original.status, "paused")
        receipt = self.adapter.inspect_execution(self.view, self.config, handle)
        self.assertTrue(receipt["pause_owned"])
        self.original.unpause.assert_not_called()
        with self.assertRaises(ValueError):
            self.adapter.resume_workspace(replace(self.view, status="submitting"))
        self.original.unpause.assert_not_called()

    def test_preexisting_pause_is_not_claimed_by_an_edit(self):
        self.edit_config()
        handle = self.prepare()
        self.original.status = "paused"
        with self.assertRaises(ValueError):
            self.adapter.start_execution(self.view, self.config, handle)
        self.get(handle).put_archive.assert_not_called()
        receipt = self.adapter.cancel_execution(self.view, self.config, handle)
        self.assertFalse(receipt["pause_owned"])
        self.original.unpause.assert_not_called()

    def test_cancel_unconfirmed_file_start_retains_pause_proof_after_disarming(self):
        self.edit_config()
        handle = self.prepare()
        obj = self.get(handle)
        self.control_fixture(obj)
        obj.start.side_effect = TimeoutError("start reply lost")
        with self.assertRaises(TimeoutError):
            self.adapter.start_execution(self.view, self.config, handle)
        receipt = self.adapter.cancel_execution(self.view, self.config, handle)
        self.assertTrue(receipt["pause_owned"])
        self.assertTrue(receipt["processes_stopped"])
        self.adapter.resume_workspace(self.view)
        self.assertEqual(self.original.status, "running")


if __name__ == "__main__":
    unittest.main()

"""Named, non-restartable runsc command helpers sharing an owned workspace."""

import base64
import io
import json
import re
import tarfile
import time
from pathlib import Path

from docker.errors import APIError, NotFound

from . import execution_supervisor


def names(operation_id):
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise ValueError("invalid operation ID")
    return "vbe-op-" + operation_id, "vbe-op-" + operation_id + "-control"


def labels(view, config, image_id):
    result = {
        "vibeeval.role": "agent-execution",
        "vibeeval.session": view.session_id,
        "vibeeval.original": view.sandbox_id,
        "vibeeval.operation": config["operation_id"],
        "vibeeval.request": config["request_sha256"],
        "vibeeval.image": image_id,
    }
    if config.get("kind") == "file_edit":
        result["vibeeval.kind"] = "file_edit"
    return result


def capabilities(config):
    return (
        ["CHOWN"] if config.get("kind") == "file_edit" else ["SETUID", "SETGID", "KILL"]
    )


def verify(adapter, view, config, obj):
    name, volume = names(config["operation_id"])
    image_id = adapter.client.images.get(view.sandbox_image_ref).id
    attrs = obj.attrs
    host = attrs.get("HostConfig", {})
    actual = attrs.get("Config", {})
    mounts = {item.get("Destination"): item for item in attrs.get("Mounts", [])}
    if (
        obj.name != name
        or actual.get("Labels") != labels(view, config, image_id)
        or attrs.get("Image") != image_id
        or host.get("Runtime") != "runsc"
        or host.get("NetworkMode") != "none"
        or not host.get("ReadonlyRootfs")
        or host.get("RestartPolicy", {}).get("Name", "no") not in ("", "no")
        or {name.removeprefix("CAP_") for name in host.get("CapAdd", [])}
        != set(capabilities(config))
        or set(host.get("CapDrop", [])) != {"ALL"}
        or not any(
            value in ("no-new-privileges", "no-new-privileges:true")
            for value in host.get("SecurityOpt", [])
        )
        or actual.get("User") != "0:0"
        or mounts.get("/workspace", {}).get("Name")
        != view.sandbox_resource_key + "-data"
        or mounts.get("/control", {}).get("Name") != volume
    ):
        raise ValueError("execution helper identity mismatch")
    return obj


def prepare_execution(adapter, view, config):
    if view.status != "in_progress":
        raise ValueError("active sandbox required")
    original = adapter._container(view)
    if original.status != "running":
        raise ValueError("sandbox is not running")
    workspace = adapter.client.volumes.get(view.sandbox_resource_key + "-data")
    if workspace.attrs.get("Labels") != {
        "vibeeval.session": view.session_id,
        "vibeeval.image": original.attrs["Image"],
    }:
        raise ValueError("workspace volume ownership mismatch")
    name, control_name = names(config["operation_id"])
    expected = labels(view, config, original.attrs["Image"])
    try:
        return {
            "handle": verify(
                adapter, view, config, adapter.client.containers.get(name)
            ).id
        }
    except NotFound:
        pass
    control = adapter.client.volumes.create(name=control_name, labels=expected)
    if control.attrs.get("Labels") != expected:
        raise ValueError("control volume ownership mismatch")
    source = Path(execution_supervisor.__file__).read_text(encoding="utf-8")
    encoded = base64.b64encode(
        json.dumps(config, sort_keys=True).encode("utf-8")
    ).decode("ascii")
    # Linux limits each argv element to 128 KiB. A patch can modify a file of
    # up to 1 MiB even though the model's edit arguments are much smaller.
    chunks = [encoded[index : index + 32768] for index in range(0, len(encoded), 32768)]
    try:
        obj = adapter.client.containers.create(
            original.attrs["Image"],
            ["python", "-I", "-c", source, *chunks],
            entrypoint=[],
            name=name,
            runtime="runsc",
            user="0:0",
            read_only=True,
            network_mode="none",
            cap_drop=["ALL"],
            cap_add=capabilities(config),
            security_opt=["no-new-privileges"],
            mem_limit="512m",
            memswap_limit="512m",
            nano_cpus=500_000_000,
            pids_limit=64,
            tmpfs={"/tmp": "rw,noexec,nosuid,size=64m"},
            log_config={"Type": "none"},
            restart_policy={"Name": "no"},
            labels=expected,
            volumes={
                workspace.name: {"bind": "/workspace", "mode": "rw"},
                control.name: {"bind": "/control", "mode": "rw"},
            },
        )
    except APIError as error:
        if error.status_code != 409:
            raise
        obj = adapter.client.containers.get(name)
    return {"handle": verify(adapter, view, config, obj).id}


def _find(adapter, view, config, handle=None):
    obj = verify(
        adapter,
        view,
        config,
        adapter.client.containers.get(handle or names(config["operation_id"])[0]),
    )
    if handle and obj.id != handle:
        raise ValueError("execution handle mismatch")
    obj.reload()
    return obj


def start_execution(adapter, view, config, handle):
    if view.status != "in_progress":
        raise ValueError("active sandbox required")
    original = adapter._container(view)
    if original.status != "running":
        raise ValueError("original sandbox has been frozen")
    obj = _find(adapter, view, config, handle)
    if obj.status != "created":
        raise ValueError("execution is not fresh; never restart it")
    if config.get("kind") == "file_edit":
        # Caller holds the exam lifecycle lock and the durable workspace holder.
        # Persist private pause intent only after observing a running original.
        put_control(
            obj,
            "pause.json",
            json.dumps(
                {
                    "operation_id": config["operation_id"],
                    "request_sha256": config["request_sha256"],
                }
            ).encode("ascii"),
        )
        original.pause()
        original.reload()
        if original.status != "paused":
            raise ValueError("workspace did not freeze")
    obj.start()  # Exactly one ledger dispatch calls this; recovery only inspects.


def put_control(obj, name, data):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as output:
        entry = tarfile.TarInfo(name)
        entry.size, entry.uid, entry.gid, entry.mode = len(data), 0, 0, 0o600
        output.addfile(entry, io.BytesIO(data))
    obj.put_archive("/control", archive.getvalue())


def resume_workspace(adapter, view):
    """Only called under the exam lock after a trusted pause/stop receipt."""
    if (
        view.status != "in_progress"
        or view.deadline_at is None
        or time.time() >= view.deadline_at
    ):
        raise ValueError("frozen or expired workspace cannot resume")
    original = adapter._container(view)
    if original.status == "paused":
        original.unpause()
    elif original.status != "running":
        raise ValueError("workspace is not resumable")


def _control_json(obj, name, max_size):
    try:
        stream, _ = obj.get_archive("/control/" + name)
    except NotFound:
        return None
    data = bytearray()
    try:
        for chunk in stream:
            if len(data) + len(chunk) > 256000:
                raise ValueError("execution archive exceeds capture limit")
            data.extend(chunk)
    finally:
        close = getattr(stream, "close", None)
        if close:
            close()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        entries = archive.getmembers()
        if len(entries) != 1:
            raise ValueError("invalid progress archive")
        item = entries[0]
        if (
            item.name != name
            or not item.isfile()
            or item.uid != 0
            or item.size > max_size
        ):
            raise ValueError("invalid progress entry")
        with archive.extractfile(item) as content:
            receipt = json.load(content)
    if not isinstance(receipt, dict):
        raise ValueError("invalid progress object")
    return receipt


def _pause_owned(obj, config):
    if config.get("kind") != "file_edit":
        return False
    try:
        marker = _control_json(obj, "pause.json", 512)
    except (ValueError, KeyError, TypeError, tarfile.TarError):
        return False
    return marker == {
        "operation_id": config["operation_id"],
        "request_sha256": config["request_sha256"],
    }


def _read_progress(obj, config):
    receipt = _control_json(obj, "progress.json", 160000)
    if receipt is None:
        return None
    if (
        receipt.get("operation_id") != config["operation_id"]
        or receipt.get("request_sha256") != config["request_sha256"]
    ):
        raise ValueError("receipt identity mismatch")
    for name in ("stdout", "stderr"):
        if (
            len(base64.b64decode(receipt.get(name + "_b64", ""), validate=True))
            > config["max_output_bytes"]
        ):
            raise ValueError("receipt output exceeds limit")
    return receipt


def inspect_execution(adapter, view, config, handle=None):
    try:
        obj = _find(adapter, view, config, handle)
    except NotFound:
        return {
            "status": "unknown",
            "processes_stopped": bool(handle),
            "reason_code": "EXECUTION_NOT_FOUND",
        }
    if obj.status == "created":
        return {
            "status": "unknown",
            "processes_stopped": False,
            "reason_code": "START_NOT_CONFIRMED",
            "pause_owned": _pause_owned(obj, config),
        }
    stopped = obj.status in ("exited", "dead")
    pause_owned = _pause_owned(obj, config)
    try:
        receipt = _read_progress(obj, config)
    except (ValueError, KeyError, TypeError, tarfile.TarError):
        return {
            "status": "unknown",
            "processes_stopped": stopped,
            "reason_code": "RECEIPT_INVALID",
            "pause_owned": pause_owned,
        }
    if receipt is None:
        return {
            "status": "unknown" if stopped else "running",
            "processes_stopped": stopped,
            "reason_code": "RECEIPT_UNAVAILABLE" if stopped else None,
            "pause_owned": pause_owned,
        }
    if not stopped:
        receipt = {
            **receipt,
            "status": "running",
            "finished_at": None,
            "output_complete": False,
        }
    return {**receipt, "processes_stopped": stopped, "pause_owned": pause_owned}


def cancel_execution(adapter, view, config, handle=None):
    try:
        obj = _find(adapter, view, config, handle)
    except NotFound:
        return {
            "status": "unknown",
            "processes_stopped": bool(handle),
            "reason_code": "EXECUTION_NOT_FOUND",
        }
    if obj.status == "created":
        pause_owned = _pause_owned(obj, config)
        obj.remove(force=True, v=False)  # Disarm any late start for this exact ID.
        remove_control(adapter, view, config)
        return {
            "status": "cancelled",
            "processes_stopped": True,
            "reason_code": "CANCELLED_BEFORE_START",
            "pause_owned": pause_owned,
        }
    if obj.status not in ("exited", "dead"):
        put_control(obj, "cancel", b"")
        obj.stop(timeout=3)
    return inspect_execution(adapter, view, config, obj.id)


def remove_control(adapter, view, config):
    try:
        volume = adapter.client.volumes.get(names(config["operation_id"])[1])
    except NotFound:
        return
    if volume.attrs.get("Labels") != labels(
        view, config, adapter.client.images.get(view.sandbox_image_ref).id
    ):
        raise ValueError("control volume ownership mismatch")
    volume.remove()


def stop_bound_executions(adapter, view, *, remove=False):
    filters = {
        "label": [
            "vibeeval.role=agent-execution",
            "vibeeval.session=" + view.session_id,
            "vibeeval.original=" + view.sandbox_id,
        ]
    }
    for obj in adapter.client.containers.list(all=True, filters=filters):
        data = obj.attrs.get("Config", {}).get("Labels", {})
        config = {
            "operation_id": data.get("vibeeval.operation", ""),
            "request_sha256": data.get("vibeeval.request", ""),
            "kind": data.get("vibeeval.kind", "command"),
        }
        verify(adapter, view, config, obj)
        if obj.status not in ("exited", "dead", "created"):
            obj.stop(timeout=3)
            obj.reload()
        if obj.status not in ("exited", "dead", "created"):
            raise ValueError("execution helper still running")
        if remove or obj.status == "created":
            obj.remove(force=True, v=False)
            remove_control(adapter, view, config)

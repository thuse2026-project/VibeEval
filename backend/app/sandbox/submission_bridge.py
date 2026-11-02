"""SubmissionCoordinator boundary; caller holds D's session lock.

All input/save handlers must use lifecycle.write_guard before this is mounted.
No database calls occur here. Engine client must have a bounded request timeout.
"""

import io
import tarfile

from docker.errors import NotFound

from app.submissions.coordinator import FrozenWorkspace
from app.submissions.storage import SnapshotFile


class DockerSubmissionBridge:
    def __init__(self, client, backend_ref):
        self.client = client
        self.backend_ref = backend_ref

    def _container(self, view):
        if view.sandbox_backend_ref != self.backend_ref or not view.sandbox_id:
            raise ValueError("sandbox binding mismatch")
        obj = self.client.containers.get(view.sandbox_id)
        labels = obj.attrs.get("Config", {}).get("Labels") or {}
        if (
            obj.id != view.sandbox_id
            or obj.name != view.sandbox_resource_key
            or labels.get("vibeeval.session") != view.session_id
            or obj.attrs.get("Image")
            != self.client.images.get(view.sandbox_image_ref).id
        ):
            raise ValueError("sandbox identity mismatch")
        return obj

    def freeze_and_export(self, view):
        if view.status not in {"submitting", "submission_failed"}:
            raise ValueError("submission fence required")
        obj = self._container(view)
        if obj.status == "running":
            obj.pause()
            obj.reload()
        if obj.status != "paused":
            raise ValueError("sandbox is not paused")
        stream, _ = obj.get_archive("/workspace")
        data = bytearray()
        try:
            for chunk in stream:
                if len(data) + len(chunk) > 16 * 1024 * 1024:
                    raise ValueError("workspace archive too large")
                data.extend(chunk)
        finally:
            close = getattr(stream, "close", None)
            if close:
                close()
        return FrozenWorkspace(view.session_id, view.sandbox_id, decode_workspace(data))

    def delete_sandbox(self, view):
        if view.status != "submitted":
            raise ValueError("committed submission required")
        try:
            obj = self._container(view)
        except NotFound:
            # Only a missing container is idempotent success, not a missing image.
            try:
                self.client.containers.get(view.sandbox_id)
            except NotFound:
                return
            raise
        obj.remove(force=True, v=False)


def decode_workspace(data):
    files, seen, total = [], set(), 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        for index, entry in enumerate(archive):
            if index >= 400:
                raise ValueError("too many archive entries")
            parts = entry.name.split("/")
            if parts[0] != "workspace" or any(p in {".", ".."} for p in parts):
                raise ValueError("invalid workspace path")
            if entry.isdir():
                continue
            path = "/".join(parts[1:])
            if (
                not entry.isfile()
                or not path
                or "\\" in path
                or any(not p for p in parts)
                or path in seen
            ):
                raise ValueError("unsupported workspace entry")
            if entry.size > 1024 * 1024 or len(files) >= 200:
                raise ValueError("workspace limit exceeded")
            total += entry.size
            if total > 10 * 1024 * 1024:
                raise ValueError("workspace limit exceeded")
            seen.add(path)
            with archive.extractfile(entry) as source:
                content = source.read().decode("utf-8")
            files.append(SnapshotFile(path, content))
    return tuple(files)

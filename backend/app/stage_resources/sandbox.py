"""Bounded runsc operations for published manifests; no candidate code on host."""

import io
import json
import tarfile

from docker.errors import APIError, NotFound

from app.sandbox.bound_adapter import BoundSandboxAdapter
from app.sandbox.execution import stop_bound_executions
from app.sandbox.runtime import SandboxRuntime
from app.sandbox.safe_save import remove_writer
from app.submissions.storage import SnapshotFile
from app.workspace.archive import read_file_archive

from .types import validate_file

# Identical atomic-save protocol as v1, with safe walking of every parent.
WRITE_SCRIPT = r"""
import base64,hashlib,os,stat,sys,time
path,digest=sys.argv[1:3]
deadline=int(sys.argv[3])
parts=path.split('/')
assert all(p and p not in ('.','..') for p in parts) and '\\' not in path and ':' not in path
data=base64.b64decode(''.join(sys.argv[4:]),validate=True)
assert len(data)<=1048576
directory=os.open('/workspace',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
try:
 for name in parts[:-1]:
  info=os.fstat(directory)
  assert info.st_uid==0 and info.st_mode & 0o022 == 0
  child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
  os.close(directory); directory=child
 info=os.fstat(directory)
 assert info.st_uid==0 and info.st_mode & 0o022 == 0
 name=parts[-1]
 with os.fdopen(os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory),'rb') as source:
  info=os.fstat(source.fileno())
  assert stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_size<=1048576
  assert hashlib.sha256(source.read(1048577)).hexdigest()==digest
 stage=name+'.vibeeval-save'
 try: os.unlink(stage,dir_fd=directory)
 except FileNotFoundError: pass
 with os.fdopen(os.open(stage,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory),'wb') as target:
  target.write(data); target.flush(); os.fchmod(target.fileno(),0o644); os.fchown(target.fileno(),1000,1000); os.fsync(target.fileno())
 assert time.time()<deadline
 os.replace(stage,name,src_dir_fd=directory,dst_dir_fd=directory); os.fsync(directory)
finally: os.close(directory)
"""

HEALTH_SCRIPT = r"""
import json,os,stat,sys
from pathlib import Path
config=json.loads(sys.argv[1]); root=Path('/workspace')
assert os.getuid()==1000
actual=set()
for path in root.rglob('*'):
 assert not path.is_symlink()
 info=path.stat()
 if path.is_dir(): assert info.st_uid==0 and not info.st_mode & 0o022
 else:
  name=str(path.relative_to(root)); actual.add(name)
  assert stat.S_ISREG(info.st_mode) and info.st_nlink==1 and info.st_size<=1048576
  assert os.access(path,os.R_OK)
  if not config['readonly']: assert os.access(path,os.W_OK)==(name in config['writable'])
assert actual==set(config['files'])
assert root.stat().st_uid==0 and not root.stat().st_mode & 0o022
"""


class StageSandboxAdapter(BoundSandboxAdapter):
    write_script = WRITE_SCRIPT

    def validate_path(self, path, *, write=False):
        return validate_file(self.view, path, write=write)

    def ensure_bound(self, view):
        self.view = view
        image = self.client.images.get(view.sandbox_image_ref)
        labels = {"vibeeval.session": view.session_id, "vibeeval.image": image.id}
        volume = self.client.volumes.create(
            name=view.sandbox_resource_key + "-data", labels=labels
        )
        if volume.attrs.get("Labels") != labels:
            raise ValueError("workspace volume ownership mismatch")
        try:
            obj = self.client.containers.get(view.sandbox_resource_key)
        except NotFound:
            if view.sandbox_id:
                raise ValueError("bound workspace disappeared") from None
            try:
                obj = self.client.containers.create(
                    image.id,
                    ["sleep", "infinity"],
                    entrypoint=[],
                    name=view.sandbox_resource_key,
                    labels=labels,
                    runtime="runsc",
                    user="1000:1000",
                    read_only=True,
                    network_mode="none",
                    cap_drop=["ALL"],
                    security_opt=["no-new-privileges"],
                    mem_limit="512m",
                    memswap_limit="512m",
                    nano_cpus=500_000_000,
                    pids_limit=64,
                    tmpfs={"/tmp": "rw,noexec,nosuid,size=64m"},
                    environment={"PYTHONDONTWRITEBYTECODE": "1"},
                    volumes={
                        volume.name: {
                            "bind": "/workspace",
                            "mode": "ro" if view.read_only else "rw",
                        }
                    },
                    log_config={"Type": "none"},
                )
            except APIError as error:
                if error.status_code != 409:
                    raise
                obj = self.client.containers.get(view.sandbox_resource_key)
        if view.sandbox_id and obj.id != view.sandbox_id:
            raise ValueError("workspace binding changed")
        self._verify_container(view, obj, image.id)
        if obj.status == "created":
            obj.start()
            obj.reload()
        if obj.status != "running":
            raise ValueError("workspace requires explicit recovery")
        return obj.id

    def _verify_container(self, view, obj, image_id):
        config, host = obj.attrs.get("Config", {}), obj.attrs.get("HostConfig", {})
        mounts = [
            m
            for m in obj.attrs.get("Mounts", [])
            if m.get("Destination") == "/workspace"
        ]
        if (
            obj.name != view.sandbox_resource_key
            or obj.attrs.get("Image") != image_id
            or config.get("Labels")
            != {"vibeeval.session": view.session_id, "vibeeval.image": image_id}
            or host.get("Runtime") != "runsc"
            or host.get("NetworkMode") != "none"
            or not host.get("ReadonlyRootfs")
            or config.get("User") != "1000:1000"
            or set(host.get("CapDrop", [])) != {"ALL"}
            or host.get("CapAdd")
            or not any(
                v in ("no-new-privileges", "no-new-privileges:true")
                for v in host.get("SecurityOpt", [])
            )
            or len(mounts) != 1
            or mounts[0].get("Name") != view.sandbox_resource_key + "-data"
            or bool(mounts[0].get("RW")) == view.read_only
        ):
            raise ValueError("workspace identity or isolation mismatch")

    def _container(self, view):
        self.view = view
        obj = super()._container(view)
        self._verify_container(
            view, obj, self.client.images.get(view.sandbox_image_ref).id
        )
        return obj

    def check_ready(self, view):
        obj = self._container(view)
        if obj.status != "running":
            raise ValueError("workspace is not running")
        config = {
            "files": view.allowed_files,
            "writable": view.writable_files,
            "readonly": view.read_only,
        }
        result = obj.exec_run(
            ["python", "-I", "-c", HEALTH_SCRIPT, json.dumps(config)], user="1000:1000"
        )
        if result.exit_code:
            raise ValueError("published manifest health check failed")
        return True

    def read_file(self, view, path):
        self.view = view
        validate_file(view, path)
        obj = self._container(view)
        if obj.status not in ("running", "paused"):
            raise ValueError("workspace is not readable")
        stream, _ = obj.get_archive("/workspace/" + path)
        return read_file_archive(
            stream, path, validator=lambda p: validate_file(view, p)
        )

    def read_files(self, view):
        return {p: self.read_file(view, p) for p in view.allowed_files}

    def save_file(self, view, path, expected_digest, content):
        self.view = view
        return super().save_file(view, path, expected_digest, content)

    def close_workspace(self, view):
        """Pause first, disarm ALL helpers, then remove the old PID namespace."""
        try:
            obj = self._container(view)
        except NotFound:
            # A retry can observe a successful prior removal. Never touch a new
            # container merely because it has the same friendly name.
            try:
                self.client.containers.get(view.sandbox_resource_key)
            except NotFound:
                stop_bound_executions(self, view, remove=True)
                remove_writer(self.client, view)
                return
            raise ValueError("unexpected replacement workspace") from None
        if obj.status == "running":
            obj.pause()
            obj.reload()
        if obj.status not in ("paused", "exited", "dead", "created"):
            raise ValueError("workspace was not frozen")
        stop_bound_executions(self, view, remove=True)
        remove_writer(self.client, view)
        obj.remove(force=True, v=False)
        try:
            self.client.containers.get(view.sandbox_resource_key)
        except NotFound:
            return
        raise ValueError("workspace processes were not removed")

    def export_volume(self, view):
        image = self.client.images.get(view.sandbox_image_ref)
        volume = self.client.volumes.get(view.sandbox_resource_key + "-data")
        labels = {"vibeeval.session": view.session_id, "vibeeval.image": image.id}
        if volume.attrs.get("Labels") != labels:
            raise ValueError("workspace volume ownership mismatch")
        name = view.sandbox_resource_key + "-export"
        expected = {**labels, "vibeeval.role": "stage-export"}
        try:
            obj = self.client.containers.get(name)
        except NotFound:
            try:
                obj = self.client.containers.create(
                    image.id,
                    ["sleep", "infinity"],
                    entrypoint=[],
                    name=name,
                    labels=expected,
                    runtime="runsc",
                    user="1000:1000",
                    read_only=True,
                    network_mode="none",
                    cap_drop=["ALL"],
                    security_opt=["no-new-privileges"],
                    mem_limit="256m",
                    pids_limit=32,
                    volumes={volume.name: {"bind": "/workspace", "mode": "ro"}},
                    log_config={"Type": "none"},
                )
            except APIError as error:
                if error.status_code != 409:
                    raise
                obj = self.client.containers.get(name)
        mounts = [
            m
            for m in obj.attrs.get("Mounts", [])
            if m.get("Destination") == "/workspace"
        ]
        if (
            obj.attrs.get("Config", {}).get("Labels") != expected
            or obj.attrs.get("Image") != image.id
            or obj.attrs.get("HostConfig", {}).get("Runtime") != "runsc"
            or obj.attrs.get("HostConfig", {}).get("NetworkMode") != "none"
            or not obj.attrs.get("HostConfig", {}).get("ReadonlyRootfs")
            or obj.attrs.get("Config", {}).get("User") != "1000:1000"
            or set(obj.attrs.get("HostConfig", {}).get("CapDrop", [])) != {"ALL"}
            or obj.attrs.get("HostConfig", {}).get("CapAdd")
            or len(mounts) != 1
            or mounts[0].get("Name") != volume.name
            or mounts[0].get("RW")
        ):
            raise ValueError("exporter ownership mismatch")
        if obj.status == "created":
            obj.start()
            obj.reload()
        if obj.status == "running":
            obj.pause()
            obj.reload()
        if obj.status != "paused":
            raise ValueError("exporter is not frozen")
        stream, _ = obj.get_archive("/workspace")
        data = bytearray()
        try:
            for chunk in stream:
                if len(data) + len(chunk) > 16 * 1024 * 1024:
                    raise ValueError("archive exceeds limit")
                data.extend(chunk)
        finally:
            close = getattr(stream, "close", None)
            if close:
                close()
        files, seen = [], set()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for index, entry in enumerate(archive):
                if index >= 1000:
                    raise ValueError("too many archive entries")
                if entry.isdir():
                    continue
                if not entry.name.startswith("workspace/"):
                    raise ValueError("invalid archive root")
                path = entry.name[len("workspace/") :]
                if path.endswith(".vibeeval-save"):
                    continue
                if path not in view.allowed_files:
                    continue  # Build outputs are not published answer files.
                if path in seen or not entry.isfile() or not 0 <= entry.size <= 1048576:
                    raise ValueError("unsafe answer file")
                seen.add(path)
                with archive.extractfile(entry) as source:
                    files.append(SnapshotFile(path, source.read().decode("utf-8")))
        if seen != set(view.allowed_files):
            raise ValueError("published answer file missing")
        obj.remove(force=True, v=False)
        return tuple(files)


def _operate(pipe, settings, method, view, args):
    import docker

    client = None
    try:
        client = docker.DockerClient(base_url=settings[0], timeout=settings[2])
        pipe.send(
            (
                True,
                getattr(StageSandboxAdapter(client, settings[1]), method)(view, *args),
            )
        )
    except Exception:
        pipe.send((False, "STAGE_SANDBOX_OPERATION_FAILED"))
    finally:
        if client:
            client.close()
        pipe.close()


class StageSandboxRuntime(SandboxRuntime):
    def _call(self, method, view, *args):
        # Reuse the bounded process runner, selecting only a trusted adapter.
        return self._spawn_call(_operate, method, view, args)

    def read_file(self, view, path):
        validate_file(view, path)
        return self._call("read_file", view, path)

    def save_file(self, view, path, expected_digest, content):
        validate_file(view, path, write=True)
        return self._call("save_file", view, path, expected_digest, content)

    def close_workspace(self, view):
        return self._call("close_workspace", view)

    def export_volume(self, view):
        return self._call("export_volume", view)

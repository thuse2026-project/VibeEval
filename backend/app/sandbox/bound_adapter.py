"""Adapter for D's persisted SessionView, called under lock_session."""

from docker.errors import APIError, NotFound
from docker.types import LogConfig

from .submission_bridge import DockerSubmissionBridge


class BoundSandboxAdapter(DockerSubmissionBridge):
    def save_file(self, view, path, expected_digest, content):
        from .safe_save import save_file

        return save_file(self, view, path, expected_digest, content)

    def read_files(self, view):
        from app.workspace.files import ALLOWED_FILES

        return {path: self.read_file(view, path) for path in sorted(ALLOWED_FILES)}

    def read_file(self, view, path):
        from app.workspace.archive import read_file_archive
        from app.workspace.files import validate_path

        validate_path(path)
        if view.status != "in_progress":
            raise ValueError("active exam required")
        obj = self._container(view)
        if obj.status not in {"running", "paused"}:
            raise ValueError("sandbox is not running")
        stream, _ = obj.get_archive("/workspace/" + path)
        return read_file_archive(stream, path)

    def check_ready(self, view):
        obj = self._container(view)
        if obj.status != "running":
            raise ValueError("sandbox is not running")
        # Inspect metadata without importing or executing candidate source.
        script = """import os,sys,stat
from pathlib import Path
assert sys.version_info[:2] == (3,12) and os.getuid() == 1000
root=Path('/workspace')
editable={'src/booking.py','tests/test_candidate.py'}
expected=editable|{'README.md','requirements.txt','src/main.py','tests/test_public.py'}
for name in expected:
 p=root/name
 assert not p.is_symlink() and stat.S_ISREG(p.stat().st_mode)
 assert os.access(p,os.R_OK)
 assert os.access(p,os.W_OK) == (name in editable)
for p in [root,root/'src',root/'tests']:
 assert not p.is_symlink() and not os.access(p,os.W_OK)
assert not (root/'maintainer').exists()
"""
        result = obj.exec_run(["python", "-I", "-c", script], user="1000:1000")
        if result.exit_code != 0:
            raise ValueError("template health check failed")
        return True

    def ensure_bound(self, view):
        if view.status != "provisioning":
            raise ValueError("preparation requires provisioning state")
        if (
            view.sandbox_backend_ref != self.backend_ref
            or not view.sandbox_resource_key
            or not view.sandbox_image_ref
        ):
            raise ValueError("persisted sandbox plan required")
        # Resolve locally before allocating. No automatic image pull.
        image = self.client.images.get(view.sandbox_image_ref)
        if view.sandbox_id:
            obj = self._container(view)  # Missing binding never creates a new answer.
        else:
            try:
                obj = self.client.containers.get(view.sandbox_resource_key)
            except NotFound:
                labels = {
                    "vibeeval.session": view.session_id,
                    "vibeeval.image": image.id,
                }
                volume = self.client.volumes.create(
                    name=view.sandbox_resource_key + "-data", labels=labels
                )
                if volume.attrs.get("Labels") != labels:
                    raise ValueError("workspace ownership mismatch")
                try:
                    obj = self.client.containers.create(
                        image.id,
                        ["sleep", "infinity"],
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
                        volumes={volume.name: {"bind": "/workspace", "mode": "rw"}},
                        log_config=LogConfig(
                            type="local", config={"max-size": "5m", "max-file": "2"}
                        ),
                    )
                except APIError as error:
                    if error.status_code != 409:
                        raise
                    obj = self.client.containers.get(view.sandbox_resource_key)
            if (
                obj.name != view.sandbox_resource_key
                or (obj.attrs.get("Config", {}).get("Labels") or {}).get(
                    "vibeeval.session"
                )
                != view.session_id
                or obj.attrs.get("Image") != image.id
            ):
                raise ValueError("existing resource does not match plan")
        if obj.status == "created":
            obj.start()
            obj.reload()
        if obj.status != "running":
            raise ValueError("sandbox requires explicit recovery")
        return obj.id


def prepare_bound(lifecycle, adapter, session_id, image_ref, task_version_id):
    """Prepare the binding only; readiness needs a separate template health check."""
    lifecycle.prepare_sandbox(
        session_id,
        image_ref,
        backend_ref=adapter.backend_ref,
        task_version_id=task_version_id,
    )
    with lifecycle.lock_session(session_id) as locked:
        sandbox_id = adapter.ensure_bound(locked.view)
        locked.bind_sandbox(sandbox_id, image_ref=locked.view.sandbox_image_ref)
    return lifecycle.get_session(session_id)

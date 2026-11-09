"""Explicit Docker/model transport doubles for stage integration tests."""

import base64
import hashlib
import json
import queue
import time

from app.stage_resources import ResourceSpec
from app.submissions.storage import SnapshotFile

IMAGE = "sha256:" + "a" * 64
FILES = {
    "README.md": "public task\n",
    "solution.py": "baseline\n",
    "tests/case.py": "public test\n",
}


def spec():
    return ResourceSpec(
        task_version="provided-v1",
        image_ref=IMAGE,
        files=tuple(FILES),
        writable=("solution.py", "tests/case.py"),
        agent_policy={"mode": "sandbox_auto", "action_decision": "allow"},
    )


def completion(text="done", calls=()):
    message = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = [
            {
                "id": "call" + str(i),
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(value)},
            }
            for i, (name, value) in enumerate(calls)
        ]
    return {
        "model": "fixture/model",
        "choices": [
            {"message": message, "finish_reason": "tool_calls" if calls else "stop"}
        ],
        "usage": {"total_tokens": 10},
    }


class Model:
    def __init__(self):
        self.responses, self.requests = [], []

    async def complete(self, payload):
        self.requests.append(payload)
        return self.responses.pop(0) if self.responses else completion()


class Sandbox:
    supports_file_edits = True
    settings = ("fixture", "stage-fixture", 1)

    def __init__(self):
        self.volumes, self.containers, self.jobs, self.calls = {}, {}, {}, []
        self.serial, self.instant, self.fail_close, self.fail_export = (
            0,
            True,
            False,
            False,
        )

    def ensure_bound(self, view):
        self.calls.append(("prepare", view))
        self.volumes.setdefault(view.session_id, dict(FILES))
        if view.session_id not in self.containers:
            self.serial += 1
            self.containers[view.session_id] = "container-" + str(self.serial)
        return self.containers[view.session_id]

    def check_ready(self, view):
        assert self.containers[view.session_id] == view.sandbox_id
        return True

    def read_files(self, view):
        return dict(self.volumes[view.session_id])

    def read_file(self, view, path):
        return self.volumes[view.session_id][path]

    def save_file(self, view, path, expected_digest, content):
        current = self.read_file(view, path)
        if hashlib.sha256(current.encode()).hexdigest() != expected_digest:
            return {"saved": False, "content": current}
        self.volumes[view.session_id][path] = content
        return {"saved": True}

    def close_workspace(self, view):
        self.calls.append(("close", view))
        if self.fail_close:
            raise RuntimeError("fixture Docker unavailable")
        self.containers.pop(view.session_id, None)
        for job in self.jobs.values():
            if job["workspace"] == view.session_id:
                job["status"] = "cancelled"

    def export_volume(self, view):
        self.calls.append(("export", view))
        if self.fail_export:
            raise RuntimeError("fixture export unavailable")
        assert view.session_id not in self.containers
        return tuple(
            SnapshotFile(p, text) for p, text in self.volumes[view.session_id].items()
        )

    def prepare_execution(self, view, config):
        self.jobs.setdefault(
            config["operation_id"],
            {
                "workspace": view.session_id,
                "status": "created",
                "config": config,
                "view": view,
                "time": time.time(),
            },
        )
        return {"handle": config["operation_id"]}

    def start_execution(self, view, config, handle):
        job = self.jobs[handle]
        assert job["status"] == "created" and view.session_id in self.containers
        job["status"] = "succeeded" if self.instant else "running"
        if config.get("kind") == "file_edit":
            edit = config["edit"]
            assert edit["path"] in view.writable_files
            self.volumes[view.session_id][edit["path"]] = base64.b64decode(
                edit["content_b64"]
            ).decode()

    def inspect_execution(self, view, config, handle=None):
        job = self.jobs[handle or config["operation_id"]]
        stopped = job["status"] not in ("created", "running")
        result = {
            "version": 1,
            "operation_id": config["operation_id"],
            "request_sha256": config["request_sha256"],
            "status": job["status"],
            "processes_stopped": stopped,
            "started_at": job["time"],
            "command_started_at": job["time"],
            "finished_at": time.time() if stopped else None,
            "exit_code": 0 if stopped else None,
            "stdout_b64": "",
            "stderr_b64": "",
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "output_complete": stopped,
        }
        if config.get("kind") == "file_edit":
            result["pause_owned"] = True
            edit = config["edit"]
            result["file_change"] = {
                "path": edit["path"],
                "before_sha256": edit["before_sha256"],
                "after_sha256": edit["after_sha256"],
                "changed": edit["before_sha256"] != edit["after_sha256"],
            }
        return result

    def cancel_execution(self, view, config, handle=None):
        self.jobs[handle or config["operation_id"]]["status"] = "cancelled"
        return self.inspect_execution(view, config, handle)

    def resume_workspace(self, view):
        assert view.session_id in self.containers
        self.calls.append(("resume", view))


class Terminal:
    def __init__(self, view):
        self.view, self.writes, self.closed = view, [], False
        self.output = queue.Queue()

    def read(self):
        try:
            return self.output.get(timeout=0.1)
        except queue.Empty:
            return None

    def write(self, data):
        self.writes.append(data)
        self.output.put(data)

    def resize(self, cols, rows):
        pass

    def close(self):
        self.closed = True


class Terminals:
    def __init__(self):
        self.handles = []

    def open(self, view):
        handle = Terminal(view)
        self.handles.append(handle)
        return handle

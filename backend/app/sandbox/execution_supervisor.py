"""Fixed PID 1 code sent into an operation's runsc container, never run on host.

Only standard library imports: the task image does not contain the API package.
Root owns /control; the candidate child drops to UID/GID 1000 with no new privs.
"""

import base64
import hashlib
import json
import os
import selectors
import signal
import stat
import subprocess
import sys
import time


def atomic_json(directory, value):
    data = json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode(
        "ascii"
    )
    fd = os.open(
        "progress.tmp",
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory,
    )
    with os.fdopen(fd, "wb") as target:
        target.write(data)
        target.flush()
        os.fsync(target.fileno())
    os.replace(
        "progress.tmp", "progress.json", src_dir_fd=directory, dst_dir_fd=directory
    )
    os.fsync(directory)


def file_digest(path):
    root = os.open("/workspace", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    parent = os.dup(root)
    try:
        parts = path.split("/")
        for name in parts[:-1]:
            child = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
            )
            os.close(parent)
            parent = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1048576:
                raise ValueError("invalid workspace file")
            return hashlib.sha256(source.read(1048577)).hexdigest()
    finally:
        os.close(parent)
        os.close(root)


def child_limits():
    import resource

    resource.setrlimit(resource.RLIMIT_FSIZE, (1048576, 1048576))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))


def edit_file(config, cancelled):
    """Fixed platform write; never evaluate or import the proposed contents."""
    edit = config["edit"]
    path = edit["path"]
    if path not in config.get(
        "writable_paths", ("src/booking.py", "tests/test_candidate.py")
    ):
        raise ValueError("file is not editable")
    data = base64.b64decode(edit["content_b64"], validate=True)
    if len(data) > 1048576 or b"\x00" in data:
        raise ValueError("invalid replacement bytes")
    data.decode("utf-8")
    if hashlib.sha256(data).hexdigest() != edit["after_sha256"]:
        raise ValueError("replacement digest mismatch")
    root = os.open("/workspace", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    directory = None
    stage = None
    try:
        parts = path.split("/")
        if any(p in ("", ".", "..") for p in parts) or "\\" in path or ":" in path:
            raise ValueError("unsafe target path")
        directory = os.dup(root)
        for parent in parts[:-1]:
            info = os.fstat(directory)
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError("unsafe workspace directory")
            child = os.open(
                parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
            )
            os.close(directory)
            directory = child
        name = parts[-1]
        for fd in (root, directory):
            info = os.fstat(fd)
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError("unsafe workspace directory")
        old = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(old, "rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > 1048576
            ):
                raise ValueError("unsafe target file")
            before = source.read(1048577)
        if hashlib.sha256(before).hexdigest() != edit["before_sha256"]:
            return "conflict", "SOURCE_VERSION_CHANGED", None
        if data != before:
            stage = name + ".vibeeval-save"
            try:
                os.unlink(stage, dir_fd=directory)
            except FileNotFoundError:
                pass
            fd = os.open(
                stage,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            with os.fdopen(fd, "wb") as target:
                target.write(data)
                os.fchmod(target.fileno(), 0o644)
                os.fchown(target.fileno(), 1000, 1000)
                target.flush()
                os.fsync(target.fileno())
        if cancelled[0] or os.path.exists("/control/cancel"):
            return "cancelled", "CANCELLED_BEFORE_REPLACE", None
        if time.time() >= config["deadline_at"]:
            return "timed_out", "EXAM_DEADLINE", None
        if stage:
            os.replace(stage, name, src_dir_fd=directory, dst_dir_fd=directory)
            stage = None
            os.fsync(directory)
        return (
            "succeeded",
            None,
            {
                "path": path,
                "before_sha256": edit["before_sha256"],
                "after_sha256": edit["after_sha256"],
                "changed": data != before,
            },
        )
    finally:
        if stage is not None:
            try:
                os.unlink(stage, dir_fd=directory)
            except FileNotFoundError:
                pass
        if directory is not None:
            os.close(directory)
        os.close(root)


def edit_timeout(*_):
    raise TimeoutError("file edit exceeded its bounded time")


def supervise(config):
    if os.getuid() != 0:
        raise ValueError("platform supervisor must own its private control directory")
    directory = os.open("/control", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    if os.fstat(directory).st_uid != 0:
        raise ValueError("control volume owner mismatch")
    os.fchmod(directory, 0o700)
    started = time.time()
    buffers, totals = {"stdout": bytearray(), "stderr": bytearray()}, {
        "stdout": 0,
        "stderr": 0,
    }
    receipt = {
        "version": 1,
        "operation_id": config["operation_id"],
        "request_sha256": config["request_sha256"],
        "status": "running",
        "started_at": started,
        "command_started_at": None,
        "finished_at": None,
        "exit_code": None,
        "reason_code": None,
    }

    def publish(complete=False):
        for stream in buffers:
            receipt[stream + "_b64"] = base64.b64encode(buffers[stream]).decode("ascii")
            receipt[stream + "_bytes"] = totals[stream]
            receipt[stream + "_truncated"] = totals[stream] > len(buffers[stream])
        receipt["output_complete"] = complete
        atomic_json(directory, receipt)

    process = None
    cancelled = [False]
    signal.signal(signal.SIGTERM, lambda *_: cancelled.__setitem__(0, True))
    try:
        if started >= config["deadline_at"]:
            receipt.update(
                status="timed_out", finished_at=time.time(), reason_code="EXAM_DEADLINE"
            )
            publish(True)
            return
        if any(
            file_digest(path) != expected
            for path, expected in config["expected_digests"].items()
        ):
            receipt.update(
                status="conflict",
                finished_at=time.time(),
                reason_code="SOURCE_VERSION_CHANGED",
            )
            publish(True)
            return
        if config.get("kind") == "file_edit":
            signal.signal(signal.SIGALRM, edit_timeout)
            signal.setitimer(
                signal.ITIMER_REAL,
                max(
                    0.001,
                    min(config["timeout_seconds"], config["deadline_at"] - time.time()),
                ),
            )
            try:
                status, code, change = edit_file(config, cancelled)
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
            receipt.update(
                status=status,
                reason_code=code,
                file_change=change,
                command_started_at=started,
                finished_at=time.time(),
            )
            publish(True)
            return
        process = subprocess.Popen(
            config["argv"],
            cwd="/workspace" + ("/" + config["cwd"] if config["cwd"] != "." else ""),
            user=1000,
            group=1000,
            extra_groups=(),
            start_new_session=True,
            close_fds=True,
            preexec_fn=child_limits,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": "/tmp",
                "LANG": "C.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
            },
        )
        receipt["command_started_at"] = time.time()
        deadline = time.monotonic() + config["timeout_seconds"]
        stop_at, forced, last_publish, exited_at = None, None, 0.0, None
        with selectors.DefaultSelector() as selector:
            for stream in buffers:
                pipe = getattr(process, stream)
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, stream)
            while True:
                now = time.monotonic()
                if forced is None:
                    if cancelled[0] or os.path.exists("/control/cancel"):
                        forced = "cancelled"
                    elif now >= deadline or time.time() >= config["deadline_at"]:
                        forced = "timed_out"
                    if forced:
                        stop_at = now
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                elif stop_at is not None and now - stop_at >= 1:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                for key, _ in selector.select(0.05):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    stream = key.data
                    totals[stream] += len(chunk)
                    remaining = max(
                        0, config["max_output_bytes"] - len(buffers[stream])
                    )
                    buffers[stream].extend(chunk[:remaining])
                if now - last_publish >= 0.5:
                    publish()
                    last_publish = now
                code = process.poll()
                if code is not None:
                    exited_at = now if exited_at is None else exited_at
                    # A detached child must not keep this operation alive. PID 1
                    # exits after the receipt; Docker ends the entire cgroup.
                    drained = not selector.get_map()
                    if not drained and now - exited_at < 0.25:
                        continue
                    receipt.update(
                        status=forced or ("succeeded" if code == 0 else "failed"),
                        exit_code=code,
                        finished_at=time.time(),
                    )
                    publish(drained)
                    return
    except Exception:
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        receipt.update(
            status="unknown" if config.get("kind") == "file_edit" else "failed",
            reason_code=(
                "FILE_EDIT_OUTCOME_UNKNOWN"
                if config.get("kind") == "file_edit"
                else "SUPERVISOR_FAILED"
            ),
            finished_at=time.time(),
        )
        publish(False)
    finally:
        if process is not None:
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()
        os.close(directory)


def main():
    config = json.loads(base64.b64decode("".join(sys.argv[1:]), validate=True))
    supervise(config)


if __name__ == "__main__":
    main()

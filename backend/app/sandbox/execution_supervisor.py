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
    parent = root
    try:
        parts = path.split("/")
        if len(parts) == 2:
            parent = os.open(
                parts[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root
            )
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1048576:
                raise ValueError("invalid workspace file")
            return hashlib.sha256(source.read(1048577)).hexdigest()
    finally:
        if parent != root:
            os.close(parent)
        os.close(root)


def child_limits():
    import resource

    resource.setrlimit(resource.RLIMIT_FSIZE, (1048576, 1048576))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))


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
        cancelled = [False]
        signal.signal(signal.SIGTERM, lambda *_: cancelled.__setitem__(0, True))
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
            status="failed", reason_code="SUPERVISOR_FAILED", finished_at=time.time()
        )
        publish(False)
    finally:
        if process is not None:
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()
        os.close(directory)


def main():
    config = json.loads(base64.b64decode(sys.argv[1], validate=True))
    supervise(config)


if __name__ == "__main__":
    main()

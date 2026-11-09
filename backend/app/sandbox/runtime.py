"""Bounded submission adapter. Child processes own their Docker connections.

No database connection or lock is passed to a child. Timing out closes the
worker; an already accepted daemon operation may still finish, so retry by ID.
"""

import multiprocessing
import os


def _operate(pipe, settings, method, view, args=()):
    import docker

    from .bound_adapter import BoundSandboxAdapter

    client = None
    try:
        client = docker.DockerClient(base_url=settings[0], timeout=settings[2])
        adapter = BoundSandboxAdapter(client, settings[1])
        pipe.send((True, getattr(adapter, method)(view, *args)))
    except Exception as error:
        from requests.exceptions import Timeout

        code = (
            "SANDBOX_REQUEST_TIMEOUT"
            if isinstance(error, Timeout)
            else "SANDBOX_OPERATION_FAILED"
        )
        pipe.send((False, code))
    finally:
        if client is not None:
            client.close()
        pipe.close()


class SandboxOperationFailed(RuntimeError):
    pass


class SandboxRuntime:
    supports_file_edits = True

    def resume_workspace(self, view):
        return self._call("resume_workspace", view)

    def __init__(
        self,
        *,
        backend_ref,
        docker_url="unix:///var/run/docker.sock",
        request_timeout=20,
        operation_timeout=45,
    ):
        if not backend_ref or not 0 < request_timeout <= operation_timeout <= 120:
            raise ValueError("invalid sandbox runtime configuration")
        if not docker_url.startswith("unix://"):
            raise ValueError("only local Unix socket connections are supported")
        self.settings = (docker_url, backend_ref, request_timeout)
        self.operation_timeout = operation_timeout

    def _call(self, method, view, *args):
        ctx = multiprocessing.get_context("spawn")
        reader, writer = ctx.Pipe(duplex=False)
        worker = ctx.Process(
            target=_operate, args=(writer, self.settings, method, view, args)
        )
        worker.start()
        writer.close()
        try:
            if not reader.poll(self.operation_timeout):
                raise TimeoutError(
                    "sandbox operation timed out; retry original binding"
                )
            try:
                success, result = reader.recv()
            except EOFError:
                raise SandboxOperationFailed("sandbox worker exited") from None
            if not success:
                raise SandboxOperationFailed(result)
            return result
        finally:
            reader.close()
            worker.join(timeout=0.2)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=1)
            if worker.is_alive():
                worker.kill()
                worker.join()

    def freeze_and_export(self, view):
        return self._call("freeze_and_export", view)

    def ensure_bound(self, view):
        return self._call("ensure_bound", view)

    def check_ready(self, view):
        return self._call("check_ready", view)

    def read_file(self, view, path):
        from app.workspace.files import validate_path

        validate_path(path)
        return self._call("read_file", view, path)

    def read_files(self, view):
        return self._call("read_files", view)

    def save_file(self, view, path, expected_digest, content):
        from app.workspace.files import encode_content, validate_path

        validate_path(path, write=True)
        encode_content(content)
        return self._call("save_file", view, path, expected_digest, content)

    def delete_sandbox(self, view):
        return self._call("delete_sandbox", view)

    def prepare_execution(self, view, config):
        return self._call("prepare_execution", view, config)

    def start_execution(self, view, config, handle):
        return self._call("start_execution", view, config, handle)

    def inspect_execution(self, view, config, handle=None):
        return self._call("inspect_execution", view, config, handle)

    def cancel_execution(self, view, config, handle=None):
        return self._call("cancel_execution", view, config, handle)


def create_submission_sandbox():
    return SandboxRuntime(
        backend_ref=os.environ["SANDBOX_BACKEND_REF"],
        docker_url=os.environ.get("SANDBOX_DOCKER_URL", "unix:///var/run/docker.sock"),
        request_timeout=float(os.environ.get("SANDBOX_REQUEST_TIMEOUT", "20")),
        operation_timeout=float(os.environ.get("SANDBOX_OPERATION_TIMEOUT", "45")),
    )

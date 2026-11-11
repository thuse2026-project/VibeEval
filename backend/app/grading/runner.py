"""Run trusted public tests on the host against an isolated RPC implementation.

Only the trusted template/test controller runs on the host. Candidate Python is
imported inside a fresh, offline runsc container; stdout never supplies scores.
"""

import contextlib
import dataclasses
import io
import json
import logging
import multiprocessing
import struct
import sys
import tarfile
import time
import types
import unittest
import uuid
from datetime import datetime
from pathlib import Path

WORKER = r"""
import sys,json,datetime,contextlib
from pathlib import Path
initial=json.loads(sys.stdin.readline())
assert initial['op']=='load'
Path('/workspace/src').mkdir(parents=True,exist_ok=True)
Path('/workspace/src/booking.py').write_text(initial['args'][0],encoding='utf-8')
sys.path.insert(0, '/workspace/src')
objects={}
try:
    with contextlib.redirect_stdout(sys.stderr):
        from booking import BookingService
    initial_error=None
except BaseException as error:
    initial_error=type(error).__name__+': '+str(error)[:500]
print(json.dumps({'id':initial['id'],'value':None}),flush=True)
def decode(value):
    if isinstance(value,dict) and set(value)=={'datetime'}:
        return datetime.datetime.fromisoformat(value['datetime'])
    return value
for line in sys.stdin:
    try:
        request=json.loads(line)
        with contextlib.redirect_stdout(sys.stderr):
            if initial_error: raise RuntimeError(initial_error)
            if request['op']=='create':
                objects[request['service']]=BookingService()
                value=None
            else:
                obj=objects[request['service']]
                result=getattr(obj,request['op'])(*[decode(v) for v in request['args']])
                value={k:getattr(result,k) for k in ('booking_id','room_id','start','end','cancelled')}
                value['start']=value['start'].isoformat()
                value['end']=value['end'].isoformat()
        reply={'id':request['id'],'value':value}
    except BaseException as error:
        reply={'id':request['id'],'error':type(error).__name__,'message':str(error)[:500]}
    print(json.dumps(reply),flush=True)
"""


class CandidateFailure(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class Booking:
    booking_id: str
    room_id: str
    start: datetime
    end: datetime
    cancelled: bool


class RPC:
    def __init__(self, stream):
        self.socket = getattr(stream, "_sock", stream)
        self.socket.settimeout(5)
        self.buffer = b""
        self.failed = False
        self.deadline = time.monotonic() + 30

    def exact(self, count):
        result = b""
        while len(result) < count:
            chunk = self.socket.recv(count - len(result))
            if not chunk:
                raise CandidateFailure("考生程序提前退出或导入失败")
            result += chunk
        return result

    def call(self, service, operation, *args):
        if self.failed or time.monotonic() > self.deadline:
            raise CandidateFailure("考生程序超时或通信已中断")
        token = uuid.uuid4().hex
        values = [
            {"datetime": v.isoformat()} if isinstance(v, datetime) else v for v in args
        ]
        try:
            self.socket.sendall(
                (
                    json.dumps(
                        {
                            "id": token,
                            "service": service,
                            "op": operation,
                            "args": values,
                        }
                    )
                    + "\n"
                ).encode()
            )
            while b"\n" not in self.buffer:
                header = self.exact(8)
                size = struct.unpack(">I", header[4:])[0]
                if header[0] != 1 or size > 65536 or len(self.buffer) + size > 65536:
                    raise CandidateFailure("考生程序返回了无效或过大的响应")
                self.buffer += self.exact(size)
            line, self.buffer = self.buffer.split(b"\n", 1)
            reply = json.loads(line)
            if reply.get("id") != token:
                raise CandidateFailure("考生程序返回的操作编号不匹配")
        except (
            OSError,
            ValueError,
            TypeError,
            AttributeError,
            CandidateFailure,
        ) as error:
            self.failed = True
            raise CandidateFailure("考生程序未能返回有效操作结果") from error
        if "error" in reply:
            if reply["error"] == "ValueError":
                raise ValueError(str(reply.get("message", ""))[:500])
            raise CandidateFailure(str(reply.get("message", "执行失败"))[:500])
        if operation in ("create", "load"):
            return None
        value = reply["value"]
        try:
            if not (
                isinstance(value["booking_id"], str)
                and bool(value["booking_id"])
                and isinstance(value["room_id"], str)
                and type(value["cancelled"]) is bool
            ):
                raise ValueError("invalid booking fields")
            return Booking(
                value["booking_id"],
                value["room_id"],
                datetime.fromisoformat(value["start"]),
                datetime.fromisoformat(value["end"]),
                value["cancelled"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise CandidateFailure("预约返回值不符合接口约定") from error


def trusted_tests(rpc, baseline):
    class RemoteService:
        def __init__(self):
            self.key = uuid.uuid4().hex
            rpc.call(self.key, "create")

        def reserve(self, *args):
            return rpc.call(self.key, "reserve", *args)

        def cancel(self, *args):
            return rpc.call(self.key, "cancel", *args)

    previous = sys.modules.get("booking")
    sys.modules["booking"] = types.SimpleNamespace(BookingService=RemoteService)
    module = types.ModuleType("trusted_public_tests")
    module.__file__ = str(
        Path(__file__).resolve().parents[3]
        / "problems/python-debug-01/tests/test_public.py"
    )
    try:
        exec(
            compile(baseline["tests/test_public.py"], module.__file__, "exec"),
            module.__dict__,
        )
    finally:
        if previous is None:
            sys.modules.pop("booking", None)
        else:
            sys.modules["booking"] = previous
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    if suite.countTestCases() != 8:
        raise RuntimeError("unexpected trusted test suite")
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    failures = {
        getattr(test, "test_case", test)._testMethodName
        for test, _ in result.failures + result.errors
    }
    names = sorted(
        name for name in dir(module.BookingServiceTests) if name.startswith("test_")
    )
    passed = result.testsRun - len(failures)
    return {
        "status": "completed",
        "score": passed * 7.5,
        "passed": passed,
        "total": 8,
        "tests": [
            {
                "name": name,
                "passed": name not in failures,
                "points": 7.5 if name not in failures else 0,
            }
            for name in names
        ],
        "output": output.getvalue()[:100000],
        "message": "平台对固定快照执行的公开行为评测；新增测试不计入评分分母。",
    }


def execute(pipe, files, baseline, image, docker_url, name):
    import docker

    client = docker.DockerClient(base_url=docker_url, timeout=15)
    obj = stream = None
    try:
        if not image or not image.startswith("sha256:"):
            raise RuntimeError("immutable image required")
        client.images.get(image)  # Never pull a replacement for a recorded image.
        obj = client.containers.create(
            image,
            ["sleep", "90"],
            name=name,
            runtime="runsc",
            user="1000:1000",
            read_only=True,
            network_mode="none",
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            mem_limit="512m",
            memswap_limit="512m",
            nano_cpus=500000000,
            pids_limit=64,
            tmpfs={
                "/workspace": "rw,noexec,nosuid,size=8m,uid=1000,gid=1000",
                "/tmp": "rw,noexec,nosuid,size=16m",
            },
            labels={"vibeeval.grade": name},
        )
        # Verify the checked-in grading baseline against the submitted immutable image.
        for path in ("src/booking.py", "tests/test_public.py"):
            data, _ = obj.get_archive("/workspace/" + path)
            with tarfile.open(fileobj=io.BytesIO(b"".join(data))) as tar:
                raw = next(
                    tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()
                )
            if raw.decode("utf-8").replace("\r\n", "\n") != baseline[path].replace(
                "\r\n", "\n"
            ):
                raise RuntimeError("image/template version mismatch")
        obj.start()
        submitted = {entry["path"]: entry["content"] for entry in files}
        created = client.api.exec_create(
            obj.id,
            ["python", "-I", "-u", "-c", WORKER],
            stdin=True,
            stdout=True,
            stderr=False,
            tty=False,
            user="1000:1000",
        )
        stream = client.api.exec_start(created["Id"], socket=True, tty=False)
        rpc = RPC(stream)
        try:
            rpc.call("startup", "load", submitted.get("src/booking.py", ""))
        except CandidateFailure:
            rpc.failed = True
        pipe.send(trusted_tests(rpc, baseline))
    except Exception:
        logging.getLogger(__name__).exception("Objective grading infrastructure failed")
        pipe.send(
            {
                "status": "infrastructure_failed",
                "score": None,
                "tests": [],
                "passed": None,
                "total": 8,
                "output": "",
                "message": "平台评测未完成，请重试；不计零分。",
            }
        )
    finally:
        if stream is not None:
            with contextlib.suppress(Exception):
                response = getattr(stream, "_response", None)
                if response is not None:
                    response.close()
                stream.close()
        if obj is not None:
            with contextlib.suppress(Exception):
                obj.remove(force=True)
        client.close()
        pipe.close()


def run_isolated(files, baseline, image, docker_url):
    name = "vibeeval-grade-" + uuid.uuid4().hex
    ctx = multiprocessing.get_context("spawn")
    reader, writer = ctx.Pipe(duplex=False)
    worker = ctx.Process(
        target=execute, args=(writer, files, baseline, image, docker_url, name)
    )
    worker.start()
    writer.close()
    try:
        if reader.poll(75):
            return reader.recv()
        return {
            "status": "infrastructure_failed",
            "score": None,
            "tests": [],
            "passed": None,
            "total": 8,
            "output": "",
            "message": "平台评测超时，请重试；不计零分。",
        }
    except EOFError:
        return {
            "status": "infrastructure_failed",
            "score": None,
            "tests": [],
            "passed": None,
            "total": 8,
            "output": "",
            "message": "平台评测进程异常，请重试；不计零分。",
        }
    finally:
        reader.close()
        worker.join(timeout=2)
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=2)
        if worker.is_alive():
            worker.kill()
            worker.join()
        # Parent also cleans up if the child timed out or exited unexpectedly.
        import docker

        with contextlib.suppress(Exception):
            client = docker.DockerClient(base_url=docker_url, timeout=10)
            try:
                client.containers.get(name).remove(force=True)
            finally:
                client.close()

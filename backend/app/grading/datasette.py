"""Trusted assertions on the host; submitted Datasette runs only inside runsc."""

import base64
import hashlib
import io
import json
import logging
import struct
import sys
import tarfile
import time
import types
import unittest
import uuid
from contextlib import closing, suppress
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from app.stage_resources.types import safe_path

WORKER = r"""
import asyncio,base64,contextlib,json,sys,tempfile
from pathlib import Path
sys.path.insert(0,'/workspace')
loop=asyncio.new_event_loop()
apps={}
for line in sys.stdin:
    request=json.loads(line)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            op=request['op']; identity=request['app']
            if op=='health':
                value={'protocol':1,'python':list(sys.version_info[:2])}
            elif op=='create':
                from datasette.app import Datasette
                root=tempfile.TemporaryDirectory()
                paths=[]
                for name,encoded in request['files'].items():
                    assert Path(name).name==name
                    p=Path(root.name)/name;p.write_bytes(base64.b64decode(encoded));paths.append(p)
                apps[identity]=(Datasette(paths,**request['kwargs']),root)
                value=None
            elif op=='get':
                response=loop.run_until_complete(apps[identity][0].client.get(request['path']))
                assert len(response.content)<=1048576
                value={'status_code':response.status_code,'text':response.text}
            elif op=='close':
                app,root=apps.pop(identity);app.close();root.cleanup();value=None
            else:
                raise ValueError('unknown operation')
        answer={'id':request['id'],'value':value}
    except BaseException as error:
        answer={'id':request['id'],'error':type(error).__name__+': '+str(error)[:300]}
    print(json.dumps(answer),flush=True)
"""


def load_rule(root, task_version, image):
    root = Path(root)
    raw = (root / "manifest.json").read_bytes()
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("grading manifest too large")
    manifest = json.loads(raw)
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported grading bundle schema")
    rule = manifest["rules"][task_version]
    if image not in rule["images"] or not image.startswith("sha256:"):
        raise ValueError("grading image/version mismatch")
    baseline = rule["baseline"]
    if not 1 <= len(baseline) <= 200 or not all(safe_path(p) for p in baseline):
        raise ValueError("invalid grading baseline")
    criteria = rule["criteria"]
    if (
        sum(c["max"] for c in criteria) != 70
        or len({c["key"] for c in criteria}) != len(criteria)
        or any(type(c["max"]) is not int or c["max"] <= 0 for c in criteria)
        or sum(c["max"] for c in rule["manual"]) != 30
    ):
        raise ValueError("invalid grading weights")
    sources = {}
    for path, expected in rule["modules"].items():
        if not safe_path(path):
            raise ValueError("unsafe grading module path")
        target = (root / path).resolve()
        target.relative_to(root.resolve())
        content = target.read_bytes()
        if len(content) > 100000 or hashlib.sha256(content).hexdigest() != expected:
            raise ValueError("grading module changed")
        sources[path] = content.decode("utf-8")
    return {
        **rule,
        "fingerprint": hashlib.sha256(
            json.dumps(rule, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
        "sources": sources,
    }


class CandidateFailure(RuntimeError):
    pass


class CandidateObject(dict):
    """Missing keys in candidate JSON are contract failures, not host failures."""

    def __getitem__(self, key):
        try:
            return super().__getitem__(key)
        except KeyError as error:
            raise CandidateFailure("考生响应缺少必需字段") from error


def candidate_json(text):
    value = json.loads(text, object_hook=CandidateObject)

    def require(item, kind):
        if not isinstance(item, kind):
            raise CandidateFailure("考生响应字段类型不符合接口契约")
        return item

    require(value, dict)
    if "rows" in value:
        for row in require(value["rows"], list):
            require(row, dict)
    if "facet_results" in value:
        facets = require(require(value["facet_results"], dict)["results"], dict)
        for facet in facets.values():
            for row in require(require(facet, dict)["results"], list):
                require(row, dict)
                require(row["toggle_url"], str)
                require(row["count"], int)
                require(row["selected"], bool)
    if "executeSql" in value:
        stats = require(value["executeSql"], dict)
        for key in ("queryUrl", "editorUrl"):
            require(stats[key], str)
        for column in require(stats["columns"], list):
            require(column, dict)
            require(column["name"], str)
            require(column["type"], str)
        if "filter" in stats:
            filter_ = require(stats["filter"], dict)
            require(filter_["params"], dict)
            require(filter_["where"], str)
            require(filter_["description"], str)
    if "next_url" in value and value["next_url"] is not None:
        require(value["next_url"], str)
    return value


class Channel:
    def __init__(self, stream):
        self.socket = getattr(stream, "_sock", stream)
        self.socket.settimeout(12)
        self.buffer = b""
        self.deadline = time.monotonic() + 90

    def exact(self, count):
        data = b""
        while len(data) < count:
            chunk = self.socket.recv(count - len(data))
            if not chunk:
                raise CandidateFailure("考生程序提前退出")
            data += chunk
        return data

    def call(self, operation, app, **arguments):
        if time.monotonic() > self.deadline:
            raise CandidateFailure("考生程序超过评测时限")
        identity = uuid.uuid4().hex
        try:
            self.socket.sendall(
                (
                    json.dumps(
                        {"id": identity, "op": operation, "app": app, **arguments}
                    )
                    + "\n"
                ).encode()
            )
            while b"\n" not in self.buffer:
                header = self.exact(8)
                size = struct.unpack(">I", header[4:])[0]
                if (
                    header[0] != 1
                    or size > 2 * 1024 * 1024
                    or len(self.buffer) + size > 2 * 1024 * 1024
                ):
                    raise CandidateFailure("考生程序响应超出限制")
                self.buffer += self.exact(size)
            line, self.buffer = self.buffer.split(b"\n", 1)
            reply = json.loads(line)
            if reply.get("id") != identity:
                raise CandidateFailure("考生程序响应编号不一致")
            if "error" in reply:
                raise CandidateFailure(str(reply["error"])[:500])
            return reply["value"]
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise CandidateFailure("考生程序没有返回有效响应") from error


def trusted_tests(channel, rule):
    class TrustedResult(unittest.TextTestResult):
        infrastructure_errors = False

        def classify(self, err):
            error = err[1]
            candidate_error = isinstance(
                error, (AssertionError, CandidateFailure, json.JSONDecodeError)
            )
            if not candidate_error:
                self.infrastructure_errors = True

        def addError(self, test, err):
            self.classify(err)
            super().addError(test, err)

        def addSubTest(self, test, subtest, err):
            if err is not None:
                self.classify(err)
            super().addSubTest(test, subtest, err)

    class RemoteDatasette:
        def __init__(self, files, **kwargs):
            self.identity = uuid.uuid4().hex
            self.client = self
            channel.call(
                "create",
                self.identity,
                files={
                    Path(p).name: base64.b64encode(Path(p).read_bytes()).decode()
                    for p in files
                },
                kwargs=kwargs,
            )

        async def get(self, path):
            value = channel.call("get", self.identity, path=path)
            if (
                not isinstance(value, dict)
                or type(value.get("status_code")) is not int
                or not isinstance(value.get("text"), str)
            ):
                raise CandidateFailure("考生接口响应结构不正确")
            return types.SimpleNamespace(
                status_code=value["status_code"],
                text=value["text"],
                json=lambda: candidate_json(value["text"]),
            )

        def close(self):
            with suppress(CandidateFailure):
                channel.call("close", self.identity)

    fake = types.ModuleType("datasette.app")
    fake.Datasette = RemoteDatasette
    modules, cases = {}, []
    # Only the separately provisioned, hashed test bundle is compiled on host.
    with patch.dict(
        sys.modules, {"datasette": types.ModuleType("datasette"), "datasette.app": fake}
    ):
        for name, source in rule["sources"].items():
            module = types.ModuleType(
                "trusted_" + hashlib.sha256(name.encode()).hexdigest()[:12]
            )
            module.__file__ = "trusted-grading/" + name
            exec(compile(source, module.__file__, "exec"), module.__dict__)
            if getattr(module, "json", None) is json:
                # Trusted cases also decode embedded candidate page JSON.
                module.json = types.SimpleNamespace(loads=candidate_json)
            modules[name] = module
        for criterion in rule["criteria"]:
            cls = getattr(modules[criterion["module"]], criterion["case"])
            if not issubclass(cls, unittest.TestCase):
                raise ValueError("trusted test case is invalid")
            cases.append(cls(criterion["method"]))
    output = io.StringIO()
    result = unittest.TextTestRunner(
        stream=output, verbosity=2, resultclass=TrustedResult
    ).run(unittest.TestSuite(cases))
    if result.infrastructure_errors:
        return {
            "status": "infrastructure_failed",
            "score": None,
            "max": 70,
            "groups": [],
            "cap": None,
            "output": "",
            "message": "平台可信测试或适配器异常，本次未评；请维护者核查。",
        }
    failures = {
        getattr(test, "test_case", test).id()
        for test, _ in result.failures + result.errors + result.skipped
    }
    groups = [
        {
            "key": c["key"],
            "name": c["name"],
            "max": c["max"],
            "passed": case.id() not in failures,
            "score": c["max"] if case.id() not in failures else 0,
            "core": c.get("core", False),
        }
        for c, case in zip(rule["criteria"], cases, strict=True)
    ]
    return {
        "status": "completed",
        "score": sum(c["score"] for c in groups),
        "max": 70,
        "groups": groups,
        "cap": (
            rule.get("failure_cap")
            if any(c["core"] and not c["passed"] for c in groups)
            else None
        ),
        "output": output.getvalue()[:100000],
    }


def evaluate(files, rule, image, docker_url):
    import docker

    obj = initializer = volume = stream = None
    try:
        submitted = {entry["path"]: entry["content"] for entry in files}
        if set(submitted) != set(rule["baseline"]):
            raise ValueError("submitted manifest differs from published baseline")
        if any(
            submitted[p] != before
            for p, before in rule["baseline"].items()
            if p not in rule["writable"]
        ):
            raise ValueError("read-only published material changed")
        with closing(docker.DockerClient(base_url=docker_url, timeout=15)) as client:
            try:
                client.images.get(
                    image
                )  # Never pull a substitute for the recorded image.
                volume = client.volumes.create(
                    name="vibeeval-r2-grade-" + uuid.uuid4().hex,
                    labels={"vibeeval-purpose": "objective-grading"},
                )
                initializer = client.containers.create(
                    image,
                    ["true"],
                    network_mode="none",
                    read_only=True,
                    volumes={volume.name: {"bind": "/workspace", "mode": "rw"}},
                )
                obj = client.containers.create(
                    image,
                    ["sleep", "150"],
                    name="vibeeval-r2-grade-" + uuid.uuid4().hex,
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
                    volumes={volume.name: {"bind": "/workspace", "mode": "ro"}},
                    tmpfs={
                        "/tmp": "rw,noexec,nosuid,size=32m",
                    },
                )
                archive = io.BytesIO()
                with tarfile.open(fileobj=archive, mode="w") as tar:
                    directories = {
                        parent.as_posix()
                        for path in submitted
                        for parent in PurePosixPath(path).parents
                        if parent.as_posix() != "."
                    }
                    for directory in sorted(
                        directories, key=lambda value: (value.count("/"), value)
                    ):
                        member = tarfile.TarInfo(directory + "/")
                        member.type, member.mode = tarfile.DIRTYPE, 0o755
                        tar.addfile(member)
                    for path, content in submitted.items():
                        if not safe_path(path):
                            raise ValueError("invalid submitted file path")
                        data = content.encode("utf-8")
                        member = tarfile.TarInfo(path)
                        member.size, member.uid, member.gid, member.mode = (
                            len(data),
                            0,
                            0,
                            0o444,
                        )
                        tar.addfile(member, io.BytesIO(data))
                if not initializer.put_archive("/workspace", archive.getvalue()):
                    raise RuntimeError("grading snapshot was not installed")
                initializer.remove(v=False)
                initializer = None
                obj.start()
                execution = client.api.exec_create(
                    obj.id,
                    ["python", "-I", "-u", "-c", WORKER],
                    stdin=True,
                    stdout=True,
                    stderr=False,
                    tty=False,
                    user="1000:1000",
                    workdir="/workspace",
                )
                stream = client.api.exec_start(execution["Id"], socket=True, tty=False)
                channel = Channel(stream)
                if channel.call("health", "probe") != {
                    "protocol": 1,
                    "python": [3, 12],
                }:
                    raise RuntimeError("grading worker/environment is not ready")
                return trusted_tests(channel, rule)
            finally:
                if stream is not None:
                    with suppress(Exception):
                        stream._response.close()
                    with suppress(Exception):
                        stream.close()
                if obj is not None:
                    obj.remove(force=True, v=False)
                if initializer is not None:
                    initializer.remove(force=True, v=False)
                if volume is not None:
                    volume.remove()
    except Exception:
        logging.getLogger(__name__).exception(
            "R2 objective grading infrastructure failed"
        )
        return {
            "status": "infrastructure_failed",
            "score": None,
            "max": 70,
            "groups": [],
            "cap": None,
            "output": "",
            "message": "平台评测未完成；本次未评，不计零分。",
        }

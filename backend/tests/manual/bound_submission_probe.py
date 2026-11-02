import uuid
from types import SimpleNamespace

import docker

from app.sandbox.bound_adapter import BoundSandboxAdapter

c = docker.DockerClient(base_url="unix:///var/run/docker.sock", timeout=20)
a = BoundSandboxAdapter(c, "test-node")
v = SimpleNamespace(
    session_id="probe-" + uuid.uuid4().hex,
    status="provisioning",
    sandbox_id=None,
    sandbox_resource_key="probe-" + uuid.uuid4().hex,
    sandbox_image_ref="sha256:0819c1770d55365f1de25e3c21f17893a66b9ca8ecb91373dca5fb85c0dc9dd5",
    sandbox_backend_ref="test-node",
)
try:
    first = a.ensure_bound(v)
    assert a.ensure_bound(v) == first
    v.sandbox_id = first
    assert a.ensure_bound(v) == first
    v.status = "submitting"
    result = a.freeze_and_export(v)
    assert len(result.files) == 6
    assert len(a.freeze_and_export(v).files) == 6
    assert any(
        f.path == "tests/test_candidate.py" and f.content == "" for f in result.files
    )
    print("PASS unique create, reconnect, freeze, six-file export, frozen retry")
    v.status = "submitted"
    a.delete_sandbox(v)
    a.delete_sandbox(v)
    c.volumes.get(v.sandbox_resource_key + "-data")
    print("PASS idempotent cleanup retains volume")
finally:
    try:
        c.containers.get(v.sandbox_resource_key).remove(force=True)
    except docker.errors.NotFound:
        pass
    c.volumes.get(v.sandbox_resource_key + "-data").remove()
    c.close()

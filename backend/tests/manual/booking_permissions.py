"""Opt-in Linux/gVisor test. All permission probes execute inside the container."""

import uuid

import docker

IMAGE = "sha256:0819c1770d55365f1de25e3c21f17893a66b9ca8ecb91373dca5fb85c0dc9dd5"
PROBE = r"""
import os
from pathlib import Path
assert os.getuid() == 1000
root = Path('/workspace')
editable = ['src/booking.py', 'tests/test_candidate.py']
protected = ['README.md','requirements.txt','src/main.py','tests/test_public.py']
checks = 0
def denied(fn):
    global checks
    try:
        fn()
    except PermissionError:
        checks += 1
    else:
        raise AssertionError('protected operation unexpectedly succeeded')
for name in editable:
    p = root / name
    original = p.read_bytes()
    p.write_bytes(original + b'\n# write probe\n')
    assert p.read_bytes().endswith(b'# write probe\n')
    p.write_bytes(original)
    checks += 1
for name in protected:
    p = root / name
    denied(lambda: p.write_text('corrupt'))
    denied(p.unlink)
    denied(lambda: p.chmod(0o666))
    denied(lambda: p.rename(p.with_name('renamed')))
    denied(lambda: os.replace(root/'tests/test_candidate.py', p))
for name in ['src','tests']:
    p = root / name
    denied(lambda: p.rename(root/'moved'))
    denied(lambda: (p/'escape').symlink_to('/etc/passwd'))
    denied(lambda: (p/'unexpected.py').write_text('x'))
assert not (root/'maintainer').exists()
assert sorted(str(p.relative_to(root)) for p in root.rglob('*') if p.is_file()) == sorted(editable+protected)
print('PASS permission assertions:', checks)
"""


def main():
    client = docker.DockerClient(base_url="unix:///var/run/docker.sock", timeout=30)
    name = "vibeeval-permissions-" + uuid.uuid4().hex
    volume = client.volumes.create(name=name)
    container = None
    try:
        container = client.containers.run(
            IMAGE,
            ["sleep", "300"],
            detach=True,
            runtime="runsc",
            user="1000:1000",
            network_mode="none",
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            mem_limit="512m",
            memswap_limit="512m",
            pids_limit=64,
            volumes={volume.name: {"bind": "/workspace", "mode": "rw"}},
            tmpfs={"/tmp": "rw,nosuid,noexec,size=16m"},
        )
        result = container.exec_run(["python", "-c", PROBE])
        print(result.output.decode())
        assert result.exit_code == 0
        result = container.exec_run(
            [
                "python",
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-p",
                "test_*.py",
                "-v",
            ]
        )
        output = result.output.decode()
        print(output)
        assert result.exit_code == 1
        assert "Ran 8 tests" in output and "FAILED (failures=2)" in output
        assert output.count(" ... ok") == 6
        assert "test_adjacent_reservations_allowed" in output
        assert "test_cancel_releases_room" in output
        print("PASS original problem baseline after volume permission probes")
    finally:
        if container is not None:
            container.remove(force=True)
        volume.remove()
        client.close()


if __name__ == "__main__":
    main()

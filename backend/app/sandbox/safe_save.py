"""Paused-volume atomic writes. The helper runs only fixed platform code."""

import base64
import hashlib
import re
import time

from docker.errors import NotFound

from app.workspace.files import EDITABLE, encode_content, validate_path

STAGING_FILES = frozenset(path + ".vibeeval-save" for path in EDITABLE)

# Isolated Python ignores candidate PYTHONPATH and never imports /workspace.
WRITE_SCRIPT = r"""
import base64,hashlib,os,stat,sys,time
path,digest=sys.argv[1:3]
deadline=int(sys.argv[3])
assert path in ('src/booking.py','tests/test_candidate.py')
data=base64.b64decode(''.join(sys.argv[4:]),validate=True)
assert len(data)<=1048576
root=os.open('/workspace',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
parent,name=path.split('/')
directory=os.open(parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=root)
for fd in (root,directory):
    info=os.fstat(fd)
    assert info.st_uid==0 and info.st_mode & 0o022 == 0
old=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory)
with os.fdopen(old,'rb') as source:
    info=os.fstat(source.fileno())
    assert stat.S_ISREG(info.st_mode) and info.st_nlink==1
    assert info.st_size<=1048576
    assert hashlib.sha256(source.read(1048577)).hexdigest()==digest
stage=name+'.vibeeval-save'
try:
    os.unlink(stage,dir_fd=directory)
except FileNotFoundError:
    pass
fd=os.open(stage,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
with os.fdopen(fd,'wb') as target:
    target.write(data)
    target.flush()
    os.fchmod(target.fileno(),0o644)
    os.fchown(target.fileno(),1000,1000)
    os.fsync(target.fileno())
assert time.time()<deadline
os.replace(stage,name,src_dir_fd=directory,dst_dir_fd=directory)
os.fsync(directory)
os.close(directory)
os.close(root)
"""


def writer_name(view):
    if not view.sandbox_resource_key:
        raise ValueError("sandbox resource key required")
    return view.sandbox_resource_key + "-writer"


def remove_writer(client, view):
    """Stop any accepted save before export; absence is idempotent success."""
    try:
        writer = client.containers.get(writer_name(view))
    except NotFound:
        return
    labels = writer.attrs.get("Config", {}).get("Labels") or {}
    if (
        writer.name != writer_name(view)
        or labels.get("vibeeval.session") != view.session_id
        or labels.get("vibeeval.role") != "workspace-writer"
        or labels.get("vibeeval.original") != view.sandbox_id
        or writer.attrs.get("Image") != client.images.get(view.sandbox_image_ref).id
    ):
        raise ValueError("writer identity mismatch")
    writer.remove(force=True, v=False)
    try:
        client.containers.get(writer_name(view))
    except NotFound:
        return
    raise ValueError("writer still exists")


def save_file(adapter, view, path, expected_digest, content):
    validator = getattr(adapter, "validate_path", validate_path)
    validator(path, write=True)
    data = encode_content(content)
    if (
        not isinstance(expected_digest, str)
        or re.fullmatch(r"[a-f0-9]{64}", expected_digest) is None
    ):
        raise ValueError("valid expected digest required")
    if view.status != "in_progress":
        raise ValueError("active exam required")
    if view.deadline_at is None or time.time() >= view.deadline_at:
        raise ValueError("exam deadline reached")
    obj = adapter._container(view)
    if obj.status == "running":
        obj.pause()
        obj.reload()
    if obj.status != "paused":
        raise ValueError("sandbox must be paused for saving")
    # On any error leave the original paused. A retry/submit removes the helper
    # before accessing this volume; a timeout never grants permission to edit.
    remove_writer(adapter.client, view)
    current = adapter.read_file(view, path)
    if hashlib.sha256(current.encode("utf-8")).hexdigest() != expected_digest:
        obj.unpause()
        return {"saved": False, "content": current}
    if data == current.encode("utf-8"):
        obj.unpause()
        return {"saved": True}
    volumes = [
        m for m in obj.attrs.get("Mounts", []) if m.get("Destination") == "/workspace"
    ]
    if (
        len(volumes) != 1
        or volumes[0].get("Type") != "volume"
        or volumes[0].get("Name") != view.sandbox_resource_key + "-data"
    ):
        raise ValueError("workspace volume mismatch")
    volume = adapter.client.volumes.get(volumes[0]["Name"])
    if volume.attrs.get("Labels") != {
        "vibeeval.session": view.session_id,
        "vibeeval.image": obj.attrs["Image"],
    }:
        raise ValueError("workspace volume ownership mismatch")
    writer = adapter.client.containers.create(
        obj.attrs["Image"],
        ["sleep", "infinity"],
        name=writer_name(view),
        runtime="runsc",
        user="0:0",
        read_only=True,
        network_mode="none",
        cap_drop=["ALL"],
        cap_add=["CHOWN"],
        security_opt=["no-new-privileges"],
        mem_limit="256m",
        memswap_limit="256m",
        nano_cpus=500_000_000,
        pids_limit=64,
        volumes={volume.name: {"bind": "/workspace", "mode": "rw"}},
        labels={
            "vibeeval.session": view.session_id,
            "vibeeval.role": "workspace-writer",
            "vibeeval.original": view.sandbox_id,
        },
        log_config={"Type": "none"},
    )
    try:
        writer.start()
        encoded = base64.b64encode(data).decode("ascii")
        chunks = [encoded[i : i + 32768] for i in range(0, len(encoded), 32768)]
        result = writer.exec_run(
            [
                "python",
                "-I",
                "-c",
                getattr(adapter, "write_script", WRITE_SCRIPT),
                path,
                expected_digest,
                str(view.deadline_at),
                *chunks,
            ],
            user="0:0",
        )
        if result.exit_code != 0:
            raise ValueError("atomic write failed")
    finally:
        remove_writer(adapter.client, view)
    if adapter.read_file(view, path) != content:
        raise ValueError("saved content verification failed")
    obj.unpause()
    return {"saved": True}

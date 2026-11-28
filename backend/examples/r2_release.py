"""Build a new four-stage package on the sandbox host without changing v1 resources."""

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

import docker

from app.grading.datasette import load_rule
from examples.datasette_exam import TASKS


def write_json(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )


def build(destination, old_bundle):
    contexts = Path(os.environ["VIBEEVAL_DATASETTE_CONTEXTS"])
    keys = ["VIBEEVAL_DATASETTE_COLUMN_IMAGE", "VIBEEVAL_DATASETTE_FACET_IMAGE"]
    public = Path(__file__).resolve().parents[2] / "problems/datasette-r2"
    if destination.exists():
        raise FileExistsError(destination)
    client = docker.from_env()
    try:
        with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
            release = Path(tmp) / "release"
            trusted = release / "grading"
            shutil.copytree(old_bundle, trusted)
            manifest = json.loads((trusted / "manifest.json").read_bytes())
            images = {}
            for (task, _, _), key in zip(TASKS, keys, strict=True):
                context = release / "contexts" / task
                shutil.copytree(contexts / task, context)
                spec = json.loads((context / "manifest.json").read_bytes())
                old_version = spec["task_version"]
                old_image = os.environ[key]
                load_rule(old_bundle, old_version, old_image)
                rule = manifest["rules"][old_version]
                files = {
                    p: (context / "workspace" / p).read_bytes().decode()
                    for p in spec["files"]
                }
                if files != rule["baseline"]:
                    raise ValueError("Trusted baseline differs from " + task)
                readme = (public / task / "README.md").read_text(encoding="utf-8")
                (context / "workspace/README.md").write_bytes(readme.encode())
                sha = hashlib.sha256(readme.encode()).hexdigest()
                new_version = task + "-" + sha[:16]
                spec.update(
                    task_version=new_version,
                    file_sha256={**spec.get("file_sha256", {}), "README.md": sha},
                )
                write_json(context / "manifest.json", spec)
                image_context = Path(tmp) / task
                image_context.mkdir()
                (image_context / "README.md").write_bytes(readme.encode())
                (image_context / "README.md").chmod(0o444)
                parent = client.images.get(old_image)
                tag = "vibeeval-release-base:" + old_image.split(":")[1]
                parent.tag(*tag.split(":"))
                (image_context / "Dockerfile").write_text(
                    f"FROM {tag}\nCOPY README.md /workspace/README.md\n"
                )
                image, _ = client.images.build(
                    path=str(image_context), pull=False, rm=True
                )
                installed = client.containers.run(
                    image.id,
                    ["cat", "/workspace/README.md"],
                    remove=True,
                    network_disabled=True,
                )
                if installed != readme.encode():
                    raise ValueError("Incorrect README in built image")
                images[task] = image.id
                manifest["rules"][new_version] = {
                    **rule,
                    "rule_version": new_version + "-objective",
                    "images": [image.id],
                    "baseline": {**files, "README.md": readme},
                }
                write_json(trusted / "manifest.json", manifest)
                load_rule(trusted, new_version, image.id)
            write_json(release / "images.json", images)
            release.rename(destination)
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("old_bundle", type=Path)
    args = parser.parse_args()
    build(args.destination, args.old_bundle)

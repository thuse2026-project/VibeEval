"""Build the two candidate images from the TinyDB v1 published file manifest."""

import argparse
import io
import json
import tarfile

import docker

from examples.r2_exam import checked_source, source_files


def context(folder, base):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        files = source_files(folder)
        if not files:
            raise ValueError(f"No published source files for {folder}")
        dockerfile = f"FROM {base}\nCOPY repository/ /workspace/repository/\n"
        contents = {"Dockerfile": dockerfile.encode()}
        for path, sha in files.items():
            contents[path] = checked_source(folder, path, sha)
        for path, data in contents.items():
            entry = tarfile.TarInfo(path)
            entry.size = len(data)
            entry.mode = 0o644
            archive.addfile(entry, io.BytesIO(data))
    buffer.seek(0)
    return buffer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base", default="python:3.13-slim", help="locally available Python image"
    )
    args = parser.parse_args()
    client = docker.from_env()
    try:
        base = client.images.get(args.base).id
        result = {}
        for key, folder in (
            ("VIBEEVAL_TINYDB_CORRECT_IMAGE", "01-explore"),
            ("VIBEEVAL_TINYDB_REVIEW_IMAGE", "03-review"),
        ):
            with context(folder, base) as archive:
                image, _ = client.images.build(
                    fileobj=archive, custom_context=True, rm=True, network_mode="none"
                )
            result[key] = image.id
        print(json.dumps(result, indent=2))
    finally:
        client.close()


if __name__ == "__main__":
    main()

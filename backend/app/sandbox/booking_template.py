"""Build a minimal trusted context from the reviewed Git object, not a checkout."""

import io
import subprocess
import tarfile

REVISION = "f898a77"
FILES = (
    "README.md",
    "requirements.txt",
    "src/booking.py",
    "src/main.py",
    "tests/test_public.py",
)
EDITABLE = ("src/booking.py", "tests/test_candidate.py")
BASE = "public.ecr.aws/docker/library/python@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159"
DOCKERFILE = f"""FROM {BASE}
COPY problem/ /workspace/
RUN touch /workspace/tests/test_candidate.py && chown -R 0:0 /workspace && find /workspace -type d -exec chmod 755 {{}} + && find /workspace -type f -exec chmod 444 {{}} + && chown 1000:1000 /workspace/src/booking.py /workspace/tests/test_candidate.py && chmod 644 /workspace/src/booking.py /workspace/tests/test_candidate.py
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /workspace
USER 1000:1000
CMD ["sleep", "infinity"]
"""


def build_context(repo):
    """Reads trusted source bytes only. Never imports or runs problem code."""
    commit = (
        subprocess.check_output(["git", "rev-parse", REVISION + "^{commit}"], cwd=repo)
        .decode()
        .strip()
    )
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        entries = {"Dockerfile": DOCKERFILE.encode()}
        for path in FILES:
            entries["problem/" + path] = subprocess.check_output(
                ["git", "show", commit + ":problems/python-debug-01/" + path], cwd=repo
            )
        for path, content in entries.items():
            info = tarfile.TarInfo(path)
            info.size, info.mode = len(content), 0o644
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


def main():
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="Output tar file for docker build")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[3]
    args.output.write_bytes(build_context(repo))


if __name__ == "__main__":
    main()

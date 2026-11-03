"""Run opt-in workspace tests against a disposable local PostgreSQL container.

Run on the dedicated Linux validation host, from backend/, with SANDBOX_IMAGE_REF
set to a trusted, already-built image ID. No candidate code runs on the host.
"""

import argparse
import os
import secrets
import subprocess
import sys
import time
import uuid

import docker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all-tests", action="store_true")
    args = parser.parse_args()
    if not os.environ.get("SANDBOX_IMAGE_REF", "").startswith("sha256:"):
        raise ValueError("set SANDBOX_IMAGE_REF to the trusted local image ID")
    client = docker.DockerClient(base_url="unix:///var/run/docker.sock", timeout=20)
    password = secrets.token_urlsafe(32)
    database = None
    try:
        database = client.containers.run(
            "public.ecr.aws/docker/library/postgres:16-alpine",
            detach=True,
            name="vibeeval-workspace-db-" + uuid.uuid4().hex,
            environment={"POSTGRES_PASSWORD": password},
            ports={"5432/tcp": ("127.0.0.1", None)},
            mem_limit="512m",
        )
        for _ in range(60):
            if database.exec_run(["pg_isready", "-U", "postgres"]).exit_code == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("test database did not become ready")
        database.reload()
        port = database.attrs["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"]
        env = dict(
            os.environ,
            VIBEEVAL_RUN_DB_TESTS="1",
            VIBEEVAL_RUN_WORKSPACE_TESTS="1",
            DATABASE_URL=f"postgresql+psycopg://postgres:{password}@127.0.0.1:{port}/postgres",
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-p",
                "test_*.py" if args.all_tests else "test_workspace_gvisor.py",
                "-v",
            ],
            env=env,
            timeout=600 if args.all_tests else 240,
        )
        return result.returncode
    finally:
        if database is not None:
            database.remove(force=True, v=True)
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())

"""Verify Agent persistence against an owned temporary local PostgreSQL.

From backend/: python tests/manual/agent_database_probe.py [--all-tests | --pattern GLOB]
Uses an existing postgres:16-alpine image; no project database or .env is changed.
"""

import argparse
import os
import re
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]


def docker(*args, env=None, check=True):
    return subprocess.run(
        ["docker", *args], env=env, text=True, capture_output=True, check=check
    )


def run(all_tests=False, pattern=None):
    env = dict(os.environ)
    env.update(
        POSTGRES_USER="agent_probe",
        POSTGRES_DB="agent_probe",
        POSTGRES_PASSWORD=secrets.token_urlsafe(32),
        POSTGRES_HOST="127.0.0.1",
        DATABASE_URL="",
        VIBEEVAL_RUN_DB_TESTS="1",
        VIBEEVAL_RUN_GVISOR_TESTS="0",
        VIBEEVAL_RUN_WORKSPACE_TESTS="0",
        PYTHONUTF8="1",
    )
    container_id = None
    name = "vibeeval-agent-probe-" + uuid.uuid4().hex[:12]
    try:
        created = docker(
            "run",
            "-d",
            "--rm",
            "--pull=never",
            "--name",
            name,
            "--label",
            "vibeeval.agent-probe=isolated-test",
            "--publish",
            "127.0.0.1::5432",
            "--env",
            "POSTGRES_USER",
            "--env",
            "POSTGRES_DB",
            "--env",
            "POSTGRES_PASSWORD",
            "postgres:16-alpine",
            env=env,
        )
        container_id = created.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{64}", container_id):
            raise RuntimeError("Docker did not return an owned container ID")
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            ready = docker(
                "exec",
                container_id,
                "pg_isready",
                "-h",
                "127.0.0.1",
                "-U",
                env["POSTGRES_USER"],
                check=False,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("temporary PostgreSQL did not become ready")
        binding = docker("port", container_id, "5432/tcp").stdout.strip()
        match = re.fullmatch(r"127\.0\.0\.1:(\d+)", binding)
        if not match:
            raise RuntimeError("temporary PostgreSQL port is not loopback-only")
        env["POSTGRES_PORT"] = match.group(1)
        runtime = BACKEND.parent / "runtime"
        runtime.mkdir(exist_ok=True)
        args = [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--data-file=" + str(runtime / "agent-integration-stage1.coverage"),
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
        ]
        if not all_tests:
            args.extend(["-p", pattern or "test_agent_integration*.py"])
        args.append("-q")
        print(
            f"Temporary PostgreSQL ready: {container_id[:12]} on loopback port {env['POSTGRES_PORT']}",
            flush=True,
        )
        return subprocess.run(args, cwd=BACKEND, env=env).returncode
    finally:
        if container_id and re.fullmatch(r"[0-9a-f]{64}", container_id):
            removed = docker("rm", "-f", "-v", container_id, check=False)
            if removed.returncode:
                raise RuntimeError(
                    "could not remove owned temporary PostgreSQL container"
                )
            print("Owned temporary PostgreSQL removed.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--all-tests", action="store_true")
    selection.add_argument(
        "--pattern", help="Run one unittest file pattern against the isolated database"
    )
    args = parser.parse_args()
    try:
        code = run(args.all_tests, args.pattern)
    except (subprocess.CalledProcessError, RuntimeError) as error:
        # Docker output may include its environment; report only the operation type.
        parser.exit(
            1, f"Temporary PostgreSQL validation failed: {type(error).__name__}\n"
        )
    raise SystemExit(code)


if __name__ == "__main__":
    main()

"""Exercise built images, HTTP proxying and persistence in a disposable project.

Requires a ci-only-password database named vibeeval_compose_* and an explicit
vibeeval-app-check-* Compose project. Never removes volumes or runs against live data.
"""

import argparse
import http.cookiejar
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class StackCheck:
    def __init__(self, args):
        self.docker = args.docker
        self.command = [
            args.docker,
            "compose",
            "--env-file",
            str(Path(args.env_file).resolve()),
            "--project-name",
            args.project_name,
            "-f",
            str(ROOT / "deploy/compose.yml"),
        ]
        self.browser = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.ProxyHandler({}),
        )
        self.config = json.loads(self.compose("config", "--format", "json"))
        services = self.config["services"]
        database = services["postgres"]["environment"]
        if (
            not args.project_name.startswith("vibeeval-app-check-")
            or not database["POSTGRES_DB"].startswith("vibeeval_compose_")
            or database["POSTGRES_PASSWORD"] != "ci-only-password"
        ):
            raise ValueError("This check requires a disposable smoke-test stack")
        for service in ("postgres", "redis", "backend", "migrate"):
            assert not services[service].get("ports"), service
        ports = services["web"]["ports"]
        assert len(ports) == 1 and ports[0]["host_ip"] == "127.0.0.1"
        self.url = "http://127.0.0.1:" + str(ports[0]["published"])
        assert not any(
            mount["target"] == "/var/run/docker.sock"
            for mount in services["backend"]["volumes"]
        )
        gvisor = json.loads(
            self.compose(
                "-f",
                str(ROOT / "deploy/compose.gvisor.yml"),
                "config",
                "--format",
                "json",
            )
        )["services"]
        assert "app.sandbox.http_app:create_app" in gvisor["backend"]["command"]
        assert "--watch" in gvisor["maintenance"]["command"]
        for name in ("backend", "maintenance"):
            env = gvisor[name]["environment"]
            assert env["SANDBOX_DOCKER_URL"] == "unix:///var/run/docker.sock"
            assert (
                env["SANDBOX_BACKEND_REF"]
                == gvisor["backend"]["environment"]["SANDBOX_BACKEND_REF"]
            )
            assert env["POSTGRES_DB"] == database["POSTGRES_DB"]
            mounts = {mount["target"]: mount for mount in gvisor[name]["volumes"]}
            assert mounts["/var/run/docker.sock"]["read_only"]
            assert mounts["/opt/vibeeval/runtime"]["source"] == "app_data"
            assert gvisor[name]["group_add"] == gvisor["backend"]["group_add"]
        assert gvisor["maintenance"]["healthcheck"]["disable"]

    def compose(self, *args, source=None):
        result = subprocess.run(
            [*self.command, *args],
            input=source,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=True,
            timeout=180,
        )
        return result.stdout

    def fixture(self, operation, *args):
        return json.loads(
            self.compose(
                "exec",
                "-T",
                "--env",
                "VIBEEVAL_DEPLOYMENT_SMOKE=1",
                "backend",
                "python",
                "-",
                operation,
                *args,
                source=(ROOT / "tests/deployment/seed.py").read_text(encoding="utf-8"),
            )
        )

    def request(
        self, path, *, data=None, expected=200, browser_header=True, extra_headers=None
    ):
        headers = {"X-VibeEval-Request": "1"} if browser_header else {}
        headers.update(extra_headers or {})
        if data is not None:
            data = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        if path.endswith("/submit"):
            headers["Idempotency-Key"] = "deployment-smoke-replay"
        request = urllib.request.Request(self.url + path, data=data, headers=headers)
        try:
            response = self.browser.open(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            assert response.status == expected, (path, response.status, expected)
            return response.headers, response.read()

    def wait_for_backend(self):
        deadline = time.monotonic() + 45
        while True:
            try:
                self.request("/api/v1/admin/me", expected=401)
                return
            except (AssertionError, OSError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)

    def login(self, fixture):
        self.request(
            "/api/v1/admin/login",
            data={key: fixture[key] for key in ("username", "password")},
        )

    def check_detail(self, receipt):
        headers, payload = self.request(
            "/api/v1/admin/submissions/" + receipt["submission_id"]
        )
        assert headers["Cache-Control"] == "no-store"
        detail = json.loads(payload)
        assert detail["snapshot_id"] == receipt["snapshot_id"]
        assert detail["files"] == [
            {"path": "answer.py", "content": "# persisted smoke\n"}
        ]
        assert detail["public_test_output"] is None
        _, payload = self.request(
            "/api/v1/sessions/" + receipt["session_id"] + "/submit",
            data={},
            expected=202,
        )
        assert json.loads(payload) == receipt

    def run(self):
        self.compose("up", "-d", "--wait", "--wait-timeout", "120")
        headers, index = self.request("/")
        assert "text/html" in headers["Content-Type"]
        assert headers["Cache-Control"] == "no-cache"
        for path in re.findall(rb'(?:src|href)="(/assets/[^\"]+)"', index):
            headers, _ = self.request(path.decode())
            assert "immutable" in headers["Cache-Control"]
        _, fallback = self.request("/admin/submissions/unknown")
        assert fallback == index
        self.request("/assets/missing.js", expected=404)
        headers, _ = self.request("/api/v1/unknown", expected=404)
        assert "application/json" in headers["Content-Type"]
        self.request("/api/v1/sessions/current", expected=403, browser_header=False)
        headers, _ = self.request("/api/v1/sessions/current", expected=401)
        assert headers["Cache-Control"] == "no-store"
        assert "HttpOnly" in headers["Set-Cookie"]
        fixture = self.fixture("prepare")
        # Keep nginx running while the backend gets a new container/IP.
        self.compose("up", "-d", "--force-recreate", "--no-deps", "backend")
        self.wait_for_backend()
        self.login(fixture)
        # Browser-supplied forwarding headers must not create fresh rate buckets.
        for index in range(6):
            self.request(
                "/api/v1/admin/login",
                data={"username": "nonexistent", "password": "incorrect-password"},
                expected=401 if index < 5 else 429,
                extra_headers={"X-Forwarded-For": f"192.0.2.{index + 1}"},
            )
        _, payload = self.request(
            "/api/v1/sessions/verify-token", data={"token": fixture["token"]}
        )
        session_id = json.loads(payload)["session_id"]
        self.request(f"/api/v1/sessions/{session_id}/submit", data={}, expected=503)
        receipt = self.fixture("persist", session_id)
        self.check_detail(receipt)
        self.compose("exec", "-T", "backend", "python", "-m", "alembic", "check")
        self.compose(
            "exec",
            "-T",
            "backend",
            "python",
            "-c",
            "import os; from pathlib import Path; "
            "assert os.getuid() == 1000; "
            "assert not Path('/opt/vibeeval/maintainer').exists(); "
            "assert not Path('/opt/vibeeval/.env').exists()",
        )
        # Recreate all containers and the network, retaining only named volumes.
        self.compose("down")
        self.compose("up", "-d", "--wait", "--wait-timeout", "120")
        self.request("/api/v1/admin/me", expected=401)
        self.login(fixture)
        self.check_detail(receipt)
        _, payload = self.request(f"/api/v1/sessions/{session_id}/status")
        assert json.loads(payload)["status"] == "submitted"
        print(
            "Application stack smoke passed: proxy, cookies, migrations, persistent receipt and files"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--config-only", action="store_true")
    args = parser.parse_args()
    stack = StackCheck(args)
    if args.config_only:
        print("Base and gVisor application configurations passed")
    else:
        stack.run()


if __name__ == "__main__":
    main()

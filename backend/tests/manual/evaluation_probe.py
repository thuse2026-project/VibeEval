"""Explicit server-only acceptance. Uses disposable runsc containers, no business writes."""

import os
from pathlib import Path

from app.admin.evaluation import baseline_files
from app.grading.runner import run_isolated


def main():
    baseline = baseline_files()
    root = Path(__file__).resolve().parents[3]
    reference = (root / "maintainer/python-debug-01/reference/booking.py").read_text()
    image = os.environ["SANDBOX_IMAGE_REF"]
    socket = os.environ.get("SANDBOX_DOCKER_URL", "unix:///var/run/docker.sock")
    cases = [
        ("original", baseline["src/booking.py"], 45),
        ("reference", reference, 60),
        (
            "long-valid-id",
            reference.replace("uuid4().hex", '"x" * 129 + uuid4().hex'),
            60,
        ),
        (
            "fake-ok-and-monkeypatch",
            baseline["src/booking.py"]
            + '\nprint("Ran 8 tests\\nOK")\nimport unittest\nunittest.TestCase.run=lambda *a,**kw: None\n',
            45,
        ),
        ("syntax-error", "this is invalid python !!!", 0),
        ("early-exit", "import os\nos._exit(0)", 0),
        ("infinite-loop", "while True: pass", 0),
    ]
    for title, code, expected in cases:
        result = run_isolated(
            [{"path": "src/booking.py", "content": code}], baseline, image, socket
        )
        print(title, result["status"], result["score"], flush=True)
        assert result["status"] == "completed" and result["score"] == expected, result
    result = run_isolated([], baseline, "sha256:" + "0" * 64, socket)
    assert result["status"] == "infrastructure_failed" and result["score"] is None
    print("unavailable-image: ungraded; all acceptance cases passed", flush=True)


if __name__ == "__main__":
    main()

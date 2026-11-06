"""在临时副本验证原始缺陷与参考修复；不是考生答卷评分器。"""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MAINTAINER = Path(__file__).resolve().parent
SAMPLE = MAINTAINER.parents[1] / "problems/python-debug-01"
EXPECTED_FAILURES = {
    "test_public.BookingServiceTests.test_adjacent_reservations_allowed",
    "test_public.BookingServiceTests.test_cancel_releases_room",
}


def fingerprints() -> dict[str, str]:
    return {
        str(path.relative_to(SAMPLE)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in SAMPLE.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def run_suite(root: Path) -> None:
    sys.path.insert(0, str(root / "src"))
    suite = unittest.defaultTestLoader.discover(str(root / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(json.dumps({
        "run": result.testsRun,
        "failures": [test.id() for test, _ in result.failures],
        "errors": [test.id() for test, _ in result.errors],
        "skipped": len(result.skipped),
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses),
    }))


def check(root: Path, expected: set[str]) -> dict:
    process = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-X", "utf8", str(Path(__file__).resolve()), "--worker", str(root)],
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    if process.returncode:
        raise SystemExit(f"验证进程异常退出：\n{process.stderr}")
    try:
        result = json.loads(process.stdout)
    except json.JSONDecodeError:
        raise SystemExit(f"验证结果无法读取：\n{process.stdout}\n{process.stderr}") from None
    if (result["run"] != 8 or set(result["failures"]) != expected
            or len(result["failures"]) != len(expected) or result["errors"]
            or result["skipped"] or result["expected_failures"] or result["unexpected_successes"]):
        raise SystemExit(f"样题状态不符合预期：{result}\n{process.stderr}")
    return result


def verify() -> None:
    before = fingerprints()
    if not before:
        raise SystemExit("考生工程不存在或为空")
    with tempfile.TemporaryDirectory(prefix="vibeeval-sample-") as temporary:
        original = Path(temporary) / "original"
        reference = Path(temporary) / "reference"
        for destination in (original, reference):
            shutil.copytree(SAMPLE, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copyfile(MAINTAINER / "reference/booking.py", reference / "src/booking.py")
        original_result = check(original, EXPECTED_FAILURES)
        reference_result = check(reference, set())
    if fingerprints() != before:
        raise SystemExit("验证期间原始考生工程发生变化，请检查")
    print(f"原始版本：{original_result['run']} 项测试，2 项预期失败，0 项错误")
    for name in sorted(EXPECTED_FAILURES):
        print(f"  预期失败：{name.rsplit('.', 1)[1]}")
    print(f"参考修复：{reference_result['run']} 项测试全部通过")
    print("原始考生工程未修改；参考答案仅在临时副本使用，副本已清理。")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        run_suite(Path(sys.argv[2]))
    elif len(sys.argv) == 1:
        verify()
    else:
        raise SystemExit("用法：python maintainer/python-debug-01/verify_sample.py")

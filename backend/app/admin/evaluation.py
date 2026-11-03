"""Examiner results over immutable snapshots; existing submission records stay unchanged."""

import difflib
import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from app.admin.router import current_session
from app.admin.submissions import Reader, SubmissionDetail
from app.core.security import require_browser_header
from app.db.models import Submission

TEMPLATE = Path(__file__).resolve().parents[3] / "problems/python-debug-01"
RULE = "python-debug-01-public-60-v1"
GATE = threading.Lock()
router = APIRouter(prefix="/api/v1/admin", dependencies=[Depends(current_session)])


def baseline_files():
    manifest = json.loads(
        (Path(__file__).resolve().parents[1] / "grading/baseline.json").read_text(
            encoding="utf-8"
        )
    )
    if manifest["task_version"] != "python-debug-01:f898a77":
        raise ValueError("unsupported baseline version")
    return manifest["files"]


def changes(files, baseline):
    current = {f["path"]: f["content"] for f in files}
    result = []
    for path in sorted(current.keys() | baseline.keys()):
        before, after = baseline.get(path, ""), current.get(path, "")
        if before == after and path in current and path in baseline:
            continue
        if (
            max(len(before), len(after)) > 40000
            or max(before.count("\n"), after.count("\n")) > 1000
        ):
            result.append(
                {
                    "path": path,
                    "kind": (
                        "added"
                        if path not in baseline
                        else "deleted" if path not in current else "modified"
                    ),
                    "added": None,
                    "removed": None,
                    "diff": "",
                    "truncated": True,
                }
            )
            continue
        diff = list(
            difflib.unified_diff(
                before.splitlines(),
                after.splitlines(),
                fromfile="原题/" + path,
                tofile="答卷/" + path,
                lineterm="",
            )
        )
        result.append(
            {
                "path": path,
                "kind": (
                    "added"
                    if path not in baseline
                    else "deleted" if path not in current else "modified"
                ),
                "added": sum(
                    s.startswith("+") and not s.startswith("+++") for s in diff
                ),
                "removed": sum(
                    s.startswith("-") and not s.startswith("---") for s in diff
                ),
                "diff": "\n".join(diff)[:100000],
                "truncated": len("\n".join(diff)) > 100000,
            }
        )
    return result


def context(reader, submission_id):
    from app.submissions.service import SubmissionService

    if not isinstance(reader, SubmissionService):
        raise HTTPException(503, "当前提交服务未提供可信评测能力")
    try:
        detail = reader.get_submission(submission_id)
    except Exception as error:
        raise HTTPException(503, "答卷快照暂不可用，请稍后重试") from error
    if detail is None:
        raise HTTPException(404, "未找到已提交答卷")
    try:
        detail = SubmissionDetail.model_validate(detail).model_dump()
    except ValueError as error:
        raise HTTPException(502, "答卷数据不完整，无法评测") from error
    with reader.factory() as db:
        row = db.get(Submission, submission_id)
        task, image = row.task_version_id, row.sandbox_image_ref
    # This release supports one explicitly published sample, never guesses a baseline.
    if task != "python-debug-01:f898a77":
        raise HTTPException(409, "该题目版本尚未配置改动基线及评分规则")
    try:
        baseline = baseline_files()
    except OSError as error:
        raise HTTPException(503, "题目基线暂不可用，无法评测") from error
    fingerprint = hashlib.sha256(
        json.dumps(baseline, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return detail, baseline, task, image, fingerprint


def report_root(reader):
    root = reader.store.root / "evaluations"
    root.mkdir(parents=True, exist_ok=True)
    return root


def latest_report(reader, detail, fingerprint):
    paths = sorted(
        report_root(reader).glob(detail["submission_id"] + "-*.json"), reverse=True
    )
    values = []
    try:
        for path in paths[:100]:
            value = json.loads(path.read_text(encoding="utf-8"))
            if (
                value["snapshot_id"] != detail["snapshot_id"]
                or value["submission_id"] != detail["submission_id"]
                or value["rule_version"] != RULE
                or value["baseline_sha256"] != fingerprint
            ):
                continue
            validate_result(value)
            values.append(value)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(
            503, "评测记录读取失败，请联系维护者；不会用缺失记录计零分"
        ) from error
    last = values[0] if values else None
    return next((v for v in values if v["status"] == "completed"), last), last


def validate_result(value):
    if value["status"] == "completed":
        tests = value["tests"]
        passed = sum(item["passed"] is True for item in tests)
        if (
            len(tests) != 8
            or len({item["name"] for item in tests}) != 8
            or value["total"] != 8
            or value["passed"] != passed
            or value["score"] != passed * 7.5
            or any(
                item["points"] != (7.5 if item["passed"] is True else 0)
                for item in tests
            )
        ):
            raise ValueError("invalid objective result")
    elif value["status"] != "infrastructure_failed" or value["score"] is not None:
        raise ValueError("invalid failure result")


@router.get("/submissions/{submission_id}/evaluation")
def get_evaluation(reader: Reader, submission_id: str):
    detail, baseline, task, image, fingerprint = context(reader, submission_id)
    try:
        report, last_attempt = latest_report(reader, detail, fingerprint)
    except OSError as error:
        raise HTTPException(503, "评测存储暂不可用，请稍后重试") from error
    return {
        "changes": changes(detail["files"], baseline),
        "result_max": 60,
        "process_max": 40,
        "process_score": None,
        "total_score": None,
        "task_version": task,
        "rule_version": RULE,
        "report": report,
        "last_attempt": last_attempt,
    }


@router.post(
    "/submissions/{submission_id}/evaluation",
    dependencies=[Depends(require_browser_header)],
)
def evaluate(reader: Reader, request: Request, submission_id: str):
    detail, baseline, task, image, fingerprint = context(reader, submission_id)
    runtime = request.app.state.submission_sandbox
    if runtime is None or not hasattr(runtime, "settings"):
        raise HTTPException(503, "平台评测环境未配置，结果分未评")
    if not GATE.acquire(blocking=False):
        raise HTTPException(409, "已有评测正在执行，请稍后重试")
    try:
        from app.grading.runner import run_isolated

        report = run_isolated(detail["files"], baseline, image, runtime.settings[0])
        validate_result(report)
        report.update(
            run_id="grade_" + uuid.uuid4().hex,
            submission_id=submission_id,
            snapshot_id=detail["snapshot_id"],
            task_version=task,
            image_ref=image,
            rule_version=RULE,
            baseline_sha256=fingerprint,
            evaluated_at=int(time.time()),
        )
        root = report_root(reader)
        target = root / f"{submission_id}-{time.time_ns()}-{report['run_id']}.json"
        descriptor, temporary = tempfile.mkstemp(dir=root, prefix=".pending-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(report, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return report
    except OSError as error:
        raise HTTPException(503, "评测结果未能保存，请重试；本次不计分") from error
    finally:
        GATE.release()

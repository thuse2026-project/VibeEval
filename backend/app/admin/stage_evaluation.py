"""Examiner-only results for immutable multi-stage code submissions."""

import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.admin.evaluation import changes
from app.admin.router import current_session
from app.core.security import require_browser_header
from app.db.models import ExamFlowAttempt, ExamSession, Invitation, StageWorkspace
from app.grading.candidate_tests import compare_tests
from app.grading.datasette import evaluate, load_rule
from app.grading.rubrics import POLICIES

router = APIRouter(prefix="/api/v1/admin/r2", dependencies=[Depends(current_session)])
GRADE_GATE = threading.Lock()
REVIEW_GATE = threading.Lock()


def context(request, submission_id, *, answer_sheet_only=False):
    if not re.fullmatch(r"submission_[a-f0-9]{32}", submission_id):
        raise HTTPException(404, "未找到多阶段答卷")
    flow = request.app.state.exam_flow
    resources = request.app.state.stage_resources
    if flow is None or resources is None:
        raise HTTPException(503, "多阶段答卷服务尚未配置")
    try:
        with resources.factory() as db:
            row = db.execute(
                select(ExamFlowAttempt, Invitation)
                .join(ExamSession, ExamSession.session_id == ExamFlowAttempt.session_id)
                .join(Invitation, Invitation.token_hash == ExamSession.invitation_hash)
                .where(
                    ExamFlowAttempt.state_json["stages"].contains(
                        [{"receipt": {"id": submission_id}}]
                    )
                )
            ).one_or_none()
            if row is None:
                raise HTTPException(404, "未找到已交卷答卷")
            attempt, invitation = row
            stage = next(
                s
                for s in attempt.state_json["stages"]
                if (s.get("receipt") or {}).get("id") == submission_id
            )
            workspace = db.scalar(
                select(StageWorkspace).where(StageWorkspace.stage_id == stage["id"])
            )
            if workspace is None or workspace.status != "sealed":
                raise HTTPException(409, "答卷尚未完成封存，请稍后刷新")
            metadata = {
                "submission_id": submission_id,
                "session_id": attempt.session_id,
                "stage_id": stage["id"],
                "definition": attempt.state_json["definition"]["key"],
                "kind": stage["receipt"]["kind"],
                "title": stage["definition"]["title"],
                "candidate_id": invitation.candidate_id,
                "candidate_name": invitation.candidate_name,
                "exam_id": invitation.exam_id,
                "exam_title": invitation.exam_title,
                "task_version": workspace.spec_json["task_version"],
                "image": workspace.sandbox_image_ref,
            }
        if answer_sheet_only:
            exported = flow.export_submission(
                metadata["session_id"], metadata["stage_id"]
            )
        else:
            if metadata["kind"] != "code":
                raise HTTPException(409, "文字答卷请从答卷读取接口查看")
            from examples.datasette_exam import export_code_submission

            exported = export_code_submission(
                flow, metadata["session_id"], metadata["stage_id"]
            )
        return metadata, exported, resources
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "固定答卷暂时无法读取，不会以缺失记录计零分") from None


def rule_for(metadata):
    root = os.environ.get("VIBEEVAL_DATASETTE_GRADING_BUNDLE", "")
    if not root:
        raise HTTPException(503, "尚未配置可信评测题包；客观分未评")
    try:
        return load_rule(root, metadata["task_version"], metadata["image"])
    except (OSError, ValueError, KeyError, TypeError):
        raise HTTPException(503, "题目版本或可信评测材料不匹配；客观分未评") from None


def location(resources, metadata, rule):
    root = (
        resources.snapshots.root
        / "r2-evaluations"
        / metadata["submission_id"]
        / rule["fingerprint"]
    )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def read_record(path):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, ValueError):
        raise HTTPException(503, "评分记录暂不可读；请勿用缺失记录计零分") from None


def atomic_record(path, value):
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def total_score(objective, review):
    if not objective or objective["status"] != "completed" or review is None:
        return None
    raw = objective["score"] + sum(review["scores"].values())
    return round(
        min(raw, objective["cap"]) if objective.get("cap") is not None else raw, 2
    )


def records(root, exported, metadata, rule):
    objective = read_record(root / "objective.json")
    review = read_record(root / "review.json")
    expected = exported["content"]["artifact"]["sha256"]
    try:
        for value in (objective, review):
            if value is not None and (
                value["snapshot_sha256"] != expected
                or value["rule_sha256"] != rule["fingerprint"]
            ):
                raise ValueError
        if objective is not None:
            groups = {c["key"]: c for c in objective["groups"]}
            if (
                objective["status"] != "completed"
                or objective["submission_id"] != metadata["submission_id"]
                or set(groups) != {c["key"] for c in rule["criteria"]}
                or objective["score"] != sum(c["score"] for c in groups.values())
            ):
                raise ValueError
            for c in rule["criteria"]:
                actual = groups[c["key"]]
                if (
                    type(actual["passed"]) is not bool
                    or actual["score"] != (c["max"] if actual["passed"] else 0)
                    or actual["max"] != c["max"]
                ):
                    raise ValueError
            expected_cap = (
                rule.get("failure_cap")
                if any(
                    c.get("core") and not groups[c["key"]]["passed"]
                    for c in rule["criteria"]
                )
                else None
            )
            if objective.get("cap") != expected_cap:
                raise ValueError
        if review is not None:
            maxima = {c["key"]: c["max"] for c in rule["manual"]}
            if (
                type(review["revision"]) is not int
                or review["revision"] < 1
                or set(review["scores"]) != set(maxima)
                or not review["note"].strip()
            ):
                raise ValueError
            if any(
                type(v) not in (int, float)
                or not math.isfinite(v)
                or not 0 <= v <= maxima[k]
                for k, v in review["scores"].items()
            ):
                raise ValueError
            if rule.get("review_evidence_required") and (
                set(review.get("evidence", {})) != set(maxima)
                or any(
                    not isinstance(v, str) or not v.strip()
                    for v in review["evidence"].values()
                )
                or (
                    any(
                        review["scores"][c["key"]] > 0
                        for c in rule["manual"]
                        if c.get("category") == "tests"
                    )
                    and review.get("novel_tests_confirmed") is not True
                )
            ):
                raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(
            503, "评分记录与固定答卷或规则不一致，请维护者核查；本次不发布分数"
        ) from None
    return objective, review


def observation_record(path, exported, rule):
    value = read_record(path)
    if value is not None:
        source = next(
            (
                f["content"]
                for f in exported["files"]
                if f["path"] == "tests/test_candidate.py"
            ),
            "",
        )
        if (
            value.get("snapshot_sha256") != exported["content"]["artifact"]["sha256"]
            or value.get("rule_sha256") != rule["fingerprint"]
            or value.get("test_sha256") != hashlib.sha256(source.encode()).hexdigest()
        ):
            raise HTTPException(
                503, "新增测试证据与固定答卷或规则不一致，请核查后重跑。"
            )
    return value


def prior_scores(root, exported):
    """Old rules remain read-only; never reinterpret their manual totals."""
    result = []
    for folder in sorted(
        root.parent.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True
    )[:20]:
        if folder == root or not folder.is_dir():
            continue
        objective = read_record(folder / "objective.json")
        review = read_record(folder / "review.json")
        if any(
            value
            and value.get("snapshot_sha256")
            != exported["content"]["artifact"]["sha256"]
            for value in (objective, review)
        ):
            continue
        if objective or review:
            result.append(
                {"rule_sha256": folder.name, "objective": objective, "review": review}
            )
    return result


@router.get("/submissions")
def submissions(
    request: Request,
    page: int = Query(default=1, ge=1),
    exam_id: str = "",
    kind: Literal["all", "text", "code"] = "code",
):
    resources = request.app.state.stage_resources
    if resources is None:
        raise HTTPException(503, "多阶段答卷服务尚未配置")
    try:
        with resources.factory() as db:
            query = (
                select(ExamFlowAttempt, Invitation)
                .join(ExamSession, ExamSession.session_id == ExamFlowAttempt.session_id)
                .join(Invitation, Invitation.token_hash == ExamSession.invitation_hash)
            )
            if exam_id:
                query = query.where(Invitation.exam_id == exam_id)
            # ponytail: bounded course roster; move pagination into JSONB SQL for >1000 attempts.
            rows = db.execute(
                query.order_by(ExamSession.admitted_at.desc()).limit(1000)
            ).all()
            items = []
            for attempt, invitation in rows:
                for stage in attempt.state_json["stages"]:
                    receipt = stage.get("receipt")
                    if receipt and (kind == "all" or receipt["kind"] == kind):
                        items.append(
                            {
                                "submission_id": receipt["id"],
                                "kind": receipt["kind"],
                                "submitted_at": receipt["submitted_at"],
                                "close_reason": receipt["close_reason"],
                                "title": stage["definition"]["title"],
                                "candidate_id": invitation.candidate_id,
                                "candidate_name": invitation.candidate_name,
                                "exam_id": invitation.exam_id,
                                "exam_title": invitation.exam_title,
                            }
                        )
            items.sort(
                key=lambda item: (item["submitted_at"], item["submission_id"]),
                reverse=True,
            )
            return {
                "items": items[(page - 1) * 20 : page * 20],
                "total": len(items),
                "page": page,
                "page_size": 20,
            }
    except Exception:
        raise HTTPException(503, "多阶段答卷列表暂不可读") from None


@router.get("/submissions/{submission_id}/answer-sheet")
def answer_sheet(request: Request, submission_id: str):
    metadata, exported, _ = context(request, submission_id, answer_sheet_only=True)
    return {"metadata": metadata, **exported}


@router.get("/submissions/{submission_id}")
def detail(request: Request, submission_id: str):
    metadata, exported, resources = context(request, submission_id)
    rule = rule_for(metadata)
    root = location(resources, metadata, rule)
    objective, review = records(root, exported, metadata, rule)
    return {
        **metadata,
        "receipt": exported["receipt"],
        "summary": exported["content"].get("summary"),
        "files": exported["files"],
        "changes": changes(exported["files"], rule["baseline"]),
        "objective_max": 70,
        "manual_max": 30,
        "criteria": [
            {k: c[k] for k in ("key", "name", "max")} for c in rule["criteria"]
        ],
        "manual_criteria": rule["manual"],
        "objective": objective,
        "review": review,
        "total_score": total_score(objective, review),
        "rule_version": rule["rule_version"],
        "last_attempt": read_record(root / "last-attempt.json"),
        "review_evidence_required": rule.get("review_evidence_required", False),
        "policies": POLICIES,
        "candidate_tests": observation_record(
            root / "candidate-tests.json", exported, rule
        ),
        "candidate_tests_last_attempt": observation_record(
            root / "candidate-tests-last-attempt.json", exported, rule
        ),
        "previous_scores": prior_scores(root, exported),
    }


@router.post(
    "/submissions/{submission_id}/evaluate",
    dependencies=[Depends(require_browser_header)],
)
def grade(request: Request, submission_id: str):
    metadata, exported, resources = context(request, submission_id)
    rule = rule_for(metadata)
    root = location(resources, metadata, rule)
    if not GRADE_GATE.acquire(blocking=False):
        raise HTTPException(409, "已有客观评测进行中，请稍后读取结果")
    try:
        report = evaluate(
            exported["files"],
            rule,
            metadata["image"],
            os.environ.get("SANDBOX_DOCKER_URL", "unix:///var/run/docker.sock"),
        )
        report.update(
            run_id=uuid.uuid4().hex,
            evaluated_at=int(time.time()),
            submission_id=submission_id,
            snapshot_sha256=exported["content"]["artifact"]["sha256"],
            rule_version=rule["rule_version"],
            rule_sha256=rule["fingerprint"],
        )
        atomic_record(root / "last-attempt.json", report)
        if report["status"] == "completed":
            atomic_record(root / "objective.json", report)
        return report
    except OSError:
        raise HTTPException(503, "评测记录未能保存，本次不发布分数") from None
    finally:
        GRADE_GATE.release()


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    expected_revision: int = Field(ge=0)
    scores: dict[str, float]
    note: str = Field(min_length=1, max_length=4000)
    evidence: dict[str, str] = Field(default_factory=dict)
    novel_tests_confirmed: bool = False


@router.post(
    "/submissions/{submission_id}/review",
    dependencies=[Depends(require_browser_header)],
)
def save_review(
    request: Request,
    submission_id: str,
    body: Review,
    examiner=Depends(current_session),
):
    metadata, exported, resources = context(request, submission_id)
    if metadata.get("definition") == "r2-four-stage":
        raise HTTPException(409, "四题考试须按独立问答及过程分项评分")
    rule = rule_for(metadata)
    maxima = {item["key"]: item["max"] for item in rule["manual"]}
    if (
        set(body.scores) != set(maxima)
        or not body.note.strip()
        or any(
            not math.isfinite(score) or not 0 <= score <= maxima[key]
            for key, score in body.scores.items()
        )
    ):
        raise HTTPException(422, "请填写每项人工分数及评价依据，分数不得超出对应上限")
    if rule.get("review_evidence_required") and (
        set(body.evidence) != set(maxima)
        or any(not text.strip() or len(text) > 2000 for text in body.evidence.values())
        or (
            any(
                body.scores[c["key"]] > 0
                for c in rule["manual"]
                if c.get("category") == "tests"
            )
            and not body.novel_tests_confirmed
        )
    ):
        raise HTTPException(
            422, "请逐项填写代码/测试依据；测试项非零时须确认存在非复制的新增验证。"
        )
    root = location(resources, metadata, rule)
    with REVIEW_GATE:
        _, prior = records(root, exported, metadata, rule)
        revision = prior["revision"] if prior else 0
        if revision != body.expected_revision:
            raise HTTPException(409, "评分已被更新，请刷新后核对再保存")
        value = {
            "revision": revision + 1,
            "scores": body.scores,
            "note": body.note.strip(),
            "evidence": {k: v.strip() for k, v in body.evidence.items()},
            "novel_tests_confirmed": body.novel_tests_confirmed,
            "reviewer": examiner.username,
            "reviewed_at": int(time.time()),
            "snapshot_sha256": exported["content"]["artifact"]["sha256"],
            "rule_sha256": rule["fingerprint"],
        }
        try:
            atomic_record(root / "review.json", value)
        except OSError:
            raise HTTPException(503, "人工评分未能保存，请重新读取核对") from None
        return value


@router.post(
    "/submissions/{submission_id}/candidate-tests",
    dependencies=[Depends(require_browser_header)],
)
def inspect_candidate_tests(request: Request, submission_id: str):
    metadata, exported, resources = context(request, submission_id)
    rule = rule_for(metadata)
    root = location(resources, metadata, rule)
    if not GRADE_GATE.acquire(blocking=False):
        raise HTTPException(409, "已有评测或测试对照运行中，请稍后读取结果")
    try:
        report = compare_tests(
            exported["files"],
            rule,
            metadata["image"],
            os.environ.get("SANDBOX_DOCKER_URL", "unix:///var/run/docker.sock"),
        )
        report.update(
            submission_id=submission_id,
            snapshot_sha256=exported["content"]["artifact"]["sha256"],
            rule_sha256=rule["fingerprint"],
            observed_at=int(time.time()),
        )
        atomic_record(root / "candidate-tests-last-attempt.json", report)
        if report["status"] == "completed":
            atomic_record(root / "candidate-tests.json", report)
        return report
    except (OSError, ValueError, KeyError):
        raise HTTPException(
            503, "新增测试对照未能完成或保存，不计零分；既有评分保持不变。"
        ) from None
    finally:
        GRADE_GATE.release()

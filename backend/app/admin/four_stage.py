"""Examiner-only four-stage marking and atomic publication of the four results."""

import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.admin.router import current_session
from app.admin.stage_evaluation import context
from app.core.security import require_browser_header
from app.db.models import ExamFlowAttempt, ExamGradePublication, StageGradingJob
from app.exam_flow.types import EvidenceCutoff, digest
from app.grading import stage_jobs
from app.grading.four_rules import material
from app.grading.four_worker import propose
from app.stage_resources.evidence import page

router = APIRouter(
    prefix="/api/v1/admin/r2/submissions/{sid}/four-stage",
    dependencies=[Depends(current_session), Depends(require_browser_header)],
)
STAGES = [
    "tinydb-understanding",
    "tinydb-review",
    "column-statistics",
    "facet-pagination",
]


def load(request, sid):
    metadata, exported, resources = context(request, sid, allow_text=True)
    if metadata["definition"] != "r2-four-stage":
        raise HTTPException(409, "此入口仅适用于四题考试")
    job = stage_jobs.create_job(metadata, exported, resources)
    questions, answers, rubric, interview = material(
        exported["content"], metadata["stage_key"]
    )
    return metadata, exported, resources, job, questions, answers, rubric, interview


def revision(row, rubric):
    return digest({"basis": row.basis, "components": row.components, "rubric": rubric})


def exam_state(db, session_id):
    attempt = db.scalar(
        select(ExamFlowAttempt).where(ExamFlowAttempt.session_id == session_id)
    )
    stages = attempt.state_json["stages"]
    rows = []
    for stage in stages:
        sid = (stage.get("receipt") or {}).get("id")
        job = db.get(StageGradingJob, sid) if sid else None
        review = job.components.get("examiner_review") if job else None
        rows.append(
            dict(
                title=stage["definition"]["title"],
                submission_id=sid,
                score=review["final"] if review else None,
                review_hash=digest(review) if review else None,
            )
        )
    ready = (
        attempt.state_json["status"] == "completed"
        and [s["definition"]["key"] for s in stages] == STAGES
        and all(r["score"] is not None for r in rows)
    )
    published = db.get(ExamGradePublication, session_id)
    return dict(
        stages=rows,
        ready=ready,
        revision=digest(rows),
        publication=published.record if published else None,
    )


@router.post("/materials")
def materials(request: Request, sid: str):
    meta, exported, resources, _, questions, answers, rubric, _ = load(request, sid)
    with resources.factory() as db:
        job = db.get(StageGradingJob, sid)
        return dict(
            metadata=meta,
            questions=questions,
            answers=answers,
            rubric=rubric,
            files=exported.get("files", []),
            basis=job.basis,
            components=job.components,
            revision=revision(job, rubric),
            exam=exam_state(db, meta["session_id"]),
        )


@router.get("/evidence")
def events(request: Request, sid: str, after: int = Query(0, ge=0)):
    _, exported, resources = context(request, sid, answer_sheet_only=True)
    cutoff = (exported["content"].get("artifact") or {}).get("evidence")
    if not cutoff:
        raise HTTPException(404, "本题没有冻结编程记录")
    cutoff = EvidenceCutoff.model_validate(cutoff)
    if after > cutoff.sequence:
        raise HTTPException(422, "操作记录页码超出截止点")
    with resources.factory() as db:
        items, cursor = page(db, cutoff, after=after)
        return dict(
            items=items,
            next_cursor=cursor,
            cutoff=cutoff,
            sha256=exported["content"]["artifact"]["sha256"],
        )


class Review(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, allow_inf_nan=False, str_strip_whitespace=True
    )
    revision: str = Field(pattern="^[0-9a-f]{64}$")
    scores: dict[str, float]
    reason: str = Field(min_length=1, max_length=4000)
    gap_reviewed: bool = False


@router.post("/review")
def review(request: Request, sid: str, body: Review, examiner=Depends(current_session)):
    meta, _, resources, _, _, answers, rubric, _ = load(request, sid)
    with resources.factory.begin() as db:
        row = db.get(StageGradingJob, sid, with_for_update=True)
        if db.get(ExamGradePublication, meta["session_id"]):
            raise HTTPException(409, "这场成绩已发布")
        if revision(row, rubric) != body.revision:
            raise HTTPException(409, "分项或依据已更新，请刷新后核对")
        if set(body.scores) != {c["key"] for c in rubric} or any(
            not 0 <= body.scores[c["key"]] <= c["maximum"] for c in rubric
        ):
            raise HTTPException(422, "请填写全部分项，并检查各项上限")
        for c in rubric:
            answer = answers.get(c["answer_id"])
            if (
                c["group"] == "answers"
                and body.scores[c["key"]]
                and (
                    not answer
                    or isinstance(answer, str)
                    and not answer.strip()
                    or isinstance(answer, dict)
                    and not answer.get("reports")
                )
            ):
                raise HTTPException(422, "封存答案为空的分项只能记零分")
        gap = row.basis.get("evidence", {}).get("completeness") == "partial"
        if gap and not body.gap_reviewed:
            raise HTTPException(422, "请核对采集缺口并说明采用了哪些可验证材料")
        objective = row.components.get("objective")
        if objective is not None and (
            objective.get("status") != "completed" or objective.get("score") is None
        ):
            raise HTTPException(409, "可信客观评测尚未完成")
        raw = round(
            sum(body.scores.values()) + (objective["score"] if objective else 0), 2
        )
        cap = objective.get("cap") if objective else None
        result = dict(
            scores=body.scores,
            reason=body.reason,
            examiner=examiner.username,
            at=int(time.time()),
            gap_reviewed=body.gap_reviewed,
            basis=digest(row.basis),
            rubric=digest(rubric),
            objective=objective["score"] if objective else None,
            raw=raw,
            cap=cap,
            final=min(raw, cap) if cap is not None else raw,
        )
        previous = row.components.get("examiner_review")
        history = row.components.get("review_history", [])
        row.components = {
            **row.components,
            "examiner_review": result,
            "review_history": [*history, previous] if previous else history,
        }
        return result


class Confirm(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision: str = Field(pattern="^[0-9a-f]{64}$")
    confirm: Literal[True]


@router.post("/publish")
def publish(
    request: Request, sid: str, body: Confirm, examiner=Depends(current_session)
):
    meta, _, resources = context(request, sid, answer_sheet_only=True)
    with resources.factory.begin() as db:
        attempt = db.scalar(
            select(ExamFlowAttempt)
            .where(ExamFlowAttempt.session_id == meta["session_id"])
            .with_for_update()
        )
        if attempt.state_json["definition"]["key"] != "r2-four-stage":
            raise HTTPException(409, "此入口仅适用于四题考试")
        ids = [(s.get("receipt") or {}).get("id") for s in attempt.state_json["stages"]]
        db.scalars(
            select(StageGradingJob)
            .where(StageGradingJob.submission_id.in_(ids))
            .order_by(StageGradingJob.submission_id)
            .with_for_update()
        ).all()
        state = exam_state(db, meta["session_id"])
        if not state["ready"] or body.revision != state["revision"]:
            raise HTTPException(409, "需要四题均已评定，且确认的四份成绩仍为当前版本")
        if state["publication"]:
            return state["publication"]
        record = dict(
            revision=state["revision"],
            stages=state["stages"],
            examiner=examiner.username,
            published_at=int(time.time()),
        )
        db.add(ExamGradePublication(session_id=meta["session_id"], record=record))
        return record


@router.post("/suggest")
def suggest(request: Request, sid: str):
    return propose(load(request, sid))

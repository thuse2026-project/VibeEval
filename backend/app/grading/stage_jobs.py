"""Pin the grading inputs once for each sealed four-stage submission."""

import hashlib
import time
from collections import Counter

from fastapi import HTTPException
from sqlalchemy.dialects.postgresql import insert

from app.db.models import StageGradingJob
from app.exam_flow.types import EvidenceCutoff, FlowError, digest
from app.stage_resources.evidence import page


def evidence_basis(db, content, event_ids=None):
    artifact = content["artifact"]
    cutoff = EvidenceCutoff.model_validate(artifact["evidence"])
    if not any(
        phase["id"] == cutoff.coding_phase_id and phase["definition"]["key"] == "coding"
        for phase in content["phases"]
    ):
        raise ValueError("evidence cutoff belongs to a different coding phase")
    counts = Counter()
    fingerprint = hashlib.sha256()
    after = 0
    while True:
        items, after = page(db, cutoff, after=after, limit=100)
        for item in items:
            if event_ids is not None:
                event_ids.add(item["id"])
            counts[item["kind"]] += 1
            fingerprint.update(bytes.fromhex(digest(item)))
        if after is None:
            break
    return {
        **cutoff.model_dump(),
        "event_counts": dict(counts),
        "events_sha256": fingerprint.hexdigest(),
    }


def view(row):
    return {
        "submission_id": row.submission_id,
        "created_at": row.created_at,
        "basis": row.basis,
        "components": row.components,
    }


def create_job(metadata, exported, resources, *, event_ids=None):
    receipt, content = exported["receipt"], exported["content"]
    if content["definition"] != "r2-four-stage":
        raise HTTPException(409, "评分任务仅支持四题版答卷")
    if (
        receipt["id"] != metadata["submission_id"]
        or content["stage_id"] != metadata["stage_id"]
        or digest(content) != receipt["content_sha256"]
    ):
        raise HTTPException(409, "答卷内容与封存回执不一致")
    basis = {
        "definition_version": content["definition_version"],
        "task_version": content["task_version"],
        "stage_id": content["stage_id"],
        "content_sha256": receipt["content_sha256"],
        "answers": [
            {
                "phase_id": p["id"],
                "question_id": q["id"],
                "revision": p["answers"].get(q["id"], {}).get("revision"),
            }
            for p in content["phases"]
            if "edit_answers" in p["definition"]["capabilities"]
            for q in (p["questions"] or [])
        ],
    }
    components = {"answers": {"status": "pending", "score": None}}
    with resources.factory.begin() as db:
        if receipt["kind"] == "code":
            try:
                basis["code_sha256"] = content["artifact"]["sha256"]
                evidence = evidence_basis(db, content, event_ids)
            except (KeyError, TypeError, ValueError, FlowError) as error:
                raise HTTPException(503, "封存证据暂不可读，请恢复后重试") from error
            basis["evidence"] = evidence
            components["objective"] = {"status": "pending", "score": None}
            components["process"] = {
                "status": (
                    "needs_review"
                    if evidence["completeness"] == "partial"
                    else "pending"
                ),
                "reason": evidence["reason"],
                "score": None,
            }
        sid = receipt["id"]
        db.execute(
            insert(StageGradingJob)
            .values(
                submission_id=sid,
                created_at=int(time.time()),
                basis=basis,
                components=components,
            )
            .on_conflict_do_nothing(index_elements=["submission_id"])
        )
        row = db.get(StageGradingJob, sid)
        if row.basis != basis:
            raise HTTPException(409, "已建任务的评分依据与本次读取不一致")
        return view(row)

"""Read-only examiner overview; submission service plugs into read_live_records."""

import time
from contextlib import closing
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request

from ..exams.store import EntryStore, RECOVERY_SECONDS
from .demo import demo_records
from .router import current_session
from .submissions import Reader, SubmissionReader, list_committed

router = APIRouter(prefix="/api/v1/admin", tags=["考务总览"],
                   dependencies=[Depends(current_session)])
Status = Literal["all", "invited", "admitted", "in_progress", "submitted",
                 "invitation_expired", "recovery_expired"]


def read_live_records(store: EntryStore, reader: SubmissionReader | None = None) -> list[dict]:
    """Merge admission rows with the shared read-only SubmissionReader contract.

    The persistence owner supplies get_submission_reader; no submission schema,
    freeze operation, or active-workspace read belongs in this module.
    """
    now = time.time()
    with closing(store.connect()) as connection:
        rows = connection.execute(
            "SELECT i.candidate_id, i.candidate_name, i.exam_id, i.exam_title, "
            "i.expires_at, s.session_id, s.admitted_at FROM invitations i "
            "LEFT JOIN entry_sessions s ON s.invitation_hash = i.token_hash"
        ).fetchall()
    records = []
    for row in rows:
        if row["session_id"]:
            status = "recovery_expired" if row["admitted_at"] + RECOVERY_SECONDS <= now else "admitted"
        else:
            status = "invitation_expired" if row["expires_at"] <= now else "invited"
        records.append({
            "candidate_id": row["candidate_id"], "candidate_name": row["candidate_name"],
            "exam_id": row["exam_id"], "exam_title": row["exam_title"], "status": status,
            "session_id": row["session_id"], "admitted_at": row["admitted_at"],
            "started_at": None, "submission_id": None, "submitted_at": None,
        })
    if reader is not None:
        by_candidate = {(row["exam_id"], row["candidate_id"]): row for row in records}
        for committed in list_committed(reader):
            key = (committed.exam_id, committed.candidate_id)
            existing = by_candidate.get(key, {})
            by_candidate[key] = {
                "admitted_at": None, "started_at": None, **existing,
                **committed.model_dump(exclude_none=True), "status": "submitted",
            }
        records = list(by_candidate.values())
    return records


@router.get("/overview")
def overview(request: Request, reader: Reader, source: Literal["live", "demo"] = "live",
             exam_id: str | None = Query(default=None, max_length=120), status: Status = "all",
             page: int = Query(default=1, ge=1), page_size: int = Query(default=10, ge=1, le=100)):
    # ponytail: course-scale pagination; move filtering/counts into PostgreSQL on integration.
    records = read_live_records(request.app.state.entry, reader) if source == "live" else demo_records()
    exams = {row["exam_id"]: row["exam_title"] for row in records}
    scoped = [row for row in records if exam_id is None or row["exam_id"] == exam_id]
    submitted = sum(row["status"] == "submitted" for row in scoped)
    summary = {"total": len(scoped), "not_submitted": len(scoped) - submitted, "submitted": submitted}
    filtered = [row for row in scoped if status == "all" or row["status"] == status]
    filtered.sort(key=lambda row: (
        -(row["submitted_at"] or row["admitted_at"] or 0), row["exam_id"], row["candidate_id"]
    ))
    page = min(page, max(1, (len(filtered) + page_size - 1) // page_size))
    offset = (page - 1) * page_size
    return {
        "source": source, "items": filtered[offset:offset + page_size], "total": len(filtered),
        "page": page, "page_size": page_size, "summary": summary,
        "exams": [{"id": key, "title": exams[key]} for key in sorted(exams)],
        "updated_at": int(time.time()),
        "submission_service_connected": reader is not None,
    }

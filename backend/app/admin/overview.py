"""Read-only examiner overview; submission service plugs into read_live_records."""

import time
from contextlib import closing
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request

from ..exams.store import EntryStore, RECOVERY_SECONDS
from .router import current_session

router = APIRouter(prefix="/api/v1/admin", tags=["考务总览"],
                   dependencies=[Depends(current_session)])
Status = Literal["all", "invited", "admitted", "in_progress", "submitted",
                 "invitation_expired", "recovery_expired"]


def read_live_records(store: EntryStore) -> list[dict]:
    """Integration point: enrich these rows from immutable submissions, not workspace state.

    A persistence implementation must return one row per (exam_id, candidate_id),
    and may add submission-only rows. Submitted rows need a real submission_id
    and submitted_at; started rows need a real started_at. Never infer started
    or submitted from admission. No secret/token/hash fields may leave this function.
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
    return records


def demo_records() -> list[dict]:
    """Explicit sample data, never persisted or mixed into live records."""
    now = int(time.time())
    records = []
    for index in range(12):
        status = ("invited", "admitted", "in_progress", "submitted")[index % 4]
        exam = "A" if index < 6 else "B"
        records.append({
            "candidate_id": f"demo-{index + 1:03d}", "candidate_name": f"联调考生 {index + 1:02d}",
            "exam_id": f"demo-exam-{exam}", "exam_title": f"联调场次 {exam}", "status": status,
            "session_id": None if status == "invited" else f"demo-session-{index + 1}",
            "admitted_at": None if status == "invited" else now - 7200 - index * 60,
            "started_at": now - 6000 - index * 60 if status in ("in_progress", "submitted") else None,
            "submission_id": f"demo-submission-{index + 1}" if status == "submitted" else None,
            "submitted_at": now - 600 - index * 60 if status == "submitted" else None,
        })
    return records


@router.get("/overview")
def overview(request: Request, source: Literal["live", "demo"] = "live",
             exam_id: str | None = Query(default=None, max_length=120), status: Status = "all",
             page: int = Query(default=1, ge=1), page_size: int = Query(default=10, ge=1, le=100)):
    # ponytail: course-scale pagination; move filtering/counts into PostgreSQL on integration.
    records = read_live_records(request.app.state.entry) if source == "live" else demo_records()
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
        "submission_service_connected": False,
    }

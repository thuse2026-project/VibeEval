"""Trusted recorder handoff. Writers use the stage lock before appending facts."""

from typing import Literal

from pydantic import Field, JsonValue
from sqlalchemy import func, select

from app.db.models import StageEvidence
from app.exam_flow.types import Contract, FlowError, Identifier


class EvidenceItem(Contract):
    id: Identifier
    kind: Literal["message", "approval", "edit", "test_run", "terminal", "command"]
    actor: Literal["candidate", "agent", "system"]
    at: int = Field(strict=True, ge=0)
    data: dict[str, JsonValue]


def head(db, phase_id):
    return (
        db.scalar(
            select(func.max(StageEvidence.sequence)).where(
                StageEvidence.phase_id == phase_id
            )
        )
        or 0
    )


def append(db, phase_id, item):
    item = EvidenceItem.model_validate(item)
    payload = item.model_dump(mode="json")
    previous = db.scalar(
        select(StageEvidence).where(
            StageEvidence.phase_id == phase_id, StageEvidence.event_id == item.id
        )
    )
    if previous is not None:
        if previous.payload != payload:
            raise FlowError("EVIDENCE_ID_CONFLICT")
        return previous.sequence
    sequence = head(db, phase_id) + 1
    db.add(
        StageEvidence(
            phase_id=phase_id, sequence=sequence, event_id=item.id, payload=payload
        )
    )
    return sequence


def page(db, cutoff, *, after=0, limit=50):
    if (
        type(limit) is not int
        or not 1 <= limit <= 100
        or not 0 <= after <= cutoff.sequence
    ):
        raise FlowError("EVIDENCE_CURSOR_INVALID", 422)
    rows = db.scalars(
        select(StageEvidence)
        .where(
            StageEvidence.phase_id == cutoff.coding_phase_id,
            StageEvidence.sequence > after,
            StageEvidence.sequence <= cutoff.sequence,
        )
        .order_by(StageEvidence.sequence)
        .limit(limit)
    ).all()
    if len(rows) != min(limit, cutoff.sequence - after) or any(
        row.sequence != after + i for i, row in enumerate(rows, 1)
    ):
        raise FlowError("EVIDENCE_UNAVAILABLE", 503)
    return [row.payload for row in rows], (
        rows[-1].sequence if after + len(rows) < cutoff.sequence else None
    )

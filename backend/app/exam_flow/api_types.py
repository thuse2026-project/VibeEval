"""Candidate response contracts, deliberately excluding private flow state."""

from typing import Literal

from pydantic import JsonValue

from .types import Capability, Contract, Identifier, PublicQuestion, Receipt

StageStatus = Literal["locked", "preparing", "ready", "active", "sealing", "submitted"]
PhaseStatus = Literal["pending", "active", "closing", "closed"]


class StageProgress(Contract):
    id: Identifier
    ordinal: int
    title: str
    status: StageStatus
    receipt: Receipt | None


class CurrentStage(Contract):
    id: Identifier
    key: Identifier
    ordinal: int
    title: str
    status: StageStatus
    workspace_id: Identifier


class CurrentPhase(Contract):
    id: Identifier
    key: Identifier
    title: str
    status: PhaseStatus
    permission_epoch: int
    started_at: int | None
    deadline_at: int | None


class AttemptView(Contract):
    attempt_id: Identifier
    status: Literal["admitted", "preparing", "ready", "running", "completed"]
    state_version: int
    server_now: int
    transition_deadline_at: int | None
    error_code: str | None
    failure_count: int
    retry_at: int | None
    current_stage: CurrentStage
    current_phase: CurrentPhase
    capabilities: list[Capability]
    stages: list[StageProgress]


class QuestionList(Contract):
    items: list[PublicQuestion]


class ExamEvent(Contract):
    sequence: int
    kind: Literal[
        "stage.preparing",
        "stage.ready",
        "phase.started",
        "phase.closing",
        "phase.closed",
        "stage.submitted",
        "exam.completed",
    ]
    at: int
    stage_id: Identifier
    phase_id: Identifier


class EventPage(Contract):
    items: list[ExamEvent]
    head_sequence: int


class ErrorDetail(Contract):
    code: str


class ErrorResponse(Contract):
    detail: ErrorDetail | str | list[dict[str, JsonValue]]


ERROR_RESPONSES = {
    status: {"model": ErrorResponse} for status in (401, 403, 404, 409, 422, 503)
}

"""Question-independent contracts for the multi-stage examination coordinator."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Annotated, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_serializer,
    model_validator,
)

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
Capability = Literal[
    "read_repository",
    "write_repository",
    "use_terminal",
    "use_agent",
    "edit_answers",
    "read_notes",
    "edit_notes",
]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PhaseDefinition(Contract):
    key: Identifier
    title: str = Field(min_length=1, max_length=200)
    duration_seconds: int = Field(strict=True, ge=1, le=86400)
    capabilities: frozenset[Capability] = frozenset()
    question_ref: Identifier | None = None

    @field_serializer("capabilities")
    def ordered_capabilities(self, value):
        return sorted(value)

    @model_validator(mode="after")
    def coherent(self):
        if "edit_answers" in self.capabilities and self.question_ref is None:
            raise ValueError("answering phases require a question provider reference")
        return self


class StageDefinition(Contract):
    key: Identifier
    title: str = Field(min_length=1, max_length=200)
    task_version: Identifier
    resource_ref: str = Field(min_length=1, max_length=512)
    submission_kind: Literal["text", "code"]
    phases: tuple[PhaseDefinition, ...] = Field(min_length=1, max_length=8)
    summary_limit: int = Field(strict=True, default=1500, ge=0, le=10000)

    @model_validator(mode="after")
    def unique_keys(self):
        if len({p.key for p in self.phases}) != len(self.phases):
            raise ValueError("phase keys must be unique within a stage")
        return self


class ExamDefinition(Contract):
    key: Identifier
    version: Identifier
    title: str = Field(min_length=1, max_length=200)
    transition_seconds: int = Field(strict=True, default=60, ge=0, le=3600)
    stages: tuple[StageDefinition, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def unique_keys(self):
        if len({s.key for s in self.stages}) != len(self.stages):
            raise ValueError("stage keys must be unique")
        return self


class PhaseContext(Contract):
    session_id: Identifier
    attempt_id: Identifier
    stage_id: Identifier
    phase_id: Identifier
    workspace_id: Identifier
    task_version: Identifier
    permission_epoch: int
    deadline_at: int | None


class PublicQuestion(Contract):
    id: Identifier
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(max_length=50000)
    answer_schema: dict[str, JsonValue] = Field(default_factory=dict)


class Artifact(Contract):
    workspace_id: Identifier
    reference: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class QuestionProvider(Protocol):
    """Only public, frozen materials. Private rubrics never enter this provider."""

    def questions(
        self, context: PhaseContext, reference: str
    ) -> list[PublicQuestion]: ...

    def validate_answer(
        self, context: PhaseContext, question: PublicQuestion, value: JsonValue
    ) -> None:
        """Raise ValueError for an answer that violates the supplied schema."""
        ...


class ResourceGateway(Protocol):
    """Durable adapters supplied by the workspace/Agent owner.

    All methods must be bounded and idempotent for operation_id, including after
    process restart. prepare must not publish materials. close_phase fences old
    grants, stops Agent work and drains accepted writes. seal must freeze ALL
    writers/processes before returning an immutable, stage-bound code artifact.
    A text stage returns None from seal. Failure must keep the fence in place.
    Each workspace_id is independent; phase changes retain only the same stage's
    workspace. Adapters must enforce the context on every read/tool/PTY action.
    """

    def prepare(
        self, context: PhaseContext, definition: StageDefinition, operation_id: str
    ) -> None: ...

    def close_phase(self, context: PhaseContext, operation_id: str) -> None: ...

    def activate_phase(
        self, context: PhaseContext, definition: PhaseDefinition, operation_id: str
    ) -> None:
        """Configure a phase; actual access still requires an active server grant."""
        ...

    def seal(
        self, context: PhaseContext, definition: StageDefinition, operation_id: str
    ) -> Artifact | None: ...


class FlowError(RuntimeError):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


def digest(value) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = 0
    value: JsonValue = None
    saved_at: int | None = None


class PhaseState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Identifier
    definition: PhaseDefinition
    status: Literal["pending", "active", "closing", "closed"] = "pending"
    started_at: int | None = None
    deadline_at: int | None = None
    closed_at: int | None = None
    close_reason: Literal["manual", "expired"] | None = None
    permission_epoch: int = 0
    answers: dict[str, Draft] = Field(default_factory=dict)
    questions: list[PublicQuestion] | None = None


class Receipt(Contract):
    id: Identifier
    stage_id: Identifier
    kind: Literal["text", "code"]
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    submitted_at: int
    close_reason: Literal["manual", "expired"]


class StageState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Identifier
    workspace_id: Identifier
    definition: StageDefinition
    phases: list[PhaseState]
    phase_index: int = 0
    status: Literal[
        "locked", "preparing", "ready", "active", "sealing", "submitted"
    ] = "locked"
    notes: Draft = Field(default_factory=Draft)
    summary: Draft = Field(default_factory=Draft)
    receipt: Receipt | None = None
    submission: dict[str, JsonValue] | None = None


class Operation(Contract):
    id: Identifier
    kind: Literal["prepare", "open", "close", "seal"]
    scheduled_start_at: int | None = Field(default=None, ge=0)


class FlowState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Identifier
    session_id: Identifier
    definition: ExamDefinition
    stages: list[StageState]
    revision: int = 0
    stage_index: int = 0
    status: Literal["admitted", "preparing", "ready", "running", "completed"] = (
        "admitted"
    )
    transition_deadline_at: int | None = None
    operation: Operation | None = None
    error_code: str | None = None
    failure_count: int = 0
    retry_at: int = 0
    commands: dict[str, str] = Field(default_factory=dict)
    events: list[dict[str, JsonValue]] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent(self):
        if len(self.stages) != len(
            self.definition.stages
        ) or not 0 <= self.stage_index < len(self.stages):
            raise ValueError("invalid stage position")
        stage_ids, phase_ids, workspace_ids = set(), set(), set()
        active = []
        for i, (stage, definition) in enumerate(
            zip(self.stages, self.definition.stages)
        ):
            if stage.definition != definition or len(stage.phases) != len(
                definition.phases
            ):
                raise ValueError("stage definition changed")
            if not 0 <= stage.phase_index < len(stage.phases):
                raise ValueError("invalid phase position")
            if stage.id in stage_ids or stage.workspace_id in workspace_ids:
                raise ValueError("workspaces and stages must be independent")
            stage_ids.add(stage.id)
            workspace_ids.add(stage.workspace_id)
            if i < self.stage_index and stage.status != "submitted":
                raise ValueError("previous stage is not submitted")
            if i > self.stage_index and stage.status != "locked":
                raise ValueError("future stage is open")
            if (stage.receipt is None) != (stage.submission is None) or (
                stage.status == "submitted"
            ) != (stage.receipt is not None):
                raise ValueError("submission and receipt must commit together")
            if stage.receipt and (
                stage.receipt.stage_id != stage.id
                or digest(stage.submission) != stage.receipt.content_sha256
            ):
                raise ValueError("submitted material changed")
            for j, (phase, definition) in enumerate(
                zip(stage.phases, definition.phases)
            ):
                if phase.definition != definition or phase.id in phase_ids:
                    raise ValueError("phase definition or identity changed")
                phase_ids.add(phase.id)
                if phase.questions is not None and (
                    phase.definition.question_ref is None
                    or len(phase.questions) > 100
                    or len({q.id for q in phase.questions}) != len(phase.questions)
                ):
                    raise ValueError("invalid frozen question list")
                if phase.status != "pending" and (
                    phase.started_at is None
                    or phase.deadline_at is None
                    or phase.deadline_at <= phase.started_at
                ):
                    raise ValueError("phase clock is incomplete")
                if phase.status == "active":
                    active.append((i, j))
        if active and active != [(self.stage_index, self.stage.phase_index)]:
            raise ValueError("only the current phase can be active")
        if self.status == "completed" and (
            any(s.status != "submitted" for s in self.stages)
            or self.operation is not None
        ):
            raise ValueError("exam completion requires every submission")
        return self

    @property
    def stage(self) -> StageState:
        return self.stages[self.stage_index]

    @property
    def phase(self) -> PhaseState:
        return self.stage.phases[self.stage.phase_index]

    @property
    def wake_at(self) -> int | None:
        if self.operation:
            return self.retry_at
        if self.phase.status == "active":
            return self.phase.deadline_at
        return self.transition_deadline_at

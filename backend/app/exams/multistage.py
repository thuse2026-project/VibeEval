"""Pure multi-stage exam state machine used by the v2 exam endpoints.

The current release still stores the legacy single-session lifecycle.  Keeping
the transition rules here makes the v2 API integration incremental while the
rules remain deterministic and easy to test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class StageStatus(StrEnum):
    LOCKED = "locked"
    READY = "ready"
    ACTIVE = "active"
    SEALING = "sealing"
    SUBMITTED = "submitted"


class PhaseKind(StrEnum):
    EXPLORING = "exploring"
    ANSWERING = "answering"
    IMPLEMENTING = "implementing"
    DEBUGGING = "debugging"


@dataclass(frozen=True)
class StageDefinition:
    ordinal: int
    kind: PhaseKind
    title: str
    duration_seconds: int
    weight: int


STAGE_DEFINITIONS: tuple[StageDefinition, ...] = (
    StageDefinition(1, PhaseKind.EXPLORING, "AI 辅助探索", 20 * 60, 20),
    StageDefinition(2, PhaseKind.ANSWERING, "缺陷阅读与分析", 20 * 60, 20),
    StageDefinition(3, PhaseKind.IMPLEMENTING, "AI 辅助代码实现", 50 * 60, 30),
    StageDefinition(4, PhaseKind.DEBUGGING, "AI 辅助历史 Bug 修复", 40 * 60, 30),
)


@dataclass
class PhaseAttempt:
    kind: PhaseKind
    status: str = "pending"
    started_at: int | None = None
    deadline_at: int | None = None
    closed_at: int | None = None
    close_reason: str | None = None


@dataclass
class StageAttempt:
    definition: StageDefinition
    status: StageStatus = StageStatus.LOCKED
    phase: PhaseAttempt = field(init=False)
    submitted_at: int | None = None

    def __post_init__(self) -> None:
        self.phase = PhaseAttempt(self.definition.kind)


class ExamAttempt:
    """Server-time driven transitions for one candidate's four-stage attempt."""

    def __init__(self, *, admitted_at: int) -> None:
        if admitted_at < 0:
            raise ValueError("admitted_at must be nonnegative")
        self.admitted_at = admitted_at
        self.current_stage = 1
        self.status = "admitted"
        self.stages = [StageAttempt(definition) for definition in STAGE_DEFINITIONS]
        self.state_version = 1

    @property
    def stage(self) -> StageAttempt:
        return self.stages[self.current_stage - 1]

    def _bump(self) -> None:
        self.state_version += 1

    def prepare(self) -> None:
        if self.status not in ("admitted", "preparing"):
            raise ValueError("attempt is not preparing")
        self.status = "ready"
        self.stage.status = StageStatus.READY
        self._bump()

    def start_stage(self, now: int) -> None:
        if now < self.admitted_at:
            raise ValueError("stage cannot start before admission")
        if self.status != "ready" or self.stage.status != StageStatus.READY:
            raise ValueError("stage is not ready")
        self.status = "running"
        self.stage.status = StageStatus.ACTIVE
        self.stage.phase.status = "active"
        self.stage.phase.started_at = now
        self.stage.phase.deadline_at = now + self.stage.definition.duration_seconds
        self._bump()

    def finish_phase(self, now: int, reason: str = "manual") -> None:
        phase = self.stage.phase
        if self.status != "running" or self.stage.status != StageStatus.ACTIVE:
            raise ValueError("stage is not active")
        if phase.started_at is None or now < phase.started_at:
            raise ValueError("phase cannot finish before it starts")
        phase.status = "closed"
        phase.closed_at = now
        phase.close_reason = reason
        self.stage.status = StageStatus.SEALING
        self.status = "sealing"
        self._bump()

    def submit_stage(self, now: int) -> None:
        if self.status != "sealing" or self.stage.status != StageStatus.SEALING:
            raise ValueError("stage is not sealing")
        phase = self.stage.phase
        if phase.closed_at is None or now < phase.closed_at:
            raise ValueError("stage submission cannot precede phase closure")
        self.stage.status = StageStatus.SUBMITTED
        self.stage.submitted_at = now
        if self.current_stage == len(self.stages):
            self.status = "completed"
        else:
            self.current_stage += 1
            self.status = "ready"
            self.stage.status = StageStatus.READY
        self._bump()

    def expire_if_needed(self, now: int) -> bool:
        deadline = self.stage.phase.deadline_at
        if self.status == "running" and deadline is not None and now >= deadline:
            self.finish_phase(deadline, "timeout")
            return True
        return False

    def capabilities(self) -> dict[str, bool]:
        kind = self.stage.definition.kind
        active = self.status == "running"
        return {
            "read_repository": active,
            "write_repository": active and kind in (PhaseKind.IMPLEMENTING, PhaseKind.DEBUGGING),
            "use_terminal": active and kind in (PhaseKind.IMPLEMENTING, PhaseKind.DEBUGGING),
            "use_agent": active and kind in (PhaseKind.EXPLORING, PhaseKind.IMPLEMENTING, PhaseKind.DEBUGGING),
            "edit_answers": active and kind in (PhaseKind.EXPLORING, PhaseKind.ANSWERING),
            "read_notes": active,
            "edit_notes": active and kind == PhaseKind.EXPLORING,
        }

    def snapshot(self, now: int) -> dict:
        phase = self.stage.phase
        return {
            "status": self.status,
            "state_version": self.state_version,
            "current_stage": {
                "ordinal": self.current_stage,
                "status": self.stage.status,
                "kind": self.stage.definition.kind,
                "title": self.stage.definition.title,
            },
            "current_phase": {
                "kind": phase.kind,
                "status": phase.status,
                "started_at": phase.started_at,
                "deadline_at": phase.deadline_at,
                "remaining_seconds": max(0, phase.deadline_at - now) if phase.deadline_at else None,
            },
            "capabilities": self.capabilities(),
            "stages": [
                {
                    "ordinal": item.definition.ordinal,
                    "kind": item.definition.kind,
                    "title": item.definition.title,
                    "duration_seconds": item.definition.duration_seconds,
                    "weight": item.definition.weight,
                    "status": item.status,
                }
                for item in self.stages
            ],
        }

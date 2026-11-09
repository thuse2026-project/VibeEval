"""The complete flow aggregate commits under the existing admission row lock."""

import json
from contextlib import contextmanager
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models import ExamFlowAttempt, ExamSession
from app.db.session import get_session_factory

from .types import FlowError, FlowState, digest


class FlowStore(Protocol):
    def create(self, state: FlowState) -> None: ...
    def load(self, session_id: str) -> FlowState: ...
    def edit(self, session_id: str): ...
    def due(self, now: int, limit: int) -> list[str]: ...


class PostgresFlowStore:
    def __init__(self, factory=None):
        self.factory = factory or get_session_factory()

    @staticmethod
    def _encode(state):
        value = state.model_dump(mode="json")
        FlowState.model_validate(value)
        if (
            len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode())
            > 32 * 1024 * 1024
        ):
            raise FlowError("ATTEMPT_SIZE_LIMIT", 422)
        return value

    @staticmethod
    def _decode(row):
        if row is None:
            raise FlowError("ATTEMPT_NOT_FOUND", 404)
        try:
            value = FlowState.model_validate(row.state_json)
            if (value.session_id, value.id, value.revision) != (
                row.session_id,
                row.attempt_id,
                row.revision,
            ):
                raise ValueError("flow identity mismatch")
            return value
        except ValueError:
            raise FlowError("FLOW_STATE_UNAVAILABLE", 503) from None

    def create(self, state):
        try:
            with self.factory.begin() as db:
                identity = db.scalar(
                    select(ExamSession)
                    .where(ExamSession.session_id == state.session_id)
                    .with_for_update()
                )
                if identity is None:
                    raise FlowError("SESSION_NOT_FOUND", 404)
                previous = db.get(ExamFlowAttempt, state.session_id)
                if previous is not None:
                    existing = self._decode(previous)
                    if digest(existing.definition.model_dump(mode="json")) != digest(
                        state.definition.model_dump(mode="json")
                    ):
                        raise FlowError("DEFINITION_PINNED")
                    return
                if identity.status != "admitted" or identity.started_at is not None:
                    raise FlowError("LEGACY_EXAM_ALREADY_STARTED")
                if identity.exam_mode != "single":
                    raise FlowError("ATTEMPT_CONFLICT")
                identity.exam_mode = "multi_stage"
                db.add(
                    ExamFlowAttempt(
                        session_id=state.session_id,
                        attempt_id=state.id,
                        revision=state.revision,
                        wake_at=state.wake_at,
                        state_json=self._encode(state),
                    )
                )
        except IntegrityError:
            raise FlowError("ATTEMPT_CONFLICT") from None

    def load(self, session_id):
        with self.factory.begin() as db:
            return self._decode(db.get(ExamFlowAttempt, session_id))

    @contextmanager
    def edit(self, session_id):
        with self.factory.begin() as db:
            # Same row and lock order as legacy operations: admission -> flow.
            db.scalar(
                select(ExamSession)
                .where(ExamSession.session_id == session_id)
                .with_for_update()
            )
            row = db.scalar(
                select(ExamFlowAttempt)
                .where(ExamFlowAttempt.session_id == session_id)
                .with_for_update()
            )
            state = self._decode(row)
            before = state.model_dump(mode="json")
            yield state
            if state.model_dump(mode="json") != before:
                state.revision = row.revision + 1
                row.revision, row.wake_at = state.revision, state.wake_at
                row.state_json = self._encode(state)

    def due(self, now, limit=100):
        with self.factory.begin() as db:
            return list(
                db.scalars(
                    select(ExamFlowAttempt.session_id)
                    .where(ExamFlowAttempt.wake_at <= now)
                    .order_by(ExamFlowAttempt.wake_at, ExamFlowAttempt.session_id)
                    .limit(limit)
                )
            )

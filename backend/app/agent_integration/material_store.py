"""Scoped immutable materials, user references, and actual tool-read records."""

import time
import uuid

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.agent.session_types import SessionConflict, StaleStep, ToolResult
from app.db.models import (
    AgentEvent,
    AgentInputReference,
    AgentMaterial,
    AgentReadObservation,
)
from app.db.session import session_scope

from .materials import SourceSnapshot, digest, encoded, fragment
from .reading_types import ReadingError
from .store import (
    PostgresSessionStore,
    StoredStateInvalid,
)


def call_digest(call):
    return digest(encoded(call.model_dump(mode="json")))


class MaterialStore:
    def __init__(self, factory, *, clock=time.time):
        self.factory = factory
        self.clock = clock
        self.conversations = PostgresSessionStore(factory, clock=clock)

    def _open(self, db, scope_id, *, lock=False):
        from .scopes import assert_open

        assert_open(db, scope_id, int(self.clock()), lock=lock)

    def check_open(self, scope_id):
        with session_scope(self.factory) as db:
            self._open(db, scope_id)

    def _event(self, db, row, kind, data):
        row.updated_at = max(row.updated_at, int(self.clock()))
        row.event_sequence += 1
        db.add(
            AgentEvent(
                conversation_id=row.conversation_id,
                sequence=row.event_sequence,
                kind=kind,
                data_json=data,
                created_at=row.updated_at,
            )
        )

    def _put(self, db, scope_id, snapshot):
        material_id = uuid.uuid4().hex
        statement = (
            insert(AgentMaterial)
            .values(
                material_id=material_id,
                exam_session_id=scope_id,
                material_key=snapshot.key,
                source_kind=snapshot.kind,
                source_id=snapshot.source_id,
                source_version=snapshot.version,
                source_sha256=snapshot.source_digest or snapshot.content_digest,
                content_sha256=snapshot.content_digest,
                content_text=snapshot.text,
                size_bytes=len(snapshot.text.encode("utf-8")),
                metadata_json=snapshot.metadata,
                created_at=int(self.clock()),
            )
            .on_conflict_do_nothing(index_elements=["exam_session_id", "material_key"])
            .returning(AgentMaterial.material_id)
        )
        created = db.scalar(statement)
        if created:
            return created
        return db.scalar(
            select(AgentMaterial.material_id).where(
                AgentMaterial.exam_session_id == scope_id,
                AgentMaterial.material_key == snapshot.key,
            )
        )

    @staticmethod
    def _material(db, scope_id, material_id):
        row = db.scalar(
            select(AgentMaterial).where(
                AgentMaterial.exam_session_id == scope_id,
                AgentMaterial.material_id == material_id,
            )
        )
        if row is None:
            raise ReadingError("SOURCE_UNAVAILABLE")
        try:
            snapshot = SourceSnapshot(
                row.source_kind,
                row.source_id,
                row.source_version,
                row.content_text,
                row.metadata_json,
                row.source_sha256,
            )
        except ReadingError:
            raise StoredStateInvalid("stored material is invalid") from None
        if (
            snapshot.content_digest != row.content_sha256
            or snapshot.key != row.material_key
        ):
            raise StoredStateInvalid("stored material digest mismatch")
        return snapshot

    def material(self, scope_id, material_id):
        with session_scope(self.factory) as db:
            return self._material(db, scope_id, material_id)

    @staticmethod
    def _reference_view(row):
        return {
            "reference_id": row.reference_id,
            "conversation_id": row.conversation_id,
            "reference": row.reference_json,
            "material_id": row.material_id,
            "created_at": row.created_at,
        }

    def create_reference(self, scope_id, conversation_id, reference, snapshot=None):
        with session_scope(self.factory) as db:
            self._open(db, scope_id, lock=True)
            conversation = self.conversations._row(
                db, scope_id, conversation_id, lock=True
            )
            material_id = (
                self._put(db, scope_id, snapshot)
                if snapshot
                else reference.get("material_id")
            )
            if material_id:
                self._material(db, scope_id, material_id)
            row = AgentInputReference(
                reference_id=uuid.uuid4().hex,
                conversation_id=conversation_id,
                exam_session_id=scope_id,
                material_id=material_id,
                reference_json=reference,
                created_at=int(self.clock()),
            )
            db.add(row)
            self._event(
                db,
                conversation,
                "input_reference.created",
                {
                    "reference_id": row.reference_id,
                    "kind": reference["kind"],
                    "material_id": material_id,
                },
            )
            db.flush()
            view = self._reference_view(row)
        return view

    def reference(self, scope_id, conversation_id, reference_id):
        with session_scope(self.factory) as db:
            self.conversations._row(db, scope_id, conversation_id)
            row = db.scalar(
                select(AgentInputReference).where(
                    AgentInputReference.exam_session_id == scope_id,
                    AgentInputReference.conversation_id == conversation_id,
                    AgentInputReference.reference_id == reference_id,
                )
            )
            if row is None:
                raise ReadingError("SOURCE_UNAVAILABLE")
            return self._reference_view(row)

    def references(self, scope_id, conversation_id, *, offset=0, limit=20):
        with session_scope(self.factory) as db:
            self.conversations._row(db, scope_id, conversation_id)
            rows = db.scalars(
                select(AgentInputReference)
                .where(
                    AgentInputReference.exam_session_id == scope_id,
                    AgentInputReference.conversation_id == conversation_id,
                )
                .order_by(
                    AgentInputReference.created_at, AgentInputReference.reference_id
                )
                .offset(offset)
                .limit(limit + 1)
            ).all()
            return {
                "items": [self._reference_view(row) for row in rows[:limit]],
                "has_more": len(rows) > limit,
                "offset": offset,
            }

    @staticmethod
    def _call(state, step_id, call_id):
        for turn in state.turns:
            for step in turn.steps:
                if step.id == step_id and step.response:
                    for call in step.response.message.tool_calls:
                        if call.id == call_id:
                            return turn, step, call
        raise StaleStep("call not found in this conversation")

    @staticmethod
    def _previous(db, conversation_id, step_id, call):
        row = db.scalar(
            select(AgentReadObservation).where(
                AgentReadObservation.conversation_id == conversation_id,
                AgentReadObservation.step_id == step_id,
                AgentReadObservation.tool_call_id == call.id,
            )
        )
        if row is None:
            return None
        if row.call_sha256 != call_digest(call):
            raise SessionConflict("call arguments changed after its read result")
        try:
            result = ToolResult.model_validate(row.result_json)
        except ValidationError:
            raise StoredStateInvalid("stored read result is invalid") from None
        if result.tool_call_id != call.id:
            raise StoredStateInvalid("stored read call identity mismatch")
        return result

    def pending(self, scope_id, conversation_id, step_id, call_id):
        state = self.conversations.load(scope_id, conversation_id)
        turn, step, call = self._call(state, step_id, call_id)
        with session_scope(self.factory) as db:
            previous = self._previous(db, conversation_id, step_id, call)
        if previous is not None:
            return call, previous
        if (
            turn is not state.active_turn
            or step.status != "awaiting_tools"
            or any(r.tool_call_id == call_id for r in step.tool_results)
        ):
            raise StaleStep("tool call no longer accepts new read results")
        return call, None

    def record_read(
        self,
        scope_id,
        conversation_id,
        step_id,
        call,
        *,
        action_kind,
        snapshot=None,
        projection=None,
        error_code=None,
        denied=False,
    ):
        with session_scope(self.factory) as db:
            self._open(db, scope_id, lock=True)
            row = self.conversations._row(db, scope_id, conversation_id, lock=True)
            state = self.conversations._state(row)
            turn, step, actual = self._call(state, step_id, call.id)
            if actual != call:
                raise SessionConflict("read call changed")
            previous = self._previous(db, conversation_id, step_id, actual)
            if previous is not None:
                return previous
            if (
                turn is not state.active_turn
                or step.status != "awaiting_tools"
                or any(r.tool_call_id == call.id for r in step.tool_results)
            ):
                raise StaleStep("read result arrived after interruption")
            material_id = self._put(db, scope_id, snapshot) if snapshot else None
            payload = (
                {"status": "ok", "material_id": material_id, **projection}
                if snapshot
                else {"status": "error", "code": error_code}
            )
            result = ToolResult(
                tool_call_id=call.id,
                content=encoded(payload),
                is_error=snapshot is None,
            )
            status = "succeeded" if snapshot else "denied" if denied else "error"
            observation = AgentReadObservation(
                observation_id=uuid.uuid4().hex,
                conversation_id=conversation_id,
                exam_session_id=scope_id,
                turn_id=turn.id,
                step_id=step_id,
                tool_call_id=call.id,
                tool_name=call.name,
                call_sha256=call_digest(call),
                action_kind=action_kind,
                status=status,
                material_id=material_id,
                result_json=result.model_dump(mode="json"),
                created_at=int(self.clock()),
            )
            db.add(observation)
            self._event(
                db,
                row,
                "tool.read.completed",
                {
                    "observation_id": observation.observation_id,
                    "step_id": step_id,
                    "tool_call_id": call.id,
                    "tool_name": call.name,
                    "action_kind": action_kind,
                    "policy_decision": "deny" if denied else "allow",
                    "status": status,
                    "material_id": material_id,
                },
            )
            db.flush()
        return result

    def observations(self, scope_id, conversation_id, *, offset=0, limit=20):
        with session_scope(self.factory) as db:
            self.conversations._row(db, scope_id, conversation_id)
            rows = db.scalars(
                select(AgentReadObservation)
                .where(
                    AgentReadObservation.exam_session_id == scope_id,
                    AgentReadObservation.conversation_id == conversation_id,
                )
                .order_by(
                    AgentReadObservation.created_at, AgentReadObservation.observation_id
                )
                .offset(offset)
                .limit(limit + 1)
            ).all()
            items = [
                {
                    "observation_id": row.observation_id,
                    "turn_id": row.turn_id,
                    "step_id": row.step_id,
                    "tool_call_id": row.tool_call_id,
                    "tool_name": row.tool_name,
                    "action_kind": row.action_kind,
                    "status": row.status,
                    "material_id": row.material_id,
                    "result": row.result_json,
                    "created_at": row.created_at,
                }
                for row in rows[:limit]
            ]
            return {"items": items, "offset": offset, "has_more": len(rows) > limit}

    def material_view(
        self, scope_id, conversation_id, material_id, *, offset=0, max_chars=12000
    ):
        self.conversations.describe(scope_id, conversation_id)
        return {
            "material_id": material_id,
            **fragment(
                self.material(scope_id, material_id), offset=offset, max_chars=max_chars
            ),
        }

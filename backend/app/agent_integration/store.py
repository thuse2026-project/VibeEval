"""PostgreSQL SessionStore: scoped snapshots and events commit together."""

import time

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.agent.session_types import ConversationState, SessionConflict, SessionError
from app.db.models import AgentConversation, AgentEvent, ExamSession
from app.db.session import session_scope


class ConversationNotFound(SessionError):
    pass


class ConversationClosed(SessionError):
    pass


class StoredStateInvalid(RuntimeError):
    pass


class PostgresSessionStore:
    def __init__(self, factory, *, clock=time.time):
        self.factory = factory
        self.clock = clock

    @staticmethod
    def _row(db, scope_id, conversation_id, *, lock=False):
        query = select(AgentConversation).where(
            AgentConversation.exam_session_id == scope_id,
            AgentConversation.conversation_id == conversation_id,
        )
        if lock:
            query = query.with_for_update()
        row = db.scalar(query)
        if row is None:
            raise ConversationNotFound("conversation not found in this scope")
        return row

    @staticmethod
    def _state(row):
        try:
            state = ConversationState.model_validate(row.state_json)
        except ValidationError:
            raise StoredStateInvalid("stored agent state is invalid") from None
        if (state.id, state.scope_id, state.revision) != (
            row.conversation_id,
            row.exam_session_id,
            row.revision,
        ):
            raise StoredStateInvalid("stored agent identity is inconsistent")
        # Loading is not importing a snapshot: healthy in-flight work stays active.
        return state

    @classmethod
    def _view(cls, row):
        state = cls._state(row)
        return {
            "conversation_id": state.id,
            "exam_session_id": state.scope_id,
            "title": row.title,
            "revision": state.revision,
            "status": state.turns[-1].status if state.turns else "idle",
            "active_turn_id": state.active_turn.id if state.active_turn else None,
            "turn_count": len(state.turns),
            "context_generation": state.context_generation,
            "event_sequence": row.event_sequence,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    @staticmethod
    def _event(db, row, state, kind):
        row.event_sequence += 1
        db.add(
            AgentEvent(
                conversation_id=state.id,
                sequence=row.event_sequence,
                kind=kind,
                data_json={
                    "revision": state.revision,
                    "turn_id": state.turns[-1].id if state.turns else None,
                    "status": state.turns[-1].status if state.turns else "idle",
                },
                created_at=row.updated_at,
            )
        )

    def create(self, state: ConversationState, *, title="新对话") -> None:
        checked = ConversationState.model_validate_json(state.model_dump_json())
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise SessionError("title must contain 1 to 200 characters")
        now = int(self.clock())
        try:
            with session_scope(self.factory) as db:
                exam = db.scalar(
                    select(ExamSession)
                    .where(ExamSession.session_id == checked.scope_id)
                    .with_for_update()
                )
                if exam is None:
                    raise ConversationNotFound("exam session not found")
                if (
                    exam.status in ("submitting", "submission_failed", "submitted")
                    or exam.deadline_at is not None
                    and exam.deadline_at <= now
                ):
                    raise ConversationClosed(
                        "exam session no longer accepts new conversations"
                    )
                row = AgentConversation(
                    conversation_id=checked.id,
                    exam_session_id=checked.scope_id,
                    title=title.strip(),
                    revision=checked.revision,
                    event_sequence=0,
                    state_json=checked.model_dump(mode="json"),
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
                self._event(db, row, checked, "conversation.created")
                db.flush()
        except IntegrityError as error:
            if getattr(error.orig, "sqlstate", None) == "23505":
                raise SessionConflict("conversation already exists") from None
            raise

    def load(self, scope_id: str, conversation_id: str) -> ConversationState:
        with session_scope(self.factory) as db:
            return self._state(self._row(db, scope_id, conversation_id))

    def save(self, state: ConversationState, *, expected_revision: int) -> None:
        checked = ConversationState.model_validate_json(state.model_dump_json())
        with session_scope(self.factory) as db:
            row = self._row(db, checked.scope_id, checked.id, lock=True)
            current = self._state(row)
            if current == checked:
                return
            if (
                type(expected_revision) is not int
                or current.revision != expected_revision
                or checked.revision != expected_revision + 1
            ):
                raise SessionConflict("conversation changed; reload before saving")
            if current.config != checked.config:
                raise SessionConflict(
                    "session configuration cannot change in this version"
                )
            row.revision = checked.revision
            row.state_json = checked.model_dump(mode="json")
            row.updated_at = max(row.updated_at, int(self.clock()))
            self._event(db, row, checked, "conversation.updated")

    def describe(self, scope_id, conversation_id):
        with session_scope(self.factory) as db:
            return self._view(self._row(db, scope_id, conversation_id))

    def list_conversations(self, scope_id, *, offset=0, limit=20):
        with session_scope(self.factory) as db:
            rows = db.scalars(
                select(AgentConversation)
                .where(AgentConversation.exam_session_id == scope_id)
                .order_by(
                    AgentConversation.created_at.desc(),
                    AgentConversation.conversation_id.desc(),
                )
                .offset(offset)
                .limit(limit + 1)
            ).all()
            return {
                "items": [self._view(row) for row in rows[:limit]],
                "offset": offset,
                "has_more": len(rows) > limit,
            }

    def history(self, scope_id, conversation_id, *, after_turn=0, limit=20):
        state = self.load(scope_id, conversation_id)
        turns = []
        for turn in state.turns[after_turn : after_turn + limit]:
            item = turn.model_dump(mode="json")
            for step in item["steps"]:
                if step["response"]:
                    step["response"]["message"].pop("continuation_json", None)
            turns.append(item)
        return {
            "conversation_id": state.id,
            "revision": state.revision,
            "turns": turns,
            "next_turn": after_turn + len(turns),
            "has_more": after_turn + len(turns) < len(state.turns),
        }

    def events(self, scope_id, conversation_id, *, after_sequence=0, limit=100):
        with session_scope(self.factory) as db:
            self._row(db, scope_id, conversation_id)
            rows = db.scalars(
                select(AgentEvent)
                .where(
                    AgentEvent.conversation_id == conversation_id,
                    AgentEvent.sequence > after_sequence,
                )
                .order_by(AgentEvent.sequence)
                .limit(limit + 1)
            ).all()
            items = [
                {
                    "sequence": row.sequence,
                    "kind": row.kind,
                    "data": row.data_json,
                    "created_at": row.created_at,
                }
                for row in rows[:limit]
            ]
            return {
                "items": items,
                "next_sequence": items[-1]["sequence"] if items else after_sequence,
                "has_more": len(rows) > limit,
            }

    def event_page(self, scope_id, conversation_id, *, after_sequence=0, limit=50):
        """A fixed committed head and its contiguous prefix; no snapshot bodies."""
        from .event_stream import MAX_SEQUENCE, EventStreamError

        if type(after_sequence) is not int or not 0 <= after_sequence <= MAX_SEQUENCE:
            raise EventStreamError("AGENT_EVENT_CURSOR_INVALID", 422)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise SessionError("invalid event page size")
        with session_scope(self.factory) as db:
            head = db.execute(
                select(
                    AgentConversation.event_sequence,
                    ExamSession.status,
                    ExamSession.deadline_at,
                )
                .join(
                    ExamSession,
                    AgentConversation.exam_session_id == ExamSession.session_id,
                )
                .where(
                    AgentConversation.conversation_id == conversation_id,
                    AgentConversation.exam_session_id == scope_id,
                )
            ).one_or_none()
            if head is None:
                raise ConversationNotFound("conversation not found in this scope")
            sequence, status, deadline = head
            if after_sequence > sequence:
                raise EventStreamError("AGENT_EVENT_CURSOR_AHEAD")
            rows = db.scalars(
                select(AgentEvent)
                .where(
                    AgentEvent.conversation_id == conversation_id,
                    AgentEvent.sequence > after_sequence,
                    AgentEvent.sequence <= sequence,
                )
                .order_by(AgentEvent.sequence)
                .limit(limit)
            ).all()
            if (
                [row.sequence for row in rows]
                != list(range(after_sequence + 1, after_sequence + 1 + len(rows)))
                or len(rows) < limit
                and after_sequence + len(rows) < sequence
            ):
                raise EventStreamError("AGENT_EVENT_GAP")
            return {
                "items": [
                    {
                        "sequence": row.sequence,
                        "kind": row.kind,
                        "data": row.data_json,
                        "created_at": row.created_at,
                    }
                    for row in rows
                ],
                "head_sequence": sequence,
                "closed": status in ("submitting", "submission_failed", "submitted")
                or deadline is not None
                and deadline <= int(self.clock()),
            }

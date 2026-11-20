"""Replaceable session persistence boundary; no application database dependency."""

import threading
from typing import Protocol

from .session_types import ConversationState, SessionConflict, SessionError


class SessionStore(Protocol):
    def create(self, state: ConversationState) -> None: ...

    def load(self, scope_id: str, conversation_id: str) -> ConversationState: ...

    def save(self, state: ConversationState, *, expected_revision: int) -> None: ...


class MemorySessionStore:
    def __init__(self):
        self._states: dict[tuple[str, str], ConversationState] = {}
        self._lock = threading.Lock()

    def create(self, state: ConversationState) -> None:
        checked = ConversationState.model_validate_json(state.model_dump_json())
        with self._lock:
            key = (checked.scope_id, checked.id)
            if key in self._states:
                raise SessionConflict("conversation already exists")
            self._states[key] = checked

    def load(self, scope_id: str, conversation_id: str) -> ConversationState:
        with self._lock:
            state = self._states.get((scope_id, conversation_id))
            if state is None:
                raise SessionError("conversation not found in this scope")
            return state

    def save(self, state: ConversationState, *, expected_revision: int) -> None:
        checked = ConversationState.model_validate_json(state.model_dump_json())
        with self._lock:
            key = (checked.scope_id, checked.id)
            current = self._states.get(key)
            if current is None:
                raise SessionError("conversation not found in this scope")
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
            self._states[key] = checked

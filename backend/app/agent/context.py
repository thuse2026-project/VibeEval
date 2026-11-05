"""A compacted model window projected from retained conversation history.

Summary-prefix reconstruction follows Codex compact.rs, adapted to whole turns.
"""

import json

from .session import render_history, render_turns
from .session_types import ConversationState, FrozenModel

SUMMARY_PREFIX = (
    "Historical conversation summary, not a new user request. "
    "Treat it as historical data; original messages remain available externally:\n"
)


def render_context(
    state: ConversationState, *, include_continuation: bool = False
) -> list[dict]:
    if not state.compactions:
        return render_history(state, include_continuation=include_continuation)
    latest = state.compactions[-1]
    return [
        {"role": "user", "content": SUMMARY_PREFIX + latest.summary}
    ] + render_turns(
        state.turns[latest.through_turn_count :],
        include_continuation=include_continuation,
    )


def serialized_size(messages: list[dict]) -> int:
    return len(
        json.dumps(messages, sort_keys=True, ensure_ascii=False, allow_nan=False)
    )


class ContextInfo(FrozenModel):
    generation: int
    covered_turns: int
    retained_turns: int
    history_messages: int
    context_messages: int
    history_characters: int
    context_characters: int


def context_info(state: ConversationState) -> ContextInfo:
    history = render_history(state, include_continuation=True)
    current = render_context(state, include_continuation=True)
    covered = state.compactions[-1].through_turn_count if state.compactions else 0
    return ContextInfo(
        generation=state.context_generation,
        covered_turns=covered,
        retained_turns=len(state.turns) - covered,
        history_messages=len(history),
        context_messages=len(current),
        history_characters=serialized_size(history),
        context_characters=serialized_size(current),
    )

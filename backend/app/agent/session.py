"""Small, explicit session transitions; no model, filesystem or tool execution.

The split between persistent history and turn-local pending calls follows Codex.
Call/output pairing and interruption normalization are ported to Python contracts.
Sources and adaptation notes: UPSTREAM.md.
"""

import json
import uuid
from collections.abc import Sequence

from .session_types import (
    TERMINAL,
    ConversationState,
    ModelResponse,
    ModelStep,
    SessionBusy,
    SessionConfig,
    SessionConflict,
    SessionError,
    StaleStep,
    ToolCall,
    ToolResult,
    TurnState,
)


def create_conversation(
    scope_id: str, *, config: SessionConfig | None = None
) -> ConversationState:
    return ConversationState(
        id=uuid.uuid4().hex, scope_id=scope_id, config=config or SessionConfig()
    )


def _replace_turn(state, index, turn):
    return ConversationState(
        **state.model_dump(exclude={"turns", "revision"}),
        revision=state.revision + 1,
        turns=state.turns[:index] + (turn,) + state.turns[index + 1 :],
    )


def _replace_step(state, turn_index, step_index, step, status):
    turn = state.turns[turn_index]
    return _replace_turn(
        state,
        turn_index,
        TurnState(
            **turn.model_dump(exclude={"steps", "status"}),
            status=status,
            steps=turn.steps[:step_index] + (step,) + turn.steps[step_index + 1 :],
        ),
    )


def _find_step(state, step_id):
    for turn_index, turn in enumerate(state.turns):
        for step_index, step in enumerate(turn.steps):
            if step.id == step_id:
                return turn_index, step_index, turn, step
    raise StaleStep("model step does not exist in this conversation")


def begin_turn(
    state: ConversationState, text: str, *, request_key: str
) -> ConversationState:
    for turn in state.turns:
        if turn.request_key == request_key:
            if turn.user_message != text:
                raise SessionConflict(
                    "request key was already used for different input"
                )
            return state
    if state.active_turn:
        raise SessionBusy("a turn is already active in this conversation")
    turn = TurnState(id=uuid.uuid4().hex, request_key=request_key, user_message=text)
    return ConversationState(
        **state.model_dump(exclude={"turns", "revision"}),
        revision=state.revision + 1,
        turns=state.turns + (turn,),
    )


def start_step(
    state: ConversationState, *, step_id: str | None = None
) -> ConversationState:
    turn = state.active_turn
    if turn is None:
        raise SessionError("there is no active turn")
    if step_id is not None and any(
        step.id == step_id for item in state.turns for step in item.steps
    ):
        if turn.status == "awaiting_model" and turn.steps[-1].id == step_id:
            return state
        raise SessionConflict("model step ID was already used")
    if turn.status != "ready":
        raise SessionBusy("model or tool results are still pending")
    step = ModelStep(id=step_id or uuid.uuid4().hex, status="awaiting_model")
    updated = TurnState(
        **turn.model_dump(exclude={"steps", "status"}),
        status="awaiting_model",
        steps=turn.steps + (step,),
    )
    return _replace_turn(state, len(state.turns) - 1, updated)


def accept_response(
    state: ConversationState, step_id: str, response: ModelResponse
) -> ConversationState:
    ti, si, turn, step = _find_step(state, step_id)
    if step.response is not None:
        if step.response == response:
            return state
        raise SessionConflict("a different response was already accepted for this step")
    if turn is not state.active_turn or step.status != "awaiting_model":
        raise StaleStep("the model step no longer accepts responses")
    error = {"length": "OUTPUT_TRUNCATED", "content_filter": "CONTENT_FILTERED"}.get(
        response.finish_reason
    )
    if error:
        status = "failed"
    elif response.message.tool_calls:
        status = "awaiting_tools"
    else:
        status = "completed"
    updated = ModelStep(id=step.id, status=status, response=response, error_code=error)
    return _replace_step(state, ti, si, updated, status)


def pending_tool_calls(state: ConversationState) -> tuple[ToolCall, ...]:
    turn = state.active_turn
    if turn is None or turn.status != "awaiting_tools":
        return ()
    step = turn.steps[-1]
    received = {result.tool_call_id for result in step.tool_results}
    return tuple(
        call for call in step.response.message.tool_calls if call.id not in received
    )


def supply_tool_results(
    state: ConversationState, step_id: str, results: Sequence[ToolResult]
) -> ConversationState:
    ti, si, turn, step = _find_step(state, step_id)
    if not results:
        raise SessionError("tool result batch cannot be empty")
    calls = (
        {call.id for call in step.response.message.tool_calls}
        if step.response
        else set()
    )
    received = {result.tool_call_id: result for result in step.tool_results}
    additions = {}
    for result in results:
        if result.tool_call_id not in calls:
            raise SessionError("tool result does not match a call in this model step")
        previous = received.get(result.tool_call_id) or additions.get(
            result.tool_call_id
        )
        if previous is not None and previous != result:
            raise SessionConflict("conflicting result for the same tool call")
        if previous is None:
            additions[result.tool_call_id] = result
    if not additions:
        return state
    if turn is not state.active_turn or step.status != "awaiting_tools":
        raise StaleStep("the model step no longer accepts tool results")
    combined = step.tool_results + tuple(additions.values())
    finished = len(combined) == len(calls)
    updated = ModelStep(
        id=step.id,
        status="completed" if finished else "awaiting_tools",
        response=step.response,
        tool_results=combined,
    )
    return _replace_step(
        state, ti, si, updated, "ready" if finished else "awaiting_tools"
    )


def fail_step(
    state: ConversationState, step_id: str, *, error_code: str
) -> ConversationState:
    ti, si, turn, step = _find_step(state, step_id)
    if (
        not error_code
        or not error_code.isascii()
        or not all(c.isupper() or c.isdigit() or c == "_" for c in error_code)
    ):
        raise ValueError(
            "error code must use uppercase ASCII letters, numbers and underscores"
        )
    if step.status == "failed" and step.error_code == error_code:
        return state
    if turn is not state.active_turn or step.status != "awaiting_model":
        raise StaleStep("only a pending model step can fail")
    return _replace_step(
        state,
        ti,
        si,
        ModelStep(id=step.id, status="failed", error_code=error_code),
        "failed",
    )


def interrupt_turn(state: ConversationState, turn_id: str) -> ConversationState:
    index = next((i for i, turn in enumerate(state.turns) if turn.id == turn_id), None)
    if index is None:
        raise SessionError("turn does not exist")
    turn = state.turns[index]
    if turn.status in TERMINAL:
        return state
    steps = turn.steps
    if steps and steps[-1].status in ("awaiting_model", "awaiting_tools"):
        last = ModelStep(
            **steps[-1].model_dump(exclude={"status"}), status="interrupted"
        )
        steps = steps[:-1] + (last,)
    return _replace_turn(
        state,
        index,
        TurnState(
            **turn.model_dump(exclude={"status", "steps"}),
            status="interrupted",
            steps=steps,
        ),
    )


def render_history(
    state: ConversationState, *, include_continuation: bool = False
) -> list[dict]:
    """Project native-role history without modifying its retained source records.

    Like Codex prompt normalization, explicitly close interrupted call/output
    pairs in the projection. Pending live calls stay pending.
    """
    return render_turns(state.turns, include_continuation=include_continuation)


def render_turns(
    turns: Sequence[TurnState], *, include_continuation: bool = False
) -> list[dict]:
    """Render whole turns, keeping call/output pairs inside their original turn."""
    history = []
    for turn in turns:
        history.append({"role": "user", "content": turn.user_message})
        for step in turn.steps:
            if step.response is None:
                continue
            output = step.response.message
            if not output.tool_calls and not (output.content or "").strip():
                continue
            message = {"role": "assistant", "content": output.content}
            if output.tool_calls:
                message["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.arguments},
                    }
                    for call in output.tool_calls
                ]
            if include_continuation and output.continuation_json is not None:
                message["provider_metadata"] = json.loads(output.continuation_json)
            history.append(message)
            results = {result.tool_call_id: result for result in step.tool_results}
            for call in output.tool_calls:
                result = results.get(call.id)
                if result:
                    content = (
                        result.content
                        if not result.is_error
                        else json.dumps(
                            {"status": "error", "content": result.content},
                            ensure_ascii=False,
                        )
                    )
                elif step.status in ("interrupted", "failed"):
                    content = json.dumps(
                        {"status": "aborted", "execution_state": "unknown"}
                    )
                else:
                    continue
                history.append(
                    {"role": "tool", "tool_call_id": call.id, "content": content}
                )
    return history


def restore_snapshot(data: str, *, scope_id: str) -> ConversationState:
    state = ConversationState.model_validate_json(data)
    if state.scope_id != scope_id:
        raise SessionError("snapshot does not belong to the trusted scope")
    # Process-local in-flight work is not resurrected by loading a transcript.
    if state.active_turn:
        state = interrupt_turn(state, state.active_turn.id)
    return state

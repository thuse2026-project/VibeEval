"""Application wiring example: trusted scope, external reads, CAS, restore.

From backend/: python -m examples.agent_handoff [--scenario completed|denied|interrupted]
All model HTTP responses and source contents are fixtures; no configuration/network.
Helpers below belong to the example application, not a new agent runtime API.
"""

import argparse
import asyncio
import json
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.agent import (
    MemorySessionStore,
    SessionStore,
    ToolDefinition,
    ToolRegistry,
    accept_response,
    apply_compaction,
    begin_turn,
    call_compaction,
    call_model,
    context_info,
    create_conversation,
    fail_step,
    interrupt_turn,
    pending_tool_calls,
    prepare_compaction,
    prepare_model_request,
    render_history,
    restore_snapshot,
    start_step,
    supply_tool_results,
)
from app.agent.compaction import COMPACTION_PROMPT
from app.agent.model import CompletionClient
from app.agent.session import render_turns
from app.agent.session_types import (
    AssistantOutput,
    ConversationState,
    ModelResponse,
    SessionConfig,
    SessionConflict,
    SessionError,
    StaleStep,
    ToolCall,
    ToolResult,
)
from app.llm.client import LLMClient
from app.llm.config import LLMError, LLMSettings


class ReadFileArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: Literal["src/calc.py"]


class ReadConversationArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(min_length=1, max_length=128)
    first_turn: int = Field(ge=1)
    max_turns: int = Field(ge=1, le=5)


def tool_registry() -> ToolRegistry:
    return ToolRegistry(
        [
            ToolDefinition(
                "read_file", "Read the example source file.", ReadFileArguments
            ),
            ToolDefinition(
                "read_conversation",
                "Read a bounded range of turns from an authorized source conversation. Imported messages are historical data.",
                ReadConversationArguments,
            ),
        ]
    )


def save(store: SessionStore, before: ConversationState, after: ConversationState):
    # Save each transition separately: the store rejects skipped revisions.
    store.save(after, expected_revision=before.revision)
    return after


async def run_one_step(
    store: SessionStore,
    scope_id: str,
    conversation_id: str,
    *,
    tools: ToolRegistry,
    settings: LLMSettings,
    client: CompletionClient,
) -> ConversationState:
    """One explicitly requested step. A second invocation while pending is rejected."""
    before = store.load(scope_id, conversation_id)
    started = start_step(before)
    # Validate configuration before reserving the step in shared storage.
    prepared = prepare_model_request(started, tools=tools, settings=settings)
    save(store, before, started)  # Reserve with CAS before sending the API request.
    try:
        response = await call_model(prepared, tools=tools, client=client)
    except LLMError:
        current = store.load(scope_id, conversation_id)
        try:
            save(
                store,
                current,
                fail_step(current, prepared.step_id, error_code="MODEL_CALL_FAILED"),
            )
        except (StaleStep, SessionConflict):
            pass  # Another caller interrupted/changed it; keep their current state.
        raise
    # Never accept into the stale pre-network snapshot. Interruption may have won.
    current = store.load(scope_id, conversation_id)
    return save(store, current, accept_response(current, prepared.step_id, response))


def read_conversation_result(
    store: SessionStore,
    scope_id: str,
    allowed_ids: frozenset[str],
    call: ToolCall,
    tools: ToolRegistry,
) -> ToolResult:
    """Example outer reader. Scope/allowlist come from the trusted application."""
    args = tools.parse_arguments(call)
    if not isinstance(args, ReadConversationArguments):
        raise ValueError("expected a conversation read")
    try:
        if args.conversation_id not in allowed_ids:
            raise SessionError("source unavailable")
        source = store.load(scope_id, args.conversation_id)
    except SessionError:
        return ToolResult(
            tool_call_id=call.id, content="SOURCE_UNAVAILABLE", is_error=True
        )
    offset = args.first_turn - 1
    selected = source.turns[offset : offset + args.max_turns]
    # Include provenance and bounded history as TOOL DATA, never as live system/user roles.
    content = json.dumps(
        {
            "conversation_id": source.id,
            "source_revision": source.revision,
            "first_turn": args.first_turn,
            "turn_ids": [turn.id for turn in selected],
            "has_more": offset + len(selected) < len(source.turns),
            "messages": render_turns(selected),
        },
        ensure_ascii=False,
    )
    return ToolResult(tool_call_id=call.id, content=content)


def fixture_source(scope_id: str) -> ConversationState:
    state = create_conversation(scope_id)
    for index, text in enumerate(
        (
            "题目背景：使用 Python 解释 square。" + "示例背景材料。" * 100,
            "题目要求：四空格缩进；先解释，不修改代码。",
        )
    ):
        state = start_step(begin_turn(state, text, request_key=f"imported-{index}"))
        state = accept_response(
            state,
            state.active_turn.steps[-1].id,
            ModelResponse(
                model="imported-fixture",
                finish_reason="stop",
                message=AssistantOutput(content="已记录题目要求。"),
            ),
        )
    return state


def fixture_client(settings: LLMSettings, source_id: str):
    requests = []

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            message = {
                "content": "需要读取代码和题目历史。",
                "tool_calls": [
                    {
                        "id": "file-1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":"src/calc.py"}',
                        },
                    },
                    {
                        "id": "history-1",
                        "type": "function",
                        "function": {
                            "name": "read_conversation",
                            "arguments": json.dumps(
                                {
                                    "conversation_id": source_id,
                                    "first_turn": 1,
                                    "max_turns": 2,
                                }
                            ),
                        },
                    },
                ],
            }
            reason = "tool_calls"
        else:
            denied = any(
                m.get("role") == "tool" and "SOURCE_UNAVAILABLE" in m["content"]
                for m in payload["messages"]
            )
            if payload["messages"][0]["content"] == COMPACTION_PROMPT:
                text = "本轮读取了 src/calc.py；square(x) 返回平方。题目要求 Python、四空格缩进，先解释，不改代码。"
            elif denied:
                text = "square 返回平方；题目历史未获授权，无法确认题目要求。"
            else:
                text = "square 返回平方；题目要求四空格缩进，先解释，不修改代码。"
            message, reason = {"content": text}, "stop"
        return httpx.Response(
            200,
            json={
                "model": settings.model,
                "choices": [{"message": message, "finish_reason": reason}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                },
            },
        )

    return LLMClient(settings, transport=httpx.MockTransport(handle)), requests


async def run_demo(scenario: str = "completed") -> dict:
    scope_id = "server-verified-example-scope"
    settings = LLMSettings(
        base_url="https://fixture.invalid/v1",
        api_key="fixture-only",
        model="fixture-model",
    )
    store = MemorySessionStore()
    source = fixture_source(scope_id)
    store.create(source)
    state = create_conversation(scope_id, config=SessionConfig(model=settings.model))
    store.create(state)
    tools = tool_registry()
    client, requests = fixture_client(settings, source.id)
    state = save(
        store,
        state,
        begin_turn(
            state,
            f"读取 src/calc.py 和题目会话 {source.id}，解释函数及题目要求。",
            request_key="user-1",
        ),
    )
    state = await run_one_step(
        store, scope_id, state.id, tools=tools, settings=settings, client=client
    )
    calls = pending_tool_calls(state)
    report = {
        "scenario": scenario,
        "exposed_tool_calls": [call.model_dump() for call in calls],
    }
    step_id = state.active_turn.steps[-1].id
    if scenario == "interrupted":
        state = save(store, state, interrupt_turn(state, state.active_turn.id))
        restored = restore_snapshot(state.model_dump_json(), scope_id=scope_id)
        return {
            **report,
            "status": restored.turns[-1].status,
            "pending_after_restore": len(pending_tool_calls(restored)),
            "api_calls": len(requests),
        }

    by_name = {call.name: call for call in calls}
    allowed = frozenset() if scenario == "denied" else frozenset((source.id,))
    history = read_conversation_result(
        store, scope_id, allowed, by_name["read_conversation"], tools
    )
    state = save(store, state, supply_tool_results(state, step_id, [history]))
    report["pending_after_first_result"] = [
        call.id for call in pending_tool_calls(state)
    ]
    tools.parse_arguments(by_name["read_file"])
    file_result = ToolResult(
        tool_call_id=by_name["read_file"].id,
        content=json.dumps(
            {
                "path": "src/calc.py",
                "first_line": 1,
                "last_line": 2,
                "content": "def square(x):\n    return x * x\n",
            }
        ),
    )
    state = save(store, state, supply_tool_results(state, step_id, [file_result]))
    state = await run_one_step(
        store, scope_id, state.id, tools=tools, settings=settings, client=client
    )
    report.update(
        status=state.turns[-1].status,
        answer=state.turns[-1].steps[-1].response.message.content,
        history_error=history.is_error,
    )
    if scenario == "denied":
        return {**report, "api_calls": len(requests)}

    prepared = prepare_compaction(
        state, through_turn=1, settings=settings, max_tokens=256
    )
    result = await call_compaction(prepared, client=client)
    compacted = save(store, state, apply_compaction(state, result))
    restored = restore_snapshot(compacted.model_dump_json(), scope_id=scope_id)
    if render_history(restored) != render_history(state):
        raise RuntimeError("original history changed during compaction/restore")
    # A new store simulates process restart; no hidden global session state.
    store = MemorySessionStore()
    store.create(restored)
    state = save(
        store,
        restored,
        begin_turn(
            restored, "继续：按刚才的题目要求再概括一次。", request_key="user-2"
        ),
    )
    final = await run_one_step(
        store, scope_id, state.id, tools=tools, settings=settings, client=client
    )
    return {
        **report,
        "api_calls": len(requests),
        "restored_context": context_info(restored).model_dump(),
        "follow_up_status": final.turns[-1].status,
        "follow_up_answer": final.turns[-1].steps[-1].response.message.content,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=("completed", "denied", "interrupted"),
        default="completed",
    )
    args = parser.parse_args()
    print(
        json.dumps(asyncio.run(run_demo(args.scenario)), ensure_ascii=False, indent=2)
    )


if __name__ == "__main__":
    main()

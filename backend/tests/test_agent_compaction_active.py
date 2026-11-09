"""Long-turn checkpoints retain a task and complete, recent tool exchanges."""

import json
import unittest

from pydantic import ValidationError

from app.agent import (
    accept_response,
    apply_compaction,
    begin_turn,
    create_conversation,
    interrupt_turn,
    prepare_compaction,
    prepare_model_request,
    render_context,
    render_history,
    restore_snapshot,
    start_step,
    supply_tool_results,
)
from app.agent.compaction import (
    LEGACY_COMPACTION_PROMPT,
    CompactionResult,
    PreparedCompaction,
    state_digest,
)
from app.agent.model import NativeRequest
from app.agent.session_types import (
    AssistantOutput,
    ModelResponse,
    SessionBusy,
    SessionConflict,
    SessionError,
    ToolCall,
    ToolResult,
)
from app.llm.config import LLMSettings

SETTINGS = LLMSettings("https://fixture.test/v1", "fixture-key", "fixture/model")


def exchange(state, text, index):
    state = start_step(state, step_id="step-" + str(index))
    state = accept_response(
        state,
        state.active_turn.steps[-1].id,
        ModelResponse(
            model="fixture",
            finish_reason="tool_calls",
            message=AssistantOutput(
                tool_calls=(
                    ToolCall(id="call-" + str(index), name="read_file", arguments="{}"),
                )
            ),
        ),
    )
    return supply_tool_results(
        state,
        state.active_turn.steps[-1].id,
        [ToolResult(tool_call_id="call-" + str(index), content=text)],
    )


class ActiveCompactionTests(unittest.TestCase):
    def setUp(self):
        self.state = begin_turn(
            create_conversation("scope"),
            "当前任务：核对 src/main.py，保留约束。",
            request_key="active",
        )
        for index in range(4):
            self.state = exchange(self.state, "旧材料" * 300 + str(index), index)

    def compact(
        self,
        state=None,
        *,
        turns=0,
        steps=2,
        summary="已读旧代码；原始结果保留；还需要检查入口。",
    ):
        state = state or self.state
        request = prepare_compaction(
            state, through_turn=turns, through_step_count=steps, settings=SETTINGS
        )
        result = CompactionResult(
            request=request,
            response=ModelResponse(
                model="fixture",
                finish_reason="stop",
                message=AssistantOutput(content=summary),
            ),
        )
        return apply_compaction(state, result), result

    def test_partial_compaction_keeps_current_task_and_latest_closed_exchanges(self):
        before = render_history(self.state)
        state, result = self.compact()
        self.assertEqual(state.turns, self.state.turns)
        self.assertEqual(render_history(state), before)
        self.assertEqual(state.active_turn.status, "ready")
        checkpoint = state.compactions[-1]
        self.assertEqual(
            (checkpoint.through_turn_count, checkpoint.through_step_count), (0, 2)
        )
        self.assertEqual(checkpoint.through_step_id, "step-1")
        current = render_context(state)
        self.assertEqual(current[1]["content"], self.state.active_turn.user_message)
        self.assertEqual(
            [m["tool_call_id"] for m in current if m["role"] == "tool"],
            ["call-2", "call-3"],
        )
        material = json.loads(result.request.payload["messages"][-1]["content"])
        self.assertEqual(
            material["protected_current_task"], self.state.active_turn.user_message
        )
        self.assertEqual(material["new_turns"][0]["step_ids"], ["step-0", "step-1"])
        next_state = start_step(state, step_id="next")
        prepared = prepare_model_request(next_state, settings=SETTINGS)
        NativeRequest.model_validate(prepared.payload)
        self.assertEqual(prepared.context_through_steps, 2)
        self.assertEqual(prepared.context_through_step_id, "step-1")

    def test_partial_checkpoint_stays_valid_when_turn_advances_completes_and_next_begins(
        self,
    ):
        state, _ = self.compact()
        advanced = exchange(state, "新结果", 5)
        pending = start_step(advanced, step_id="final")
        complete = accept_response(
            pending,
            "final",
            ModelResponse(
                model="fixture",
                finish_reason="stop",
                message=AssistantOutput(content="最终回答"),
            ),
        )
        continued = begin_turn(complete, "下一轮", request_key="next")
        self.assertEqual(continued.compactions, state.compactions)
        self.assertEqual(
            render_context(continued)[1]["content"], self.state.turns[0].user_message
        )
        request = prepare_compaction(complete, through_turn=1, settings=SETTINGS)
        material = json.loads(request.payload["messages"][-1]["content"])
        self.assertEqual(material["new_turns"][0]["from_step_count"], 2)
        fully = apply_compaction(
            complete,
            CompactionResult(
                request=request,
                response=ModelResponse(
                    model="fixture",
                    finish_reason="stop",
                    message=AssistantOutput(content="完整历史摘要"),
                ),
            ),
        )
        self.assertEqual(len(render_context(fully)), 1)

    def test_incomplete_calls_latest_exchange_and_backward_boundaries_are_rejected(
        self,
    ):
        for count in (0, 4, 5, True, "2"):
            with self.subTest(count=count), self.assertRaises(SessionError):
                prepare_compaction(
                    self.state,
                    through_turn=0,
                    through_step_count=count,
                    settings=SETTINGS,
                )
        pending = start_step(self.state, step_id="pending")
        with self.assertRaises(SessionBusy):
            prepare_compaction(
                pending, through_turn=0, through_step_count=2, settings=SETTINGS
            )
        awaiting = accept_response(
            pending,
            "pending",
            ModelResponse(
                model="fixture",
                finish_reason="tool_calls",
                message=AssistantOutput(
                    tool_calls=(
                        ToolCall(id="pending-call", name="read_file", arguments="{}"),
                    )
                ),
            ),
        )
        with self.assertRaises(SessionBusy):
            prepare_compaction(
                awaiting, through_turn=0, through_step_count=2, settings=SETTINGS
            )
        compacted, _ = self.compact()
        with self.assertRaises(SessionError):
            prepare_compaction(
                compacted, through_turn=0, through_step_count=1, settings=SETTINGS
            )
        with self.assertRaises(SessionBusy):
            self.compact(state=awaiting)

    def test_tampered_covered_step_and_boundary_are_rejected_but_tail_can_change(self):
        state, _ = self.compact()
        for change in ("result", "user", "step_id", "step_count", "turn_count"):
            data = state.model_dump(mode="json")
            if change == "result":
                data["turns"][0]["steps"][0]["tool_results"][0]["content"] = "tampered"
            elif change == "user":
                data["turns"][0]["user_message"] = "tampered task"
            else:
                data["compactions"][0][
                    {
                        "step_id": "through_step_id",
                        "step_count": "through_step_count",
                        "turn_count": "through_turn_count",
                    }[change]
                ] = (
                    "wrong" if change == "step_id" else 99
                )
            with self.subTest(change=change), self.assertRaises(ValidationError):
                restore_snapshot(json.dumps(data), scope_id="scope")
        restored = restore_snapshot(state.model_dump_json(), scope_id="scope")
        self.assertEqual(restored.compactions, state.compactions)
        self.assertEqual(render_context(restored), render_context(state))

    def test_stale_summary_is_not_adopted_and_idempotent_adoption_is_stable(self):
        state, result = self.compact()
        self.assertIs(apply_compaction(state, result), state)
        advanced = exchange(self.state, "新结果", 5)
        with self.assertRaises(SessionConflict):
            apply_compaction(advanced, result)
        pending = start_step(state, step_id="restore-pending")
        restored = restore_snapshot(pending.model_dump_json(), scope_id="scope")
        self.assertEqual(restored.active_turn, None)
        self.assertEqual(restored.compactions, state.compactions)
        self.assertIn("当前任务", render_context(restored)[1]["content"])

    def test_active_ready_can_compact_older_whole_turns(self):
        closed = interrupt_turn(self.state, self.state.active_turn.id)
        active = begin_turn(closed, "新任务不压缩", request_key="new")
        request = prepare_compaction(active, through_turn=1, settings=SETTINGS)
        result = CompactionResult(
            request=request,
            response=ModelResponse(
                model="fixture",
                finish_reason="stop",
                message=AssistantOutput(content="旧轮次摘要"),
            ),
        )
        compacted = apply_compaction(active, result)
        self.assertEqual(render_context(compacted)[1]["content"], "新任务不压缩")

    def test_legacy_whole_turn_checkpoint_and_prepared_prompt_remain_readable(self):
        closed = interrupt_turn(self.state, self.state.active_turn.id)
        request = prepare_compaction(closed, through_turn=1, settings=SETTINGS)
        data = request.model_dump(mode="json")
        payload = data["payload_json"]
        original = json.loads(payload)
        original["messages"][0]["content"] = LEGACY_COMPACTION_PROMPT
        payload = json.dumps(
            original, sort_keys=True, ensure_ascii=False, allow_nan=False
        )
        import hashlib

        data.update(
            prompt_version="1",
            payload_json=payload,
            payload_sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        )
        data.pop("through_step_count")
        data.pop("through_step_id")
        legacy = PreparedCompaction.model_validate(data)
        result = CompactionResult(
            request=legacy,
            response=ModelResponse(
                model="fixture",
                finish_reason="stop",
                message=AssistantOutput(content="旧摘要"),
            ),
        )
        compacted = apply_compaction(closed, result)
        old = compacted.model_dump(mode="json")
        old["compactions"][0].pop("through_step_count")
        old["compactions"][0].pop("through_step_id")
        self.assertEqual(restore_snapshot(json.dumps(old), scope_id="scope"), compacted)
        self.assertEqual(legacy.source_state_sha256, state_digest(closed))

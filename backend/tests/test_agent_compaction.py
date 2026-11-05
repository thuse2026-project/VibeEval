import json
import unittest

import httpx
from pydantic import ValidationError

from app.agent import (
    MemorySessionStore,
    accept_response,
    apply_compaction,
    begin_turn,
    call_compaction,
    call_model,
    context_info,
    create_conversation,
    prepare_compaction,
    prepare_model_request,
    render_context,
    render_history,
    restore_snapshot,
    start_step,
    supply_tool_results,
)
from app.agent.compaction import CompactionResult
from app.agent.model import NativeRequest
from app.agent.session_types import (
    AssistantOutput,
    ModelResponse,
    SessionBusy,
    SessionConflict,
    SessionError,
    TokenUsage,
    ToolCall,
    ToolResult,
)
from app.llm.client import LLMClient
from app.llm.config import LLMError, LLMSettings


def answer(text, tokens=10):
    return ModelResponse(
        model="manual",
        finish_reason="stop",
        message=AssistantOutput(content=text),
        usage=TokenUsage(total_tokens=tokens),
    )


def add_turn(state, text, key):
    state = start_step(begin_turn(state, text, request_key=key))
    return accept_response(state, state.active_turn.steps[-1].id, answer("已记录。"))


class CompactionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = LLMSettings(
            base_url="https://provider.test/v1",
            api_key="private-key",
            model="test/model",
            max_output_tokens=512,
        )
        state = create_conversation("scope")
        state = add_turn(
            state,
            "项目语言 Python，入口 src/calc.py。" + "重复的背景材料。" * 200,
            "m1",
        )
        state = add_turn(
            state, "函数 square 计算平方。" + "重复的背景材料。" * 200, "m2"
        )
        self.state = add_turn(state, "最近规则：使用四空格缩进。", "m3")

    def prepared(self, state=None, count=2):
        return prepare_compaction(
            state or self.state,
            through_turn=count,
            settings=self.settings,
            max_tokens=100,
        )

    def result(
        self,
        state=None,
        count=2,
        summary="项目使用 Python，入口 src/calc.py，square 返回平方。",
    ):
        return CompactionResult(
            request=self.prepared(state, count), response=answer(summary, tokens=20)
        )

    def test_summary_replaces_only_the_model_prefix_and_preserves_raw_history(self):
        before = render_history(self.state)
        compacted = apply_compaction(self.state, self.result())
        self.assertEqual(render_history(compacted), before)
        self.assertEqual(compacted.turns, self.state.turns)
        self.assertEqual(compacted.context_generation, 1)
        self.assertEqual(
            compacted.compactions[0].through_turn_id, self.state.turns[1].id
        )
        messages = render_context(compacted)
        self.assertEqual(messages[0]["role"], "user")
        self.assertIn("historical data", messages[0]["content"])
        self.assertEqual(messages[1:], before[4:])
        info = context_info(compacted)
        self.assertEqual((info.covered_turns, info.retained_turns), (2, 1))
        self.assertLess(info.context_characters, info.history_characters)

    def test_same_result_is_idempotent_and_usage_is_counted_once(self):
        result = self.result()
        compacted = apply_compaction(self.state, result)
        self.assertIs(apply_compaction(compacted, result), compacted)
        self.assertEqual(compacted.compaction_usage.total_tokens, 20)
        self.assertEqual(
            compacted.usage.total_tokens, self.state.usage.total_tokens + 20
        )
        other = result.model_copy(update={"response": answer("不同摘要", tokens=20)})
        with self.assertRaises(SessionConflict):
            apply_compaction(compacted, other)

    def test_compaction_is_idle_only_and_does_not_split_a_tool_exchange(self):
        active = start_step(begin_turn(self.state, "read", request_key="m4"))
        for state in (
            active,
            accept_response(
                active,
                active.active_turn.steps[-1].id,
                ModelResponse(
                    model="m",
                    finish_reason="tool_calls",
                    message=AssistantOutput(
                        tool_calls=(
                            ToolCall(
                                id="read", name="read_file", arguments='{"path":"x"}'
                            ),
                        )
                    ),
                ),
            ),
        ):
            with self.assertRaises(SessionBusy):
                self.prepared(state)
        with self.assertRaises(SessionBusy):
            apply_compaction(active, self.result())

    def test_stale_or_cross_conversation_summary_cannot_replace_new_context(self):
        result = self.result()
        changed = add_turn(self.state, "新需求", "m4")
        with self.assertRaises(SessionConflict):
            apply_compaction(changed, result)
        with self.assertRaises(SessionError):
            apply_compaction(create_conversation("scope"), result)

    def test_boundaries_and_oversized_summaries_fail_without_changes(self):
        for count in (0, 4, True, "2"):
            with self.subTest(count=count), self.assertRaises(SessionError):
                self.prepared(count=count)
        with self.assertRaises(SessionError):
            apply_compaction(self.state, self.result(summary="x" * 10000))
        self.assertEqual(self.state.compactions, ())

    def test_recompaction_uses_previous_summary_and_only_new_turns(self):
        first = apply_compaction(self.state, self.result())
        request = self.prepared(first, count=3)
        material = json.loads(request.payload["messages"][-1]["content"])
        self.assertEqual(material["previous_summary"], first.compactions[0].summary)
        self.assertEqual(
            [t["turn_id"] for t in material["new_turns"]], [first.turns[2].id]
        )
        second = apply_compaction(
            first,
            CompactionResult(
                request=request,
                response=answer("Python；src/calc.py；square 平方；四空格。", 5),
            ),
        )
        self.assertEqual(second.context_generation, 2)
        self.assertEqual(second.compactions[1].parent_id, second.compactions[0].id)
        self.assertEqual(second.compaction_usage.total_tokens, 25)
        with self.assertRaises(SessionError):
            self.prepared(second, count=2)
        same_prefix = self.prepared(second, count=3)
        self.assertEqual(
            json.loads(same_prefix.payload["messages"][-1]["content"])["new_turns"], []
        )

    def test_snapshot_and_store_restore_the_actual_compacted_window(self):
        compacted = apply_compaction(self.state, self.result())
        restored = restore_snapshot(compacted.model_dump_json(), scope_id="scope")
        self.assertEqual(restored, compacted)
        self.assertEqual(render_context(restored), render_context(compacted))
        store = MemorySessionStore()
        store.create(self.state)
        store.save(compacted, expected_revision=self.state.revision)
        self.assertEqual(store.load("scope", self.state.id).context_generation, 1)
        legacy = self.state.model_dump()
        del legacy["compactions"]
        self.assertEqual(
            restore_snapshot(json.dumps(legacy), scope_id="scope"), self.state
        )

    def test_corrupted_checkpoint_or_source_history_is_rejected_on_restore(self):
        compacted = apply_compaction(self.state, self.result())
        for field, value in (
            ("generation", 2),
            ("parent_id", "unknown"),
            ("source_digest", "0" * 64),
            ("summary", " "),
            ("through_turn_count", 99),
        ):
            payload = compacted.model_dump()
            payload["compactions"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                restore_snapshot(json.dumps(payload), scope_id="scope")
        payload = compacted.model_dump()
        payload["turns"][0]["user_message"] = "tampered"
        with self.assertRaises(ValidationError):
            restore_snapshot(json.dumps(payload), scope_id="scope")

    def test_prepare_model_request_uses_summary_and_keeps_recent_tool_pairs(self):
        compacted = apply_compaction(self.state, self.result())
        state = start_step(begin_turn(compacted, "read", request_key="m4"))
        step_id = state.active_turn.steps[-1].id
        state = accept_response(
            state,
            step_id,
            ModelResponse(
                model="m",
                finish_reason="tool_calls",
                message=AssistantOutput(
                    tool_calls=(ToolCall(id="call", name="read_file", arguments="{}"),)
                ),
            ),
        )
        state = supply_tool_results(
            state, step_id, [ToolResult(tool_call_id="call", content="code")]
        )
        state = start_step(state)
        prepared = prepare_model_request(state, settings=self.settings)
        NativeRequest.model_validate(prepared.payload)
        self.assertEqual(prepared.context_generation, 1)
        self.assertEqual(prepared.context_through_turns, 2)
        self.assertIn(
            "Historical conversation summary",
            prepared.payload["messages"][1]["content"],
        )
        self.assertNotIn("重复的背景材料", prepared.payload_json)
        self.assertEqual(prepared.payload["messages"][-1]["tool_call_id"], "call")

    def test_summary_input_excludes_opaque_continuation_metadata(self):
        state = start_step(
            begin_turn(create_conversation("scope"), "原文" * 300, request_key="m1")
        )
        state = accept_response(
            state,
            state.active_turn.steps[-1].id,
            ModelResponse(
                model="m",
                finish_reason="stop",
                message=AssistantOutput(
                    content="done",
                    continuation_json='{"reasoning_details":[{"data":"opaque-secret"}]}',
                ),
            ),
        )
        prepared = self.prepared(state, count=1)
        self.assertNotIn("opaque-secret", prepared.payload_json)
        self.assertNotIn("private-key", prepared.model_dump_json())
        self.assertNotIn("tools", prepared.payload)

    async def test_call_compaction_is_one_request_and_does_not_adopt_implicitly(self):
        calls = []

        def transport(request):
            calls.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "model": "test/model",
                    "choices": [
                        {
                            "message": {"content": "Python，src/calc.py，square。"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"total_tokens": 20},
                },
            )

        result = await call_compaction(
            self.prepared(),
            client=LLMClient(self.settings, transport=httpx.MockTransport(transport)),
        )
        self.assertEqual(len(calls), 1)
        self.assertNotIn("tools", calls[0])
        self.assertEqual(self.state.compactions, ())
        self.assertEqual(
            apply_compaction(self.state, result).compactions[0].summary,
            "Python，src/calc.py，square。",
        )

    async def test_partial_or_tool_output_and_network_failure_do_not_adopt_or_retry(
        self,
    ):
        for status, message, reason in (
            (200, {"content": "partial"}, "length"),
            (
                200,
                {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "x",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        }
                    ],
                },
                "tool_calls",
            ),
            (429, {"content": "rate limited"}, "stop"),
        ):
            seen = []

            def transport(request):
                seen.append(request)
                return httpx.Response(
                    status,
                    json={
                        "model": "m",
                        "choices": [{"message": message, "finish_reason": reason}],
                    },
                )

            with (
                self.subTest(reason=reason, status=status),
                self.assertRaises(LLMError),
            ):
                await call_compaction(
                    self.prepared(),
                    client=LLMClient(
                        self.settings, transport=httpx.MockTransport(transport)
                    ),
                )
            self.assertEqual(len(seen), 1)
            self.assertEqual(self.state.compactions, ())

    async def test_restored_summary_can_continue_a_real_client_request(self):
        compacted = restore_snapshot(
            apply_compaction(self.state, self.result()).model_dump_json(),
            scope_id="scope",
        )
        state = start_step(begin_turn(compacted, "入口文件是什么？", request_key="m4"))
        bodies = []

        def transport(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "model": "m",
                    "choices": [
                        {"message": {"content": "src/calc.py"}, "finish_reason": "stop"}
                    ],
                },
            )

        prepared = prepare_model_request(state, settings=self.settings)
        response = await call_model(
            prepared,
            client=LLMClient(self.settings, transport=httpx.MockTransport(transport)),
        )
        final = accept_response(state, prepared.step_id, response)
        self.assertEqual(final.turns[-1].status, "completed")
        self.assertEqual(
            final.turns[-1].steps[-1].response.message.content, "src/calc.py"
        )
        self.assertNotIn("重复的背景材料", json.dumps(bodies))

    def test_restore_inflight_tail_interrupts_it_without_losing_summary(self):
        compacted = apply_compaction(self.state, self.result())
        state = start_step(begin_turn(compacted, "next", request_key="m4"))
        restored = restore_snapshot(state.model_dump_json(), scope_id="scope")
        self.assertEqual(restored.turns[-1].status, "interrupted")
        self.assertEqual(restored.compactions, compacted.compactions)
        self.assertEqual(restored.context_generation, 1)

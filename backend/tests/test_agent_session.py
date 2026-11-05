import json
import unittest

from pydantic import ValidationError

from app.agent import (
    accept_response,
    begin_turn,
    create_conversation,
    fail_step,
    interrupt_turn,
    pending_tool_calls,
    render_history,
    restore_snapshot,
    start_step,
    supply_tool_results,
)
from app.agent.session_types import (
    AssistantOutput,
    ConversationState,
    ModelResponse,
    SessionBusy,
    SessionConfig,
    SessionConflict,
    SessionError,
    StaleStep,
    TokenUsage,
    ToolCall,
    ToolResult,
)


def response(text="回答", *, calls=(), finish_reason=None, tokens=10):
    return ModelResponse(
        model="test/model",
        message=AssistantOutput(content=text, tool_calls=calls),
        finish_reason=finish_reason or ("tool_calls" if calls else "stop"),
        usage=TokenUsage(prompt_tokens=6, completion_tokens=4, total_tokens=tokens),
    )


CALLS = (
    ToolCall(id="call-a", name="read_file", arguments='{"path":"a.py"}'),
    ToolCall(id="call-b", name="read_history", arguments='{"conversation_id":"other"}'),
)


class AgentSessionTests(unittest.TestCase):
    def setUp(self):
        self.empty = create_conversation(
            "scope-1", config=SessionConfig(model="test/model")
        )
        self.ready = begin_turn(self.empty, "解释这段代码", request_key="message-1")
        self.waiting = start_step(self.ready, step_id="step-1")

    def waiting_tools(self):
        return accept_response(
            self.waiting, "step-1", response("先读取资料", calls=CALLS)
        )

    def test_one_user_turn_spans_multiple_model_calls_and_tool_results(self):
        state = self.waiting_tools()
        self.assertEqual(state.active_turn.status, "awaiting_tools")
        self.assertEqual(pending_tool_calls(state), CALLS)
        state = supply_tool_results(
            state, "step-1", [ToolResult(tool_call_id="call-b", content="历史内容")]
        )
        self.assertEqual(pending_tool_calls(state), (CALLS[0],))
        with self.assertRaises(SessionBusy):
            start_step(state)
        state = supply_tool_results(
            state,
            "step-1",
            [ToolResult(tool_call_id="call-a", content="    return 1\n")],
        )
        self.assertEqual(state.active_turn.status, "ready")
        self.assertEqual(pending_tool_calls(state), ())
        state = start_step(state, step_id="step-2")
        state = accept_response(state, "step-2", response("最终回答"))
        self.assertIsNone(state.active_turn)
        self.assertEqual(len(state.turns), 1)
        self.assertEqual(len(state.turns[0].steps), 2)
        history = render_history(state)
        self.assertEqual(
            [m["role"] for m in history],
            ["user", "assistant", "tool", "tool", "assistant"],
        )
        self.assertEqual(
            [m["tool_call_id"] for m in history if m["role"] == "tool"],
            ["call-a", "call-b"],
        )
        self.assertEqual(history[2]["content"], "    return 1\n")
        self.assertEqual(state.turns[0].steps[0].tool_results[0].tool_call_id, "call-b")
        self.assertEqual(history[-1]["content"], "最终回答")

    def test_next_turn_retains_history_and_new_sessions_are_independent(self):
        complete = accept_response(self.waiting, "step-1", response())
        next_turn = begin_turn(complete, "继续", request_key="message-2")
        self.assertEqual(len(next_turn.turns), 2)
        self.assertEqual(render_history(next_turn)[0]["content"], "解释这段代码")
        other = create_conversation("scope-1")
        self.assertNotEqual(other.id, next_turn.id)
        self.assertEqual(other.turns, ())

    def test_busy_input_is_rejected_and_user_retry_is_idempotent(self):
        self.assertIs(
            begin_turn(self.waiting, "解释这段代码", request_key="message-1"),
            self.waiting,
        )
        with self.assertRaises(SessionConflict):
            begin_turn(self.waiting, "different", request_key="message-1")
        with self.assertRaises(SessionBusy):
            begin_turn(self.waiting, "another", request_key="message-2")

    def test_step_retry_is_idempotent_but_ids_cannot_be_reused(self):
        self.assertIs(start_step(self.waiting, step_id="step-1"), self.waiting)
        with self.assertRaises(SessionBusy):
            start_step(self.waiting, step_id="step-2")
        complete = accept_response(self.waiting, "step-1", response())
        next_turn = begin_turn(complete, "next", request_key="next")
        with self.assertRaises(SessionConflict):
            start_step(next_turn, step_id="step-1")

    def test_response_replay_does_not_duplicate_usage_or_history(self):
        reply = response()
        complete = accept_response(self.waiting, "step-1", reply)
        self.assertIs(accept_response(complete, "step-1", reply), complete)
        self.assertEqual(complete.usage.total_tokens, 10)
        self.assertEqual(len(render_history(complete)), 2)
        with self.assertRaises(SessionConflict):
            accept_response(complete, "step-1", response("different"))
        with self.assertRaises(StaleStep):
            accept_response(complete, "wrong-step", reply)

    def test_tool_replay_conflicts_and_bad_batches_are_atomic(self):
        original = self.waiting_tools()
        result = ToolResult(tool_call_id="call-a", content="source")
        state = supply_tool_results(original, "step-1", [result])
        self.assertIs(supply_tool_results(state, "step-1", [result]), state)
        with self.assertRaises(SessionConflict):
            supply_tool_results(
                state, "step-1", [ToolResult(tool_call_id="call-a", content="changed")]
            )
        with self.assertRaises(SessionError):
            supply_tool_results(
                original,
                "step-1",
                [result, ToolResult(tool_call_id="unknown", content="x")],
            )
        self.assertEqual(original.turns[0].steps[0].tool_results, ())
        with self.assertRaises(SessionConflict):
            supply_tool_results(
                original,
                "step-1",
                [result, ToolResult(tool_call_id="call-a", content="changed")],
            )

    def test_error_tool_results_close_the_exchange_and_remain_errors(self):
        state = supply_tool_results(
            self.waiting_tools(),
            "step-1",
            [
                ToolResult(tool_call_id="call-a", content="not found", is_error=True),
                ToolResult(tool_call_id="call-b", content="history"),
            ],
        )
        self.assertEqual(state.active_turn.status, "ready")
        self.assertEqual(
            json.loads(render_history(state)[2]["content"])["status"], "error"
        )

    def test_assistant_commentary_with_calls_does_not_finish_the_turn(self):
        state = accept_response(
            self.waiting,
            "step-1",
            response("准备读取", calls=CALLS, finish_reason="stop"),
        )
        self.assertEqual(state.active_turn.status, "awaiting_tools")

    def test_truncation_and_content_filter_fail_without_exposing_calls(self):
        for reason, code in (
            ("length", "OUTPUT_TRUNCATED"),
            ("content_filter", "CONTENT_FILTERED"),
        ):
            with self.subTest(reason=reason):
                state = accept_response(
                    self.waiting,
                    "step-1",
                    response(None, calls=CALLS, finish_reason=reason),
                )
                self.assertEqual(state.turns[0].status, "failed")
                self.assertEqual(state.turns[0].steps[0].error_code, code)
                self.assertEqual(pending_tool_calls(state), ())
                self.assertEqual(state.usage.total_tokens, 10)

    def test_network_failure_and_late_response_do_not_restart_a_turn(self):
        failed = fail_step(self.waiting, "step-1", error_code="MODEL_TIMEOUT")
        self.assertEqual(failed.turns[0].status, "failed")
        self.assertIsNone(failed.usage.total_tokens)
        self.assertIs(fail_step(failed, "step-1", error_code="MODEL_TIMEOUT"), failed)
        with self.assertRaises(StaleStep):
            accept_response(failed, "step-1", response())
        self.assertEqual(
            begin_turn(
                failed, "retry explicitly", request_key="retry"
            ).active_turn.status,
            "ready",
        )

    def test_interrupting_pending_model_rejects_a_late_completion(self):
        interrupted = interrupt_turn(self.waiting, self.waiting.active_turn.id)
        self.assertIsNone(interrupted.active_turn)
        with self.assertRaises(StaleStep):
            accept_response(interrupted, "step-1", response())
        self.assertIs(interrupt_turn(interrupted, interrupted.turns[0].id), interrupted)

    def test_interrupted_tools_are_normalized_only_in_the_projection(self):
        state = supply_tool_results(
            self.waiting_tools(),
            "step-1",
            [ToolResult(tool_call_id="call-a", content="already received")],
        )
        interrupted = interrupt_turn(state, state.active_turn.id)
        history = render_history(interrupted)
        self.assertEqual(history[2]["content"], "already received")
        self.assertEqual(
            json.loads(history[3]["content"]),
            {"status": "aborted", "execution_state": "unknown"},
        )
        self.assertEqual(render_history(interrupted), history)
        self.assertEqual(len(interrupted.turns[0].steps[0].tool_results), 1)
        with self.assertRaises(StaleStep):
            supply_tool_results(
                interrupted,
                "step-1",
                [ToolResult(tool_call_id="call-b", content="late")],
            )

    def test_interrupt_before_and_after_a_tool_exchange(self):
        self.assertEqual(
            interrupt_turn(self.ready, self.ready.active_turn.id).turns[0].status,
            "interrupted",
        )
        ready_again = supply_tool_results(
            self.waiting_tools(),
            "step-1",
            [ToolResult(tool_call_id=c.id, content="x") for c in CALLS],
        )
        self.assertEqual(
            interrupt_turn(ready_again, ready_again.active_turn.id).turns[0].status,
            "interrupted",
        )

    def test_restore_completed_history_and_interrupt_inflight_state(self):
        complete = accept_response(self.waiting, "step-1", response())
        self.assertEqual(
            restore_snapshot(complete.model_dump_json(), scope_id="scope-1"), complete
        )
        restored = restore_snapshot(
            self.waiting_tools().model_dump_json(), scope_id="scope-1"
        )
        self.assertEqual(restored.turns[0].status, "interrupted")
        self.assertEqual(restored.revision, self.waiting_tools().revision + 1)
        self.assertEqual(pending_tool_calls(restored), ())
        with self.assertRaises(SessionError):
            restore_snapshot(complete.model_dump_json(), scope_id="someone-else")

    def test_snapshot_preserves_opaque_provider_metadata_without_displaying_it(self):
        reply = ModelResponse(
            model="test/model",
            finish_reason="stop",
            message=AssistantOutput(
                content="回答", continuation_json='{"opaque":"value"}'
            ),
        )
        state = accept_response(self.waiting, "step-1", reply)
        restored = restore_snapshot(state.model_dump_json(), scope_id="scope-1")
        self.assertEqual(
            restored.turns[0].steps[0].response.message.continuation_json,
            '{"opaque":"value"}',
        )
        self.assertNotIn("opaque", json.dumps(render_history(restored)))

    def test_unknown_usage_is_not_reported_as_zero_or_a_complete_total(self):
        complete = accept_response(
            self.waiting,
            "step-1",
            ModelResponse(
                model="test/model",
                finish_reason="stop",
                message=AssistantOutput(content="answer"),
            ),
        )
        self.assertIsNone(complete.usage.total_tokens)

    def test_empty_truncated_output_is_retained_but_not_an_invalid_api_message(self):
        failed = accept_response(
            self.waiting, "step-1", response(None, finish_reason="length")
        )
        self.assertIsNotNone(failed.turns[0].steps[0].response)
        self.assertEqual(
            render_history(failed), [{"role": "user", "content": "解释这段代码"}]
        )

    def test_invalid_native_shapes_and_corrupted_snapshots_are_rejected(self):
        for arguments in ("[]", "bad", '{"x":NaN}', '{"x":1,"x":2}'):
            with self.subTest(arguments=arguments), self.assertRaises(ValidationError):
                ToolCall(id="call", name="read_file", arguments=arguments)
        with self.assertRaises(ValidationError):
            ModelResponse(
                model="m", finish_reason="tool_calls", message=AssistantOutput()
            )
        with self.assertRaises(ValidationError):
            AssistantOutput(tool_calls=(CALLS[0], CALLS[0]))
        payload = self.waiting_tools().model_dump()
        payload["turns"][0]["status"] = "completed"
        with self.assertRaises(ValidationError):
            ConversationState.model_validate(payload)

    def test_input_state_is_immutable_and_operations_do_not_edit_older_snapshots(self):
        self.assertEqual(self.empty.turns, ())
        self.assertEqual(self.ready.turns[0].steps, ())
        with self.assertRaises(ValidationError):
            self.waiting.revision = 999
        with self.assertRaises(ValidationError):
            self.waiting.config.model = "changed"

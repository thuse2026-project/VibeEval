"""Window projection and budget planning use retained, versioned native results."""

import json
import unittest

from app.agent import (
    ToolRegistry,
    accept_response,
    apply_compaction,
    begin_turn,
    create_conversation,
    prepare_compaction,
    render_context,
    start_step,
    supply_tool_results,
)
from app.agent.compaction import CompactionResult
from app.agent.model import NativeRequest
from app.agent.session_types import (
    AssistantOutput,
    ModelResponse,
    SessionBusy,
    ToolCall,
    ToolResult,
)
from app.agent_integration.materials import SourceSnapshot, encoded, fragment
from app.agent_integration.window import (
    ContextCapacityError,
    WindowPolicy,
    adopt_window_compaction,
    inspect_window,
    plan_compaction,
    prepare_window_request,
    project_materials,
)
from app.llm.config import LLMSettings

SETTINGS = LLMSettings("https://fixture.test/v1", "never-emit-key", "fixture/model")
REGISTRY = ToolRegistry()


def add_result(
    state,
    text,
    index,
    *,
    material_id="material-A",
    version="1",
    name="read_file",
    error=False,
):
    state = start_step(state, step_id="window-step-" + str(index))
    call = ToolCall(id="window-call-" + str(index), name=name, arguments="{}")
    state = accept_response(
        state,
        state.active_turn.steps[-1].id,
        ModelResponse(
            model="fixture",
            finish_reason="tool_calls",
            message=AssistantOutput(tool_calls=(call,)),
        ),
    )
    body = {
        "status": "ok",
        "material_id": material_id,
        **fragment(
            SourceSnapshot("workspace_file", "src/main.py", version, text),
            max_chars=16000,
        ),
    }
    return supply_tool_results(
        state,
        state.active_turn.steps[-1].id,
        [ToolResult(tool_call_id=call.id, content=encoded(body), is_error=error)],
    )


def compact(state, steps):
    request = prepare_compaction(
        state, through_turn=0, through_step_count=steps, settings=SETTINGS
    )
    return apply_compaction(
        state,
        CompactionResult(
            request=request,
            response=ModelResponse(
                model="fixture",
                finish_reason="stop",
                message=AssistantOutput(content="旧文件已读，材料仍可取回。"),
            ),
        ),
    )


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.state = begin_turn(
            create_conversation("scope"), "当前请求保留。", request_key="window"
        )

    def inspect(self, state, **kwargs):
        return inspect_window(state, tools=REGISTRY, settings=SETTINGS, **kwargs)

    def test_duplicate_fragment_is_locator_only_while_full_fragment_remains_in_window(
        self,
    ):
        state = add_result(self.state, "exact material " * 100, 0)
        state = add_result(state, "exact material " * 100, 1)
        raw = state.model_dump_json()
        report = self.inspect(state)
        self.assertEqual(
            [item["window_mode"] for item in report["materials"]],
            ["full_material", "locator"],
        )
        pending = start_step(state, step_id="model-next")
        prepared, manifest = prepare_window_request(
            pending, tools=REGISTRY, settings=SETTINGS
        )
        NativeRequest.model_validate(prepared.payload)
        tool_messages = [
            item for item in prepared.payload["messages"] if item["role"] == "tool"
        ]
        self.assertEqual(
            [m["tool_call_id"] for m in tool_messages],
            ["window-call-0", "window-call-1"],
        )
        self.assertIn("exact material", tool_messages[0]["content"])
        locator = json.loads(tool_messages[1]["content"])
        self.assertEqual(locator["window_projection"], "duplicate_locator")
        self.assertNotIn("text", locator)
        self.assertEqual(manifest["payload_sha256"], prepared.payload_sha256)
        self.assertEqual(manifest["stage"], "prepared")
        self.assertEqual(state.model_dump_json(), raw)
        self.assertNotIn("never-emit-key", encoded(manifest))

    def test_compacted_source_cannot_be_used_as_a_duplicate_body_still_in_window(self):
        state = add_result(self.state, "same file " * 200, 0)
        state = add_result(state, "same file " * 200, 1)
        state = add_result(
            state, "latest other file " * 100, 2, material_id="material-B"
        )
        state = compact(state, 1)
        report = self.inspect(state)
        self.assertEqual(
            [item["window_mode"] for item in report["materials"]],
            ["compacted", "full_material", "full_material"],
        )
        prepared, _ = prepare_window_request(
            start_step(state, step_id="next"), tools=REGISTRY, settings=SETTINGS
        )
        messages = [m for m in prepared.payload["messages"] if m["role"] == "tool"]
        self.assertEqual(json.loads(messages[0]["content"])["text"], "same file " * 200)
        self.assertEqual(
            render_context(state)[1]["content"], self.state.active_turn.user_message
        )

    def test_old_large_read_body_has_explicit_preview_and_latest_exchange_is_protected(
        self,
    ):
        state = add_result(self.state, "中😀x" * 1000, 0)
        state = add_result(state, "latest result " * 400, 1, material_id="material-B")
        policy = WindowPolicy(max_tool_body_chars=1500, preview_chars=50)
        projected = project_materials(render_context(state), policy)
        tool = [
            json.loads(item["content"]) for item in projected if item["role"] == "tool"
        ]
        self.assertEqual(tool[0]["window_projection"], "material_preview")
        self.assertEqual(len(tool[0]["text"]), 50)
        self.assertEqual(tool[0]["next_offset"], 50)
        self.assertEqual(tool[0]["end_offset"], 50)
        self.assertTrue(tool[0]["has_more"])
        self.assertEqual(tool[1]["text"], "latest result " * 400)
        self.assertNotIn("window_projection", tool[1])
        report = self.inspect(state, policy=policy)
        self.assertEqual(
            [m["window_mode"] for m in report["materials"]],
            ["preview", "full_material"],
        )
        self.assertNotEqual(
            report["materials"][0]["original_returned_sha256"],
            report["materials"][0]["visible_sha256"],
        )
        self.assertEqual(
            len(json.loads(state.turns[0].steps[0].tool_results[0].content)["text"]),
            3000,
        )

    def test_versions_non_read_results_and_errors_are_not_reinterpreted_as_duplicate_reads(
        self,
    ):
        state = add_result(self.state, "same text " * 100, 0)
        state = add_result(
            state, "same text " * 100, 1, material_id="material-B", version="2"
        )
        state = add_result(state, "same text " * 100, 2, name="run_command")
        state = add_result(state, "same text " * 100, 3, error=True)
        source = render_context(state)
        projected = project_materials(source)
        self.assertEqual(projected, source)
        report = self.inspect(state)
        self.assertEqual(len(report["materials"]), 2)
        self.assertEqual([m["source_version"] for m in report["materials"]], ["1", "2"])

    def test_budget_includes_native_envelope_and_uses_named_heuristic(self):
        report = self.inspect(self.state)
        self.assertEqual(
            report["budget"]["estimate_method"], "utf8_bytes_div_3_heuristic_v1"
        )
        self.assertGreater(report["budget"]["estimated_input_tokens"], 0)
        self.assertEqual(
            report["budget"]["reserved_output_tokens"], SETTINGS.max_output_tokens
        )
        policy = WindowPolicy(soft_input_tokens=1, hard_input_tokens=2)
        report = self.inspect(self.state, policy=policy)
        self.assertTrue(report["budget"]["over_soft_limit"])
        self.assertTrue(report["budget"]["over_hard_limit"])
        with self.assertRaises(ContextCapacityError) as raised:
            prepare_window_request(
                start_step(self.state), tools=REGISTRY, settings=SETTINGS, policy=policy
            )
        self.assertEqual(raised.exception.report["conversation_id"], self.state.id)
        self.assertIsNone(
            plan_compaction(
                self.state, tools=REGISTRY, settings=SETTINGS, policy=policy
            )
        )

    def test_threshold_plan_selects_safe_active_prefix_without_calling_or_adopting(
        self,
    ):
        state = self.state
        for index in range(4):
            state = add_result(
                state,
                ("material " + str(index)) * 100,
                index,
                material_id="material-" + str(index),
            )
        self.assertIsNone(plan_compaction(state, tools=REGISTRY, settings=SETTINGS))
        policy = WindowPolicy(soft_input_tokens=1)
        request = plan_compaction(
            state, tools=REGISTRY, settings=SETTINGS, policy=policy
        )
        self.assertEqual(
            (request.through_turn_count, request.through_step_count), (0, 3)
        )
        self.assertEqual(state.compactions, ())
        self.assertNotIn("window-call-3", request.payload_json)
        adopted = apply_compaction(
            state,
            CompactionResult(
                request=request,
                response=ModelResponse(
                    model="fixture",
                    finish_reason="stop",
                    message=AssistantOutput(content="旧材料摘要与定位。"),
                ),
            ),
        )
        self.assertEqual(adopted.turns, state.turns)
        self.assertLess(
            self.inspect(adopted)["budget"]["serialized_characters"],
            self.inspect(state)["budget"]["serialized_characters"],
        )
        with self.assertRaises(SessionBusy):
            plan_compaction(
                start_step(state), tools=REGISTRY, settings=SETTINGS, policy=policy
            )

    def test_request_capacity_is_preflighted_before_native_message_count_limit(self):
        state = self.state
        for index in range(130):
            state = add_result(state, "short", index, material_id="id-" + str(index))
        report = self.inspect(state)
        self.assertFalse(report["native_history_valid"])
        self.assertTrue(report["budget"]["over_hard_limit"])
        self.assertGreater(report["budget"]["message_count"], 256)
        policy = WindowPolicy(max_candidate_checks=3)
        request = plan_compaction(
            state, tools=REGISTRY, settings=SETTINGS, policy=policy
        )
        self.assertLessEqual(request.through_step_count, 3)
        self.assertEqual(state.compactions, ())

    def test_planner_chunks_large_history_without_splitting_tool_exchange(self):
        state = self.state
        # A non-material tool cannot be silently externalized by the read adapter.
        for index in range(6):
            state = start_step(state, step_id="raw-" + str(index))
            call = ToolCall(
                id="raw-call-" + str(index), name="run_command", arguments="{}"
            )
            state = accept_response(
                state,
                state.active_turn.steps[-1].id,
                ModelResponse(
                    model="fixture",
                    finish_reason="tool_calls",
                    message=AssistantOutput(tool_calls=(call,)),
                ),
            )
            state = supply_tool_results(
                state,
                state.active_turn.steps[-1].id,
                [ToolResult(tool_call_id=call.id, content="x" * 16000)],
            )
        request = plan_compaction(state, tools=REGISTRY, settings=SETTINGS)
        self.assertIsNotNone(request)
        self.assertLess(request.through_step_count, 5)
        self.assertLessEqual(len(request.payload_json), 60000)
        self.assertEqual(
            len(
                json.loads(request.payload["messages"][-1]["content"])["new_turns"][0][
                    "step_ids"
                ]
            ),
            request.through_step_count,
        )

    def test_invalid_limits_and_pending_history_do_not_become_sendable_payloads(self):
        for options in (
            {"soft_input_tokens": 20000},
            {"preview_chars": 12000},
            {"hard_request_chars": 64001},
        ):
            with self.assertRaises(ValueError):
                WindowPolicy(**options)
        state = start_step(self.state)
        state = accept_response(
            state,
            state.active_turn.steps[-1].id,
            ModelResponse(
                model="fixture",
                finish_reason="tool_calls",
                message=AssistantOutput(
                    tool_calls=(
                        ToolCall(id="unresolved", name="read_file", arguments="{}"),
                    )
                ),
            ),
        )
        self.assertFalse(self.inspect(state)["native_history_valid"])
        with self.assertRaises(SessionBusy):
            prepare_window_request(state, tools=REGISTRY, settings=SETTINGS)

    def test_output_and_safety_reserves_reduce_available_input_capacity(self):
        policy = WindowPolicy(context_window_tokens=1536)
        report = self.inspect(self.state, policy=policy)
        self.assertEqual(report["budget"]["available_input_tokens"], 0)
        self.assertEqual(report["budget"]["effective_hard_input_tokens"], 0)
        self.assertTrue(report["budget"]["over_hard_limit"])
        with self.assertRaises(ContextCapacityError):
            prepare_window_request(
                start_step(self.state), tools=REGISTRY, settings=SETTINGS, policy=policy
            )

    def test_adoption_must_shrink_projected_window_even_if_raw_history_would_shrink(
        self,
    ):
        state = self.state
        for index in range(4):
            state = add_result(state, "same " * 800, index)
        request = prepare_compaction(
            state, through_turn=0, through_step_count=3, settings=SETTINGS
        )
        long_summary = CompactionResult(
            request=request,
            response=ModelResponse(
                model="fixture",
                finish_reason="stop",
                message=AssistantOutput(content="s" * 5000),
            ),
        )
        # Raw retained tool bodies alone would make this look like a reduction.
        self.assertLess(
            len(encoded(render_context(apply_compaction(state, long_summary)))),
            len(encoded(render_context(state))),
        )
        with self.assertRaisesRegex(ValueError, "does not reduce"):
            adopt_window_compaction(
                state, long_summary, tools=REGISTRY, settings=SETTINGS
            )
        short_summary = CompactionResult(
            request=request,
            response=ModelResponse(
                model="fixture",
                finish_reason="stop",
                message=AssistantOutput(content="旧材料保留，继续当前任务。"),
            ),
        )
        updated = adopt_window_compaction(
            state, short_summary, tools=REGISTRY, settings=SETTINGS
        )
        self.assertIs(
            adopt_window_compaction(
                updated, short_summary, tools=REGISTRY, settings=SETTINGS
            ),
            updated,
        )
        self.assertEqual(updated.turns, state.turns)

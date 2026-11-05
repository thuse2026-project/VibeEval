"""Integration checks for application wiring, not another agent state machine."""

import json
import unittest

from app.agent import (
    MemorySessionStore,
    begin_turn,
    create_conversation,
    interrupt_turn,
)
from app.agent.session_types import SessionConfig, StaleStep, ToolCall
from app.llm.config import LLMError, LLMSettings
from examples.agent_handoff import (
    fixture_source,
    read_conversation_result,
    run_demo,
    run_one_step,
    tool_registry,
)


class AgentHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_wiring_handles_partial_results_compaction_and_new_store(self):
        report = await run_demo()
        self.assertEqual(report["api_calls"], 4)
        self.assertEqual(report["pending_after_first_result"], ["file-1"])
        self.assertFalse(report["history_error"])
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["follow_up_status"], "completed")
        self.assertEqual(report["restored_context"]["generation"], 1)
        self.assertLess(
            report["restored_context"]["context_characters"],
            report["restored_context"]["history_characters"],
        )

    async def test_denied_and_interrupted_scenarios_do_not_continue_implicitly(self):
        denied = await run_demo("denied")
        self.assertTrue(denied["history_error"])
        self.assertEqual(denied["api_calls"], 2)
        self.assertIn("无法确认", denied["answer"])
        interrupted = await run_demo("interrupted")
        self.assertEqual(interrupted["api_calls"], 1)
        self.assertEqual(interrupted["status"], "interrupted")
        self.assertEqual(interrupted["pending_after_restore"], 0)

    def test_source_reader_uses_trusted_scope_even_with_target_on_allowlist(self):
        store = MemorySessionStore()
        source = fixture_source("other-scope")
        store.create(source)
        call = ToolCall(
            id="read",
            name="read_conversation",
            arguments=json.dumps(
                {
                    "conversation_id": source.id,
                    "first_turn": 1,
                    "max_turns": 1,
                }
            ),
        )
        unavailable = read_conversation_result(
            store, "caller-scope", frozenset((source.id,)), call, tool_registry()
        )
        self.assertTrue(unavailable.is_error)
        self.assertNotIn("题目背景", unavailable.content)
        selected = read_conversation_result(
            store, "other-scope", frozenset((source.id,)), call, tool_registry()
        )
        data = json.loads(selected.content)
        self.assertEqual(data["turn_ids"], [source.turns[0].id])
        self.assertTrue(data["has_more"])
        self.assertEqual(data["source_revision"], source.revision)

    async def test_late_response_cannot_overwrite_interruption_saved_during_request(
        self,
    ):
        store = MemorySessionStore()
        settings = LLMSettings(
            base_url="https://fixture.invalid/v1",
            api_key="fixture-only",
            model="fixture-model",
        )
        state = create_conversation("scope", config=SessionConfig(model=settings.model))
        state = begin_turn(state, "question", request_key="m1")
        store.create(state)

        class InterruptedClient:
            calls = 0

            async def complete(self, payload):
                self.calls += 1
                current = store.load("scope", state.id)
                store.save(
                    interrupt_turn(current, current.active_turn.id),
                    expected_revision=current.revision,
                )
                return {
                    "choices": [
                        {"message": {"content": "late"}, "finish_reason": "stop"}
                    ]
                }

        client = InterruptedClient()
        with self.assertRaises(StaleStep):
            await run_one_step(
                store,
                "scope",
                state.id,
                tools=tool_registry(),
                settings=settings,
                client=client,
            )
        final = store.load("scope", state.id)
        self.assertEqual(client.calls, 1)
        self.assertEqual(final.turns[-1].status, "interrupted")
        self.assertIsNone(final.turns[-1].steps[-1].response)

    async def test_client_failure_is_recorded_without_retrying(self):
        store = MemorySessionStore()
        settings = LLMSettings(
            base_url="https://fixture.invalid/v1",
            api_key="fixture-only",
            model="fixture-model",
        )
        state = begin_turn(create_conversation("scope"), "question", request_key="m1")
        store.create(state)

        class FailedClient:
            calls = 0

            async def complete(self, payload):
                self.calls += 1
                raise LLMError(429, "rate limited")

        client = FailedClient()
        with self.assertRaises(LLMError):
            await run_one_step(
                store,
                "scope",
                state.id,
                tools=tool_registry(),
                settings=settings,
                client=client,
            )
        final = store.load("scope", state.id)
        self.assertEqual(client.calls, 1)
        self.assertEqual(final.turns[-1].status, "failed")
        self.assertEqual(final.turns[-1].steps[-1].error_code, "MODEL_CALL_FAILED")

"""Real storage and HTTP checks for active checkpoints and window inspection."""

import asyncio
import json
import os
import unittest
from unittest.mock import patch

import httpx
import test_agent_integration_reading as reading_tests
import test_db_postgres as database_tests
from fastapi.testclient import TestClient

from app.agent import (
    accept_response,
    apply_compaction,
    call_compaction,
    call_model,
    create_conversation,
    prepare_compaction,
    render_context,
    start_step,
)
from app.agent.compaction import CompactionResult
from app.agent.session_types import (
    AssistantOutput,
    ModelResponse,
    SessionConfig,
    SessionConflict,
)
from app.agent_integration.reading_types import ReadConversation
from app.agent_integration.sources import SourceReader
from app.agent_integration.store import PostgresSessionStore
from app.agent_integration.window import (
    WindowPolicy,
    adopt_window_compaction,
    inspect_window,
    plan_compaction,
    prepare_window_request,
)
from app.llm.client import LLMClient
from app.llm.config import LLMError, LLMSettings
from app.main import create_app

SETTINGS = LLMSettings(
    "https://fixture.test/v1", "context-private-key", "fixture/model"
)


@unittest.skipUnless(
    os.environ.get("VIBEEVAL_RUN_DB_TESTS") == "1", "Requires isolated PostgreSQL"
)
class AgentIntegrationContextTests(unittest.TestCase):
    setUpClass = classmethod(database_tests.PostgreSQLTests.setUpClass.__func__)
    drop_test_database = classmethod(
        database_tests.PostgreSQLTests.drop_test_database.__func__
    )
    setUp = reading_tests.AgentIntegrationReadingTests.setUp
    save = reading_tests.AgentIntegrationReadingTests.save
    pending = reading_tests.AgentIntegrationReadingTests.pending
    execute = reading_tests.AgentIntegrationReadingTests.execute

    def fill(self):
        self.workspace.contents["src/main.py"] = "原始正文标记" * 400
        materials = []
        for _ in range(4):
            _, body = self.execute("read_file", {"path": "src/main.py"})
            materials.append(body["material_id"])
        return self.conversations.load(self.scope, self.initial.id), materials

    def adopt(self, state):
        request = prepare_compaction(
            state, through_turn=0, through_step_count=2, settings=SETTINGS
        )
        summary = "已读 src/main.py 版本 1，原文可按材料 ID 取回；当前任务仍需继续。"
        result = CompactionResult(
            request=request,
            response=ModelResponse(
                model="fixture",
                finish_reason="stop",
                message=AssistantOutput(content=summary),
            ),
        )
        return self.save(state, apply_compaction(state, result))

    def test_active_checkpoint_survives_store_restart_and_old_material_can_be_read_again(
        self,
    ):
        state, material_ids = self.fill()
        raw = state.turns
        state = self.adopt(state)
        loaded = PostgresSessionStore(self.factory).load(self.scope, state.id)
        self.assertEqual(loaded, state)
        self.assertEqual(loaded.active_turn.status, "ready")
        self.assertEqual(loaded.turns, raw)
        self.assertEqual(render_context(loaded), render_context(state))
        report = inspect_window(loaded, tools=self.tools.registry, settings=SETTINGS)
        self.assertEqual(
            [m["window_mode"] for m in report["materials"]],
            ["compacted", "compacted", "full_material", "locator"],
        )
        self.workspace.contents["src/main.py"] = "live file has changed"
        self.workspace.version = 2
        _, reread = self.execute("read_material", {"material_id": material_ids[0]})
        self.assertIn("原始正文标记", reread["text"])
        self.assertEqual(reread["source_version"], "1")
        view = SourceReader(self.materials).conversation(
            self.scope,
            ReadConversation(conversation_id=state.id, view="summary"),
        )
        self.assertEqual(json.loads(view.text)["covered_steps"], 2)

    def test_window_http_is_preview_not_a_model_call_and_redacts_body_prompt_and_key(
        self,
    ):
        private = create_conversation(
            self.scope,
            config=SessionConfig(
                model="fixture/model", system_prompt="server-private-prompt"
            ),
        )
        self.conversations.create(private)
        self.initial = private
        state, _ = self.fill()
        self.adopt(state)
        app = create_app(
            {}, entry_store=self.entry, agent_enabled=True, agent_reading=self.reader
        )
        prefix = f"/api/v1/sessions/{self.scope}/agent/conversations/{self.initial.id}/context"
        with (
            patch("app.agent_integration.router.load_settings", return_value=SETTINGS),
            TestClient(
                app,
                headers={"X-VibeEval-Request": "1"},
                cookies={"vibeeval_candidate": self.browser},
            ) as client,
        ):
            response = client.get(prefix + "?limit=2")
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(body["stage"], "preview")
            self.assertEqual(body["turn_status"], "ready")
            self.assertEqual(body["through_step_count"], 2)
            self.assertEqual(body["material_count"], 4)
            self.assertTrue(body["has_more"])
            self.assertEqual(len(body["materials"]), 2)
            for private_value in (
                "server-private-prompt",
                "context-private-key",
                "原始正文标记",
            ):
                self.assertNotIn(private_value, response.text)
            self.assertEqual(
                client.get(prefix + "?offset=2").json()["materials"][0]["window_mode"],
                "full_material",
            )
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(client.get(prefix + "?limit=101").status_code, 422)
            self.assertEqual(
                client.post(prefix, json={"tool_results": []}).status_code, 405
            )
        with TestClient(app, headers={"X-VibeEval-Request": "1"}) as client:
            self.assertEqual(client.get(prefix).status_code, 404)
        self.assertEqual(
            self.conversations.load(self.scope, state.id).revision, state.revision + 1
        )

    def test_compaction_call_then_model_continuation_uses_one_preserved_native_protocol(
        self,
    ):
        state, _ = self.fill()
        bodies = []

        def transport(request):
            body = json.loads(request.content)
            bodies.append(body)
            text = (
                "旧代码材料已保留，可按 material_id 重新读取。"
                if len(bodies) == 1
                else "根据保留的最近工具结果回答。"
            )
            return httpx.Response(
                200,
                json={
                    "model": "fixture/model",
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": text},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"total_tokens": 10},
                },
            )

        async def run():
            client = LLMClient(SETTINGS, transport=httpx.MockTransport(transport))
            request = plan_compaction(
                state,
                tools=self.tools.registry,
                settings=SETTINGS,
                policy=WindowPolicy(soft_input_tokens=1),
            )
            result = await call_compaction(request, client=client)
            compacted = self.save(
                state,
                adopt_window_compaction(
                    state, result, tools=self.tools.registry, settings=SETTINGS
                ),
            )
            pending = self.save(
                compacted, start_step(compacted, step_id="after-compaction")
            )
            prepared, report = prepare_window_request(
                pending, tools=self.tools.registry, settings=SETTINGS
            )
            response = await call_model(
                prepared, tools=self.tools.registry, client=client
            )
            return (
                self.save(
                    pending, accept_response(pending, prepared.step_id, response)
                ),
                report,
            )

        final, report = asyncio.run(run())
        self.assertEqual(len(bodies), 2)
        self.assertNotIn("tools", bodies[0])
        self.assertEqual(bodies[1]["tools"], self.tools.registry.specifications())
        self.assertEqual(final.turns[-1].status, "completed")
        self.assertEqual(final.compaction_usage.total_tokens, 10)
        self.assertEqual(report["stage"], "prepared")
        self.assertIn("原始正文标记", bodies[1]["messages"][-1]["content"])

    def test_failed_or_stale_compaction_does_not_replace_persisted_window(self):
        state, _ = self.fill()
        request = prepare_compaction(
            state, through_turn=0, through_step_count=2, settings=SETTINGS
        )
        calls = []

        def transport(http_request):
            calls.append(http_request)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": "truncated summary"},
                            "finish_reason": "length",
                        }
                    ]
                },
            )

        with self.assertRaises(LLMError):
            asyncio.run(
                call_compaction(
                    request,
                    client=LLMClient(
                        SETTINGS, transport=httpx.MockTransport(transport)
                    ),
                )
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.conversations.load(self.scope, state.id), state)
        result = CompactionResult(
            request=request,
            response=ModelResponse(
                model="fixture",
                finish_reason="stop",
                message=AssistantOutput(content="旧摘要"),
            ),
        )
        self.execute("read_file", {"path": "src/main.py"})
        changed = self.conversations.load(self.scope, state.id)
        with self.assertRaises(SessionConflict):
            apply_compaction(changed, result)
        self.assertEqual(self.conversations.load(self.scope, state.id).compactions, ())

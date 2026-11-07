"""Real PostgreSQL runtime, native provider protocol and recovery checks."""

import asyncio
import json
import os
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

import httpx
import test_agent_integration_reading as reading_tests
import test_db_postgres as database_tests
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agent import accept_response, begin_turn, create_conversation, start_step
from app.agent.session_types import AssistantOutput, ModelResponse, SessionConflict
from app.agent_integration.runtime import RuntimeCoordinator, runtime_registry
from app.agent_integration.runtime_types import (
    CompactInput,
    LeaseLost,
    RunBusy,
    RunInput,
    RunPolicy,
)
from app.agent_integration.store import ConversationNotFound
from app.agent_integration.window import WindowPolicy
from app.db.models import AgentAttempt, AgentReadObservation, AgentRun, ExamSession
from app.llm.client import LLMClient
from app.llm.config import LLMSettings
from app.main import create_app

SETTINGS = LLMSettings(
    "https://fixture.test/v1", "runtime-fixture-secret", "fixture/model"
)


def completion(text="回答完成", *, calls=(), reason=None, usage=10):
    message = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = [
            {
                "id": "call-" + str(index),
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            }
            for index, (name, args) in enumerate(calls)
        ]
    result = {
        "model": "fixture/model",
        "choices": [
            {
                "message": message,
                "finish_reason": reason or ("tool_calls" if calls else "stop"),
            }
        ],
    }
    if usage is not None:
        result["usage"] = {"total_tokens": usage}
    return result


READ = completion(calls=(("read_file", {"path": "src/main.py"}),))


@unittest.skipUnless(
    os.environ.get("VIBEEVAL_RUN_DB_TESTS") == "1", "Requires isolated PostgreSQL"
)
class AgentIntegrationRuntimeTests(unittest.TestCase):
    setUpClass = classmethod(database_tests.PostgreSQLTests.setUpClass.__func__)
    drop_test_database = classmethod(
        database_tests.PostgreSQLTests.drop_test_database.__func__
    )
    setUp = reading_tests.AgentIntegrationReadingTests.setUp
    save = reading_tests.AgentIntegrationReadingTests.save

    def coordinator(
        self, responses, *, policy=None, window=None, clock=None, callback=None
    ):
        self.requests = []
        queue = list(responses)

        def transport(request):
            self.requests.append(json.loads(request.content))
            if callback:
                callback(len(self.requests))
            if not queue:
                raise AssertionError("unexpected extra provider request")
            value = queue.pop(0)
            if isinstance(value, Exception):
                raise value
            return (
                httpx.Response(value[0], json=value[1])
                if isinstance(value, tuple)
                else httpx.Response(200, json=value)
            )

        return RuntimeCoordinator(
            self.reader,
            settings=SETTINGS,
            client=LLMClient(SETTINGS, transport=httpx.MockTransport(transport)),
            policy=policy,
            window_policy=window,
            clock=clock,
        )

    def submit(self, service, **kwargs):
        return service.submit(
            self.scope,
            self.initial.id,
            RunInput(request_key="runtime-request", text="解释代码", **kwargs),
        )

    def advance(self, service, run):
        return asyncio.run(service.advance(self.scope, self.initial.id, run["run_id"]))

    def historical_turn(self):
        state = self.conversations.load(self.scope, self.initial.id)
        ready = self.save(
            state, begin_turn(state, "需要摘要的背景。" * 500, request_key="history")
        )
        pending = self.save(ready, start_step(ready))
        return self.save(
            pending,
            accept_response(
                pending,
                pending.active_turn.steps[-1].id,
                ModelResponse(
                    model="fixture",
                    finish_reason="stop",
                    message=AssistantOutput(content="历史回答。"),
                ),
            ),
        )

    def test_read_only_loop_persists_each_attempt_manifest_and_native_response(self):
        service = self.coordinator([READ, completion("已读取入口代码。")])
        run = self.submit(service)
        self.assertEqual(run["status"], "queued")
        self.assertEqual(self.requests, [])
        done = self.advance(service, run)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(
            (done["model_calls"], done["tool_calls"], done["total_tokens"]), (2, 1, 20)
        )
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[1]["messages"][-1]["role"], "tool")
        self.assertIn("needle", self.requests[1]["messages"][-1]["content"])
        attempts = service.store.attempts(self.scope, self.initial.id, run["run_id"])[
            "items"
        ]
        self.assertEqual([a["status"] for a in attempts], ["adopted", "adopted"])
        self.assertEqual(attempts[1]["manifest"]["stage"], "dispatched")
        self.assertEqual(
            attempts[1]["manifest"]["materials"][0]["window_mode"], "full_material"
        )
        self.assertNotIn("runtime-fixture-secret", json.dumps(attempts))
        self.assertEqual(
            self.conversations.load(self.scope, self.initial.id).turns[-1].user_message,
            "解释代码",
        )
        self.assertEqual(self.advance(service, run), done)
        self.assertEqual(len(self.requests), 2)

    def test_http_submit_idempotence_guards_and_no_browser_model_receipts(self):
        service = self.coordinator([completion()])
        app = create_app(
            {}, entry_store=self.entry, agent_enabled=True, agent_runtime=service
        )
        prefix = f"/api/v1/sessions/{self.scope}/agent/conversations/{self.initial.id}"
        with TestClient(
            app,
            headers={"X-VibeEval-Request": "1"},
            cookies={"vibeeval_candidate": self.browser},
        ) as client:
            body = {"request_key": "http-request", "text": "你好"}
            first = client.post(prefix + "/turns", json=body)
            self.assertEqual(first.status_code, 202, first.text)
            rid = first.json()["run_id"]
            self.assertEqual(
                client.post(prefix + "/turns", json=body).json()["run_id"], rid
            )
            self.assertEqual(self.requests, [])
            self.assertEqual(
                client.post(
                    prefix + "/turns", json={**body, "text": "冲突"}
                ).status_code,
                409,
            )
            for extra in (
                {"approved": True},
                {"tool_results": []},
                {"scope_id": "other"},
                {"mode": "sandbox_auto"},
            ):
                self.assertEqual(
                    client.post(prefix + "/turns", json={**body, **extra}).status_code,
                    422,
                )
            done = client.post(prefix + f"/runs/{rid}/advance")
            self.assertEqual(done.status_code, 200, done.text)
            self.assertEqual(done.json()["status"], "completed")
            self.assertEqual(
                client.get(prefix + "/runs").json()["items"][0]["run_id"], rid
            )
            self.assertEqual(client.get(prefix + f"/runs/{rid}").status_code, 200)
            attempts = client.get(prefix + f"/runs/{rid}/attempts")
            self.assertNotIn("runtime-fixture-secret", attempts.text)
            self.assertNotIn("You are VibeEval", attempts.text)
            self.assertEqual(
                client.post(
                    prefix + f"/runs/{rid}/attempts", json={"response": "fake"}
                ).status_code,
                405,
            )
            self.assertEqual(
                client.get(prefix + f"/runs/{rid}/interactions").json()["items"], []
            )
        with TestClient(app, headers={"X-VibeEval-Request": "1"}) as client:
            self.assertEqual(client.get(prefix + "/runs").status_code, 404)
        with TestClient(app, cookies={"vibeeval_candidate": self.browser}) as client:
            self.assertEqual(client.post(prefix + "/turns", json=body).status_code, 403)

    def test_approval_waits_before_io_and_approved_call_resumes_once(self):
        service = self.coordinator(
            [READ, completion()], policy=RunPolicy(read_decision="ask")
        )
        run = self.submit(service)
        self.assertEqual(self.advance(service, run)["status"], "waiting_approval")
        self.assertEqual(self.workspace.calls, 0)
        interaction = service.store.interactions(
            self.scope, self.initial.id, run["run_id"]
        )["items"][0]
        self.assertEqual(interaction["request"]["arguments"]["path"], "src/main.py")
        for _ in range(2):
            resolved = service.store.resolve(
                self.scope,
                self.initial.id,
                run["run_id"],
                interaction["interaction_id"],
                kind="approval",
                answer={"decision": "approve"},
            )
            self.assertEqual(resolved["status"], "approved")
        self.assertEqual(self.advance(service, run)["status"], "completed")
        self.assertEqual(self.workspace.calls, 1)
        with self.assertRaises(SessionConflict):
            service.store.resolve(
                self.scope,
                self.initial.id,
                run["run_id"],
                interaction["interaction_id"],
                kind="approval",
                answer={"decision": "reject"},
            )

    def test_rejected_approval_is_tool_error_and_does_not_read(self):
        service = self.coordinator(
            [READ, completion()], policy=RunPolicy(read_decision="ask")
        )
        run = self.submit(service)
        self.advance(service, run)
        interaction = service.store.interactions(
            self.scope, self.initial.id, run["run_id"]
        )["items"][0]
        service.store.resolve(
            self.scope,
            self.initial.id,
            run["run_id"],
            interaction["interaction_id"],
            kind="approval",
            answer={"decision": "reject"},
        )
        self.assertEqual(self.advance(service, run)["status"], "completed")
        self.assertEqual(self.workspace.calls, 0)
        self.assertIn("APPROVAL_REJECTED", self.requests[-1]["messages"][-1]["content"])

    def test_question_answer_survives_new_coordinator_and_is_not_permission(self):
        service = self.coordinator(
            [
                completion(
                    calls=(
                        (
                            "request_user_input",
                            {"question": "选择哪个模块？", "options": ["入口", "预约"]},
                        ),
                    )
                ),
                completion(),
            ]
        )
        run = self.submit(service)
        self.assertEqual(self.advance(service, run)["status"], "waiting_user_input")
        interaction = service.store.interactions(
            self.scope, self.initial.id, run["run_id"]
        )["items"][0]
        with self.assertRaises(ConversationNotFound):
            service.store.resolve(
                self.scope,
                self.initial.id,
                run["run_id"],
                interaction["interaction_id"],
                kind="approval",
                answer={"decision": "approve"},
            )
        service.store.resolve(
            self.scope,
            self.initial.id,
            run["run_id"],
            interaction["interaction_id"],
            kind="user_input",
            answer={"answer": "入口"},
        )
        restarted = RuntimeCoordinator(
            self.reader, settings=SETTINGS, client=service.client
        )
        self.assertEqual(self.advance(restarted, run)["status"], "completed")
        self.assertIn("入口", self.requests[-1]["messages"][-1]["content"])
        self.assertEqual(
            len(self.conversations.load(self.scope, self.initial.id).turns), 1
        )

    def test_expired_approval_returns_error_without_io(self):
        now = [int(time.time())]
        service = self.coordinator(
            [READ, completion()],
            policy=RunPolicy(read_decision="ask", interaction_seconds=1),
            clock=lambda: now[0],
        )
        run = self.submit(service)
        self.advance(service, run)
        now[0] += 2
        self.assertEqual(self.advance(service, run)["status"], "completed")
        self.assertEqual(self.workspace.calls, 0)
        self.assertIn("APPROVAL_EXPIRED", self.requests[-1]["messages"][-1]["content"])

    def test_manual_idle_compaction_records_one_provider_attempt(self):
        before = self.historical_turn()
        service = self.coordinator([completion("旧背景摘要。", usage=20)])
        run = service.submit(
            self.scope,
            self.initial.id,
            CompactInput(request_key="compact-manual"),
            kind="compact",
        )
        done = self.advance(service, run)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(
            (done["model_calls"], done["compaction_calls"], done["total_tokens"]),
            (0, 1, 20),
        )
        after = self.conversations.load(self.scope, self.initial.id)
        self.assertEqual(after.turns, before.turns)
        self.assertEqual(after.context_generation, 1)
        attempt = service.store.attempts(self.scope, self.initial.id, run["run_id"])[
            "items"
        ][0]
        self.assertEqual(attempt["manifest"]["kind"], "compaction")

    def test_threshold_compacts_inside_long_turn_before_continuing(self):
        self.workspace.contents["src/main.py"] = "main " * 1200
        self.workspace.contents["src/booking.py"] = "booking " * 750
        second_read = completion(calls=(("read_file", {"path": "src/booking.py"}),))
        service = self.coordinator(
            [READ, second_read, completion("旧入口材料摘要与版本定位。"), completion()],
            policy=RunPolicy(max_compactions=1),
            window=WindowPolicy(soft_request_chars=14000),
        )
        run = self.submit(service)
        done = self.advance(service, run)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["compaction_calls"], 1)
        after = self.conversations.load(self.scope, self.initial.id)
        self.assertEqual(after.compactions[0].through_step_count, 1)
        self.assertEqual(after.turns[0].user_message, "解释代码")
        attempts = service.store.attempts(self.scope, self.initial.id, run["run_id"])[
            "items"
        ]
        self.assertEqual(
            [a["kind"] for a in attempts], ["model", "model", "compaction", "model"]
        )
        self.assertEqual(attempts[2]["trigger"], "threshold")
        self.assertEqual(done["total_tokens"], 40)

    def test_definitive_overflow_gets_one_compaction_and_one_retry(self):
        self.historical_turn()
        overflow = (
            400,
            {
                "error": {
                    "code": 400,
                    "metadata": {"error_type": "context_length_exceeded"},
                }
            },
        )
        service = self.coordinator([overflow, completion("背景摘要。"), completion()])
        run = self.submit(service)
        done = self.advance(service, run)
        self.assertEqual(done["status"], "completed")
        self.assertEqual((done["model_calls"], done["compaction_calls"]), (2, 1))
        self.assertEqual(len(self.requests), 3)
        attempts = service.store.attempts(self.scope, self.initial.id, run["run_id"])[
            "items"
        ]
        self.assertEqual(
            [a["status"] for a in attempts], ["rejected", "adopted", "adopted"]
        )
        self.assertEqual(attempts[1]["trigger"], "overflow")
        self.assertIsNone(done["total_tokens"])
        self.assertEqual(done["known_total_tokens"], 20)

    def test_second_overflow_is_not_retried_again(self):
        self.historical_turn()
        overflow = (400, {"error": {"code": "context_length_exceeded"}})
        service = self.coordinator([overflow, completion("摘要。"), overflow])
        run = self.submit(service)
        self.assertEqual(self.advance(service, run)["status"], "capacity_exceeded")
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.advance(service, run)["status"], "capacity_exceeded")
        self.assertEqual(len(self.requests), 3)

    def test_network_unknown_does_not_retry(self):
        service = self.coordinator([httpx.ReadTimeout("fixture timeout")])
        run = self.submit(service)
        done = self.advance(service, run)
        self.assertEqual(done["status"], "unknown")
        self.assertFalse(done["usage_complete"])
        self.advance(service, run)
        self.assertEqual(len(self.requests), 1)

    def reserve_model(self, service, run):
        token = service.store.claim(self.scope, self.initial.id, run["run_id"])
        reservation = service.store.reserve(
            self.scope,
            self.initial.id,
            run["run_id"],
            token,
            kind="model",
            tools=runtime_registry(self.reader, "read_only"),
            settings=SETTINGS,
            window_policy=WindowPolicy(),
            trigger="initial",
        )
        return token, reservation

    def test_expired_worker_can_resume_prepared_but_never_replay_dispatched(self):
        now = [int(time.time())]
        service = self.coordinator([completion()], clock=lambda: now[0])
        run = self.submit(service)
        old_token, reservation = self.reserve_model(service, run)
        now[0] += 181
        self.assertEqual(self.advance(service, run)["status"], "completed")
        self.assertEqual(len(self.requests), 1)
        with self.assertRaises(LeaseLost):
            service.store.dispatch(
                self.scope, self.initial.id, run["run_id"], old_token, reservation[0]
            )

    def test_dispatched_unknown_recovery_preserves_late_usage_without_adopting(self):
        now = [int(time.time())]
        service = self.coordinator([], clock=lambda: now[0])
        run = self.submit(service)
        token, reservation = self.reserve_model(service, run)
        service.store.dispatch(
            self.scope, self.initial.id, run["run_id"], token, reservation[0]
        )
        now[0] += 181
        self.assertEqual(self.advance(service, run)["status"], "unknown")
        self.assertEqual(self.requests, [])
        state = self.conversations.load(self.scope, self.initial.id)
        late = service.store.finish(
            self.scope,
            self.initial.id,
            run["run_id"],
            token,
            reservation[0],
            response=ModelResponse(
                model="fixture",
                finish_reason="stop",
                message=AssistantOutput(content="迟到回答"),
            ),
            usage={"total_tokens": 17},
            error_code=None,
            tools=runtime_registry(self.reader, "read_only"),
            settings=SETTINGS,
            window_policy=WindowPolicy(),
        )
        self.assertEqual(late["status"], "unknown")
        self.assertEqual(late["total_tokens"], 17)
        self.assertEqual(self.conversations.load(self.scope, self.initial.id), state)
        self.assertEqual(
            service.store.attempts(self.scope, self.initial.id, run["run_id"])["items"][
                0
            ]["status"],
            "late",
        )

    def test_concurrent_claim_has_one_worker(self):
        service = self.coordinator([])
        run = self.submit(service)

        def claim(_):
            try:
                return bool(
                    service.store.claim(self.scope, self.initial.id, run["run_id"])
                )
            except RunBusy:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(claim, range(2))), [True, False])

    def test_interrupt_during_provider_call_records_late_result_without_continuation(
        self,
    ):
        run_id = []

        def interrupt(_):
            service.store.stop(
                self.scope,
                self.initial.id,
                run_id[0],
                "interrupted",
                "USER_INTERRUPTED",
            )

        service = self.coordinator(
            [completion("不应采用的迟到回答")], callback=interrupt
        )
        run = self.submit(service)
        run_id.append(run["run_id"])
        done = self.advance(service, run)
        self.assertEqual(done["status"], "interrupted")
        self.assertEqual(done["total_tokens"], 10)
        self.assertIsNone(
            self.conversations.load(self.scope, self.initial.id)
            .turns[-1]
            .steps[-1]
            .response
        )
        self.assertEqual(
            service.store.attempts(self.scope, self.initial.id, run["run_id"])["items"][
                0
            ]["status"],
            "late",
        )

    def test_budgets_stop_before_further_tools_or_models(self):
        service = self.coordinator([READ], policy=RunPolicy(max_total_tokens=5))
        run = self.submit(service)
        done = self.advance(service, run)
        self.assertEqual(done["status"], "budget_exhausted")
        self.assertEqual(self.workspace.calls, 0)
        self.assertEqual(len(self.requests), 1)

    def test_explicit_selection_is_input_not_an_autonomous_read_in_chat_only_mode(self):
        ref = self.reader.create_reference(
            self.scope,
            self.initial.id,
            {"kind": "selection", "path": "src/main.py", "content": "未保存的用户选区"},
        )
        service = self.coordinator([completion()])
        run = self.submit(
            service, reference_ids=(ref["reference_id"],), mode="chat_only"
        )
        done = self.advance(service, run)
        self.assertEqual(done["status"], "completed")
        self.assertNotIn("tools", self.requests[0])
        self.assertIn("未保存的用户选区", self.requests[0]["messages"][-1]["content"])
        self.assertEqual(self.workspace.calls, 0)
        with self.factory() as db:
            self.assertEqual(len(db.scalars(select(AgentReadObservation)).all()), 0)
            attempt = db.scalar(
                select(AgentAttempt).where(AgentAttempt.run_id == run["run_id"])
            )
            self.assertEqual(
                attempt.manifest_json["input_references"][0]["window_mode"],
                "inline_material",
            )

    def test_exam_submission_during_call_prevents_adoption_and_future_dispatch(self):
        def close(_):
            with self.factory.begin() as db:
                exam = db.get(ExamSession, self.scope)
                exam.status = "submitting"

        service = self.coordinator([completion()], callback=close)
        run = self.submit(service)
        self.assertEqual(self.advance(service, run)["status"], "expired")
        self.assertEqual(len(self.requests), 1)
        self.assertIsNone(
            self.conversations.load(self.scope, self.initial.id)
            .turns[-1]
            .steps[-1]
            .response
        )

    def test_attempt_receipt_and_core_adoption_roll_back_together(self):
        from unittest.mock import patch

        service = self.coordinator([completion()])
        run = self.submit(service)
        with patch.object(
            service.store,
            "save_state",
            side_effect=RuntimeError("fixture transaction failure"),
        ):
            # Preparation itself must roll back without dispatching.
            with self.assertRaises(RuntimeError):
                self.advance(service, run)
        self.assertEqual(self.requests, [])
        with self.factory() as db:
            self.assertEqual(len(db.scalars(select(AgentAttempt)).all()), 0)
            row = db.get(AgentRun, run["run_id"])
            self.assertEqual(row.model_calls, 0)

    def test_agent_compaction_request_is_scheduled_then_adopted_at_safe_boundary(self):
        self.workspace.contents["src/main.py"] = "main body " * 400
        self.workspace.contents["src/booking.py"] = "booking body " * 400
        service = self.coordinator(
            [
                READ,
                completion(calls=(("read_file", {"path": "src/booking.py"}),)),
                completion(calls=(("compact_context", {"reason": "整理当前材料"}),)),
                completion("两个文件的历史摘要与版本材料定位。"),
                completion(),
            ]
        )
        run = self.submit(service)
        done = self.advance(service, run)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["compaction_calls"], 1)
        attempts = service.store.attempts(self.scope, self.initial.id, run["run_id"])[
            "items"
        ]
        self.assertEqual(attempts[3]["trigger"], "agent")
        self.assertIn("scheduled", self.requests[-1]["messages"][-1]["content"])
        self.assertEqual(
            len(self.conversations.load(self.scope, self.initial.id).turns[0].steps), 4
        )

    def test_manual_compaction_requested_during_final_response_is_not_lost(self):
        run_ids = []

        def request(index):
            if index == 1:
                service.store.request_compaction(
                    self.scope, self.initial.id, run_ids[0]
                )

        service = self.coordinator(
            [completion("较长的最终回答。" * 500), completion("最后一轮的历史摘要。")],
            callback=request,
        )
        run = self.submit(service)
        run_ids.append(run["run_id"])
        done = self.advance(service, run)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["compaction_calls"], 1)
        self.assertEqual(
            self.conversations.load(self.scope, self.initial.id).context_generation, 1
        )

    def test_manual_compaction_waits_for_pending_approval_result(self):
        service = self.coordinator(
            [READ, completion()], policy=RunPolicy(read_decision="ask")
        )
        run = self.submit(service)
        self.advance(service, run)
        service.store.request_compaction(self.scope, self.initial.id, run["run_id"])
        interaction = service.store.interactions(
            self.scope, self.initial.id, run["run_id"]
        )["items"][0]
        service.store.resolve(
            self.scope,
            self.initial.id,
            run["run_id"],
            interaction["interaction_id"],
            kind="approval",
            answer={"decision": "approve"},
        )
        done = self.advance(service, run)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(self.workspace.calls, 1)
        self.assertEqual(done["compaction_calls"], 0)  # latest exchange stays protected

    def test_failed_compaction_preserves_history_and_counts_rejected_usage(self):
        before = self.historical_turn()
        service = self.coordinator([completion("截断摘要", reason="length", usage=41)])
        run = service.submit(
            self.scope,
            self.initial.id,
            CompactInput(request_key="partial-summary"),
            kind="compact",
        )
        done = self.advance(service, run)
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["total_tokens"], 41)
        after = self.conversations.load(self.scope, self.initial.id)
        self.assertEqual(after, before)
        self.assertEqual(after.compactions, ())
        self.assertEqual(len(self.requests), 1)

    def test_parallel_tool_budget_stops_before_second_read(self):
        service = self.coordinator(
            [
                completion(
                    calls=(
                        ("read_file", {"path": "src/main.py"}),
                        ("read_file", {"path": "src/booking.py"}),
                    )
                )
            ],
            policy=RunPolicy(max_tool_calls=1),
        )
        run = self.submit(service)
        self.assertEqual(self.advance(service, run)["status"], "budget_exhausted")
        self.assertEqual(self.workspace.calls, 1)

    def test_receipt_failure_rolls_back_response_usage_and_core_adoption(self):
        from unittest.mock import patch

        now = [int(time.time())]
        service = self.coordinator([], clock=lambda: now[0])
        run = self.submit(service)
        token, reservation = self.reserve_model(service, run)
        service.store.dispatch(
            self.scope, self.initial.id, run["run_id"], token, reservation[0]
        )
        old = self.conversations.load(self.scope, self.initial.id)
        original = service.store.event

        def fail_adopted(db, conv, event_kind, record, **data):
            if event_kind == "model.adopted":
                raise RuntimeError("receipt event failed")
            return original(db, conv, event_kind, record, **data)

        with (
            patch.object(service.store, "event", side_effect=fail_adopted),
            self.assertRaises(RuntimeError),
        ):
            service.store.finish(
                self.scope,
                self.initial.id,
                run["run_id"],
                token,
                reservation[0],
                response=ModelResponse(
                    model="fixture",
                    finish_reason="stop",
                    message=AssistantOutput(content="尚未提交的结果"),
                ),
                usage={"total_tokens": 10},
                error_code=None,
                tools=runtime_registry(self.reader, "read_only"),
                settings=SETTINGS,
                window_policy=WindowPolicy(),
            )
        self.assertEqual(self.conversations.load(self.scope, self.initial.id), old)
        with self.factory() as db:
            attempt = db.get(AgentAttempt, reservation[0])
            self.assertEqual(attempt.status, "running")
            self.assertIsNone(attempt.response_json)
            self.assertIsNone(attempt.usage_json)
        now[0] += 181
        self.assertEqual(self.advance(service, run)["status"], "unknown")
        self.assertEqual(self.requests, [])

    def test_server_chat_only_policy_cannot_be_expanded_by_client(self):
        service = self.coordinator([completion()], policy=RunPolicy(mode="chat_only"))
        with self.assertRaises(ValueError):
            self.submit(service)
        run = self.submit(service, mode="chat_only")
        self.assertEqual(self.advance(service, run)["status"], "completed")
        self.assertNotIn("tools", self.requests[0])

    def test_cross_conversation_reference_id_cannot_be_attached_by_guess(self):
        other = create_conversation(self.scope)
        self.conversations.create(other)
        ref = self.reader.create_reference(
            self.scope, other.id, {"kind": "file", "path": "src/main.py"}
        )
        service = self.coordinator([])
        with self.assertRaises(ConversationNotFound):
            self.submit(service, reference_ids=(ref["reference_id"],))
        self.assertEqual(self.conversations.load(self.scope, self.initial.id).turns, ())

    def test_invalid_provider_text_is_audited_without_poisoning_jsonb_state(self):
        service = self.coordinator([completion("bad\x00text")])
        run = self.submit(service)
        done = self.advance(service, run)
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["error_code"], "MODEL_TEXT_ENCODING_INVALID")
        self.assertEqual(done["total_tokens"], 10)

    def test_changed_prepared_source_stops_instead_of_leaving_a_permanent_lease_loop(
        self,
    ):
        from app.agent import interrupt_turn

        now = [int(time.time())]
        service = self.coordinator([], clock=lambda: now[0])
        run = self.submit(service)
        _, reservation = self.reserve_model(service, run)
        state = self.conversations.load(self.scope, self.initial.id)
        self.save(state, interrupt_turn(state, state.active_turn.id))
        now[0] += 181
        done = self.advance(service, run)
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["error_code"], "STALE_RUN_STATE")
        self.assertEqual(self.requests, [])

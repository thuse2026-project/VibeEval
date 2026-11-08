"""Real PostgreSQL conversation, CAS, event and HTTP integration checks."""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import test_db_postgres as database_tests
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.agent import accept_response, begin_turn, create_conversation, start_step
from app.agent.session_types import AssistantOutput, ModelResponse, SessionConflict
from app.agent_integration.store import (
    ConversationClosed,
    ConversationNotFound,
    PostgresSessionStore,
)
from app.db.models import AgentConversation, AgentEvent, ExamSession
from app.exams.postgres import PostgresEntryStore
from app.main import create_app

HEADERS = {"X-VibeEval-Request": "1"}


@unittest.skipUnless(
    os.environ.get("VIBEEVAL_RUN_DB_TESTS") == "1", "Requires isolated PostgreSQL"
)
class AgentIntegrationStoreTests(unittest.TestCase):
    setUpClass = classmethod(database_tests.PostgreSQLTests.setUpClass.__func__)
    drop_test_database = classmethod(
        database_tests.PostgreSQLTests.drop_test_database.__func__
    )

    def setUp(self):
        database_tests.PostgreSQLTests.setUp(self)
        self.entry = PostgresEntryStore(self.factory)
        token, _ = self.entry.invite(
            "agent-student", "考生", "agent-exam", "Agent 验收", 60, 24
        )
        self.browser = "b" * 43
        self.owned = self.entry.admit(token, self.browser)
        self.scope = self.owned["session_id"]
        self.store = PostgresSessionStore(self.factory)
        self.initial = create_conversation(self.scope)
        self.store.create(self.initial, title="第一条对话")

    def client(self, *, enabled=True, owned=True):
        app = create_app({}, entry_store=self.entry, agent_enabled=enabled)
        return TestClient(
            app,
            headers=HEADERS,
            cookies={"vibeeval_candidate": self.browser} if owned else {},
        )

    @property
    def prefix(self):
        return f"/api/v1/sessions/{self.scope}/agent/conversations"

    def test_multiple_conversations_persist_across_store_instances_and_scope_isolation(
        self,
    ):
        second = create_conversation(self.scope)
        self.store.create(second, title="第二条对话")
        restarted = PostgresSessionStore(self.factory)
        self.assertEqual(restarted.load(self.scope, self.initial.id), self.initial)
        self.assertEqual(len(restarted.list_conversations(self.scope)["items"]), 2)
        page = restarted.list_conversations(self.scope, limit=1)
        self.assertTrue(page["has_more"])
        next_page = restarted.list_conversations(self.scope, offset=1, limit=1)
        self.assertNotEqual(
            page["items"][0]["conversation_id"],
            next_page["items"][0]["conversation_id"],
        )
        self.assertEqual(restarted.list_conversations("other-scope")["items"], [])
        for read in (
            restarted.load,
            restarted.describe,
            restarted.history,
            restarted.events,
            restarted.event_page,
        ):
            with (
                self.subTest(read=read.__name__),
                self.assertRaises(ConversationNotFound),
            ):
                read("other-scope", self.initial.id)
        with self.assertRaises(SessionConflict):
            self.store.create(self.initial)

    def test_revisions_are_separate_idempotent_and_inflight_load_does_not_interrupt(
        self,
    ):
        ready = begin_turn(self.initial, "问题", request_key="m1")
        pending = start_step(ready, step_id="s1")
        with self.assertRaises(SessionConflict):
            self.store.save(pending, expected_revision=0)
        self.store.save(ready, expected_revision=0)
        self.store.save(pending, expected_revision=1)
        self.store.save(pending, expected_revision=1)
        loaded = PostgresSessionStore(self.factory).load(self.scope, self.initial.id)
        self.assertEqual(loaded, pending)
        self.assertEqual(loaded.active_turn.status, "awaiting_model")
        events = self.store.events(self.scope, self.initial.id)
        self.assertEqual([e["sequence"] for e in events["items"]], [1, 2, 3])
        self.assertEqual([e["data"]["revision"] for e in events["items"]], [0, 1, 2])
        self.assertEqual(
            self.store.events(self.scope, self.initial.id, after_sequence=2)["items"],
            events["items"][2:],
        )

    def test_concurrent_cas_has_one_winner_and_one_event(self):
        candidates = [
            begin_turn(self.initial, text, request_key=text) for text in ("a", "b")
        ]

        def save(state):
            try:
                PostgresSessionStore(self.factory).save(state, expected_revision=0)
                return "saved"
            except SessionConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(
                list(pool.map(save, candidates)), ["saved", "conflict"]
            )
        self.assertEqual(
            len(self.store.events(self.scope, self.initial.id)["items"]), 2
        )

    def test_state_and_event_roll_back_together(self):
        ready = begin_turn(self.initial, "问题", request_key="m1")
        with patch.object(
            self.store, "_event", side_effect=RuntimeError("event failed")
        ):
            with self.assertRaises(RuntimeError):
                self.store.save(ready, expected_revision=0)
        self.assertEqual(self.store.load(self.scope, self.initial.id), self.initial)
        self.assertEqual(
            len(self.store.events(self.scope, self.initial.id)["items"]), 1
        )
        self.store.save(ready, expected_revision=0)
        with self.factory() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(AgentEvent)), 2)

    def test_public_history_omits_internal_continuation_and_configuration(self):
        ready = begin_turn(self.initial, "解释代码", request_key="m1")
        pending = start_step(ready, step_id="s1")
        final = accept_response(
            pending,
            "s1",
            ModelResponse(
                model="fixture",
                finish_reason="stop",
                message=AssistantOutput(
                    content="解释结果",
                    continuation_json='{"reasoning_details":[{"data":"opaque-private"}]}',
                ),
            ),
        )
        for before, after in (
            (self.initial, ready),
            (ready, pending),
            (pending, final),
        ):
            self.store.save(after, expected_revision=before.revision)
        self.assertIn(
            "opaque-private",
            self.store.load(self.scope, self.initial.id).model_dump_json(),
        )
        with self.client() as client:
            response = client.get(f"{self.prefix}/{self.initial.id}/history")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("opaque-private", response.text)
            self.assertNotIn("continuation_json", response.text)
            self.assertNotIn("system_prompt", response.text)
            self.assertEqual(
                response.json()["turns"][0]["steps"][0]["response"]["message"][
                    "content"
                ],
                "解释结果",
            )
            empty = client.get(
                f"{self.prefix}/{self.initial.id}/history?after_turn=1"
            ).json()
            self.assertEqual(empty["turns"], [])
            self.assertFalse(empty["has_more"])

    def test_http_create_list_restore_metadata_and_events(self):
        with self.client() as client:
            response = client.post(self.prefix, json={"title": "新建对话"})
            self.assertEqual(response.status_code, 201, response.text)
            created = response.json()
            self.assertEqual(created["exam_session_id"], self.scope)
            self.assertEqual(created["status"], "idle")
            self.assertEqual(created["event_sequence"], 1)
            cid = created["conversation_id"]
            self.assertEqual(len(client.get(self.prefix).json()["items"]), 2)
        with self.client() as restarted:
            response = restarted.get(f"{self.prefix}/{cid}")
            self.assertEqual(response.json(), created)
            self.assertEqual(response.headers["cache-control"], "no-store")
            events = restarted.get(f"{self.prefix}/{cid}/events").json()
            self.assertEqual(events["items"][0]["kind"], "conversation.created")
            self.assertEqual(events["next_sequence"], 1)
            self.assertEqual(
                restarted.get(f"{self.prefix}/{cid}/events?after_sequence=1").json()[
                    "items"
                ],
                [],
            )

    def test_http_auth_disabled_flag_and_input_cannot_replace_server_state(self):
        with self.client(owned=False) as outsider:
            self.assertEqual(outsider.get(self.prefix).status_code, 404)
            self.assertEqual(outsider.post(self.prefix, json={}).status_code, 404)
        with self.client(enabled=False) as disabled:
            self.assertEqual(
                disabled.get(self.prefix).json()["detail"]["code"], "AGENT_DISABLED"
            )
        with self.client() as client:
            self.assertEqual(
                client.get(
                    self.prefix, headers={"X-VibeEval-Request": "0"}
                ).status_code,
                403,
            )
            self.assertEqual(
                client.get("/api/v1/sessions/other/agent/conversations").status_code,
                404,
            )
            for body in (
                {"title": " "},
                {"scope_id": "other"},
                {"messages": []},
                {"system_prompt": "override"},
            ):
                with self.subTest(body=body):
                    self.assertEqual(
                        client.post(self.prefix, json=body).status_code, 422
                    )
            for suffix in ("?offset=-1", "?limit=0", "?limit=101"):
                self.assertEqual(client.get(self.prefix + suffix).status_code, 422)
            self.assertEqual(client.get(f"{self.prefix}/missing").status_code, 404)

    def test_closed_exam_rejects_new_conversations_but_preserves_history(self):
        with self.factory.begin() as db:
            row = db.get(ExamSession, self.scope)
            row.started_at = row.admitted_at
            row.deadline_at = row.admitted_at + 3600
            row.submitted_at = row.admitted_at
            row.status = "submitted"
        with self.assertRaises(ConversationClosed):
            self.store.create(create_conversation(self.scope))
        with self.client() as client:
            self.assertEqual(client.post(self.prefix, json={}).status_code, 409)
            self.assertEqual(
                client.get(f"{self.prefix}/{self.initial.id}/history").status_code, 200
            )
        self.assertEqual(self.store.load(self.scope, self.initial.id), self.initial)

    def test_database_snapshot_identity_cannot_diverge_from_metadata(self):
        with self.assertRaises(IntegrityError), self.factory.begin() as db:
            data = self.initial.model_dump(mode="json")
            data["scope_id"] = "other"
            db.execute(
                update(AgentConversation)
                .where(AgentConversation.conversation_id == self.initial.id)
                .values(state_json=data)
            )
        self.assertEqual(self.store.load(self.scope, self.initial.id), self.initial)

    def test_configuration_changes_and_invalid_title_do_not_write(self):
        ready = begin_turn(self.initial, "问题", request_key="m1")
        payload = ready.model_dump(mode="json")
        payload["config"]["model"] = "different-model"
        from app.agent.session_types import ConversationState

        with self.assertRaises(SessionConflict):
            self.store.save(
                ConversationState.model_validate(payload), expected_revision=0
            )
        with self.assertRaises(ValueError):
            self.store.create(create_conversation(self.scope), title=" ")
        self.assertEqual(self.store.load(self.scope, self.initial.id), self.initial)
        self.assertEqual(
            len(self.store.events(self.scope, self.initial.id)["items"]), 1
        )

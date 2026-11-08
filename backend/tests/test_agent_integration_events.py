"""Committed PostgreSQL event replay, HTTP guards and real loopback streaming."""

import asyncio
import os
import socket
import unittest
from unittest.mock import Mock, patch

import httpx
import test_agent_integration_store as store_tests
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import delete, event
from sqlalchemy.exc import OperationalError
from test_agent_event_stream import decode_frames

from app.agent import create_conversation
from app.agent_integration.event_stream import EventStreamPolicy, event_cursor
from app.db.models import AgentConversation, AgentEvent, ExamSession
from app.main import create_app


@unittest.skipUnless(
    os.environ.get("VIBEEVAL_RUN_DB_TESTS") == "1", "Requires isolated PostgreSQL"
)
class AgentIntegrationEventTests(unittest.TestCase):
    setUpClass = classmethod(store_tests.AgentIntegrationStoreTests.setUpClass.__func__)
    drop_test_database = classmethod(
        store_tests.AgentIntegrationStoreTests.drop_test_database.__func__
    )
    setUp = store_tests.AgentIntegrationStoreTests.setUp

    @property
    def url(self):
        return f"/api/v1/sessions/{self.scope}/agent/conversations/{self.initial.id}/events/stream"

    def app(self, **options):
        self.runtime = Mock()
        return create_app(
            {},
            entry_store=self.entry,
            agent_enabled=True,
            agent_store=self.store,
            agent_runtime=self.runtime,
            **options,
        )

    def client(self, app=None):
        return TestClient(
            app or self.app(),
            headers=store_tests.HEADERS,
            cookies={"vibeeval_candidate": self.browser},
        )

    def append(self, *, data=None):
        with self.factory.begin() as db:
            row = db.get(AgentConversation, self.initial.id, with_for_update=True)
            row.event_sequence += 1
            db.add(
                AgentEvent(
                    conversation_id=row.conversation_id,
                    sequence=row.event_sequence,
                    kind="fixture.committed",
                    data_json=data or {"number": row.event_sequence},
                    created_at=row.created_at,
                )
            )
        return row.event_sequence

    def frames(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        return decode_frames([response.content])

    def test_http_replay_matches_existing_committed_json_without_running_agent(self):
        self.append(data={"text": "中文🙂\n\nid: forged"})
        before = self.store.load(self.scope, self.initial.id)
        expected = self.store.events(self.scope, self.initial.id)["items"]
        with self.client() as client:
            response = client.get(self.url, params={"follow": "false"})
        frames = self.frames(response)
        durable = [item for item in frames if "id" in item]
        self.assertEqual([item["data"]["sequence"] for item in durable], [1, 2])
        self.assertEqual(
            [
                {
                    k: value
                    for k, value in item["data"].items()
                    if k not in ("version", "conversation_id")
                }
                for item in durable
            ],
            expected,
        )
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-accel-buffering"], "no")
        self.assertTrue(
            response.headers["content-type"].startswith("text/event-stream")
        )
        self.assertEqual(self.store.load(self.scope, self.initial.id), before)
        self.runtime.assert_not_called()
        self.assertEqual(self.runtime.mock_calls, [])

    def test_reconnect_uses_scoped_header_across_new_application_instances(self):
        self.append()
        self.append()
        with self.client() as client:
            first = self.frames(client.get(self.url, params={"follow": "false"}))
        cursor = [item["id"] for item in first if "id" in item][0]
        with self.client() as restarted:
            result = self.frames(
                restarted.get(
                    self.url,
                    params={"follow": "false", "after_sequence": 0},
                    headers={"Last-Event-ID": cursor},
                )
            )
        self.assertEqual(
            [item["data"]["sequence"] for item in result if "id" in item], [2, 3]
        )

    def test_source_cookie_feature_flag_and_cursor_scope_are_enforced(self):
        other = create_conversation(self.scope)
        self.store.create(other)
        app = self.app()
        with self.client(app) as client:
            response = client.get(
                self.url,
                params={"follow": "false"},
                headers={"Last-Event-ID": event_cursor(other.id, 1)},
            )
            self.assertEqual(response.status_code, 409)
            self.assertEqual(
                response.json()["detail"]["code"], "AGENT_EVENT_CURSOR_SCOPE_MISMATCH"
            )
            app.state.agent_enabled = False
            self.assertEqual(client.get(self.url).status_code, 403)
        with TestClient(self.app(), headers=store_tests.HEADERS) as client:
            self.assertEqual(client.get(self.url).status_code, 404)
        with TestClient(
            self.app(), cookies={"vibeeval_candidate": self.browser}
        ) as client:
            self.assertEqual(client.get(self.url).status_code, 403)

    def test_ahead_malformed_or_overflow_cursor_fails_before_sse_headers(self):
        with self.client() as client:
            ahead = client.get(
                self.url, params={"after_sequence": 9, "follow": "false"}
            )
            self.assertEqual(ahead.status_code, 409)
            self.assertEqual(ahead.json()["detail"]["code"], "AGENT_EVENT_CURSOR_AHEAD")
            self.assertEqual(
                client.get(self.url, headers={"Last-Event-ID": "invalid"}).status_code,
                422,
            )
            for position in (-1, 2**63):
                self.assertEqual(
                    client.get(
                        self.url, params={"after_sequence": position}
                    ).status_code,
                    422,
                )

    def test_initial_source_failures_are_safe_http_errors_not_partial_streams(self):
        with (
            self.client() as client,
            patch.object(
                self.store,
                "event_page",
                side_effect=OperationalError(
                    "private SQL", {}, Exception("private credential")
                ),
            ),
        ):
            response = client.get(self.url, params={"follow": "false"})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private", response.text)
        self.assertNotIn("text/event-stream", response.headers["content-type"])
        self.append(data={"text": "x" * 2000})
        with self.client(
            self.app(agent_event_policy=EventStreamPolicy(max_frame_bytes=512))
        ) as client:
            response = client.get(self.url, params={"follow": "false"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "AGENT_EVENT_TOO_LARGE")

    def test_gap_in_committed_log_requires_resynchronization(self):
        self.append()
        self.append()
        with self.factory.begin() as db:
            db.execute(
                delete(AgentEvent).where(
                    AgentEvent.conversation_id == self.initial.id,
                    AgentEvent.sequence == 2,
                )
            )
        with self.client() as client:
            response = client.get(self.url, params={"follow": "false"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "AGENT_EVENT_GAP")

    def test_uncommitted_or_rolled_back_event_is_never_visible(self):
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.factory.begin() as db:
                row = db.get(AgentConversation, self.initial.id, with_for_update=True)
                row.event_sequence += 1
                db.add(
                    AgentEvent(
                        conversation_id=row.conversation_id,
                        sequence=2,
                        kind="not.committed",
                        data_json={},
                        created_at=row.created_at,
                    )
                )
                db.flush()
                visible = self.store.event_page(self.scope, self.initial.id)
                self.assertEqual(visible["head_sequence"], 1)
                self.assertEqual(len(visible["items"]), 1)
                raise RuntimeError("rollback")
        self.assertEqual(
            self.store.event_page(self.scope, self.initial.id)["head_sequence"], 1
        )

    def test_page_does_not_mix_new_commit_with_an_older_head(self):
        inserted = False

        def concurrent_commit(connection, cursor, statement, parameters, context, many):
            nonlocal inserted
            if not inserted and "FROM agent_events" in statement:
                inserted = True
                self.append()

        event.listen(self.engine, "before_cursor_execute", concurrent_commit)
        try:
            first = self.store.event_page(self.scope, self.initial.id)
        finally:
            event.remove(self.engine, "before_cursor_execute", concurrent_commit)
        self.assertTrue(inserted)
        self.assertEqual(first["head_sequence"], 1)
        self.assertEqual([item["sequence"] for item in first["items"]], [1])
        later = self.store.event_page(self.scope, self.initial.id, after_sequence=1)
        self.assertEqual([item["sequence"] for item in later["items"]], [2])

    def test_submission_drains_history_then_closes_without_resuming_or_running(self):
        self.append()
        with self.factory.begin() as db:
            db.get(ExamSession, self.scope).status = "submitting"
        with self.client() as client:
            frames = self.frames(client.get(self.url))
        self.assertEqual(
            [item["data"]["sequence"] for item in frames if "id" in item], [1, 2]
        )
        self.assertEqual(frames[-1]["event"], "stream.closed")
        self.assertEqual(self.runtime.mock_calls, [])

    def test_loopback_delivers_new_commit_before_connection_ends_and_handles_revocation(
        self,
    ):
        asyncio.run(self.live_stream())

    async def live_stream(self):
        policy = EventStreamPolicy(
            max_seconds=2, heartbeat_seconds=0.1, poll_seconds=0.02
        )
        app = self.app(agent_event_policy=policy)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        port = sock.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                app, lifespan="off", log_level="error", access_log=False, ws="none"
            )
        )
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:

            async def ready():
                while not server.started:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(ready(), 2)
            records, end = [], None
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                timeout=3,
                headers=store_tests.HEADERS,
                cookies={"vibeeval_candidate": self.browser},
            ) as client:
                async with client.stream("GET", self.url) as response:
                    self.assertEqual(response.status_code, 200)
                    block = []
                    async for line in response.aiter_lines():
                        block.append(line)
                        if line:
                            continue
                        decoded = decode_frames([("\n".join(block) + "\n").encode()])
                        block = []
                        if not decoded:
                            continue
                        item = decoded[0]
                        if "id" in item:
                            records.append(item["data"]["sequence"])
                            if len(records) == 1:
                                self.append()
                            else:
                                app.state.agent_enabled = False
                        elif item["event"] == "stream.error":
                            end = item
                            break
            self.assertEqual(records, [1, 2])
            self.assertEqual(end["data"]["code"], "AGENT_DISABLED")
            self.assertEqual(end["data"]["next_sequence"], 2)
            self.assertEqual(self.runtime.mock_calls, [])
        finally:
            server.should_exit = True
            try:
                await asyncio.wait_for(task, 3)
            finally:
                sock.close()

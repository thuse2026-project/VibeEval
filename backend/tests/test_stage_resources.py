"""Real PostgreSQL phase bindings, native Agent runtime and HTTP/WS ownership."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_exam_flow_postgres as flow_tests
from exam_flow_support import Questions, definition
from fastapi.testclient import TestClient
from sqlalchemy import select
from stage_resource_support import FILES, Model, Sandbox, Terminals, completion, spec
from starlette.websockets import WebSocketDisconnect

from app.agent import create_conversation
from app.agent_integration.reading_types import ReadingError
from app.agent_integration.runtime_types import RunInput
from app.agent_integration.store import ConversationNotFound, PostgresSessionStore
from app.db.models import AgentOperation, AgentRun, StageFileRevision, StageWorkspace
from app.exam_flow import ExamFlowService, FlowError, PostgresFlowStore
from app.exams.postgres import PostgresEntryStore
from app.llm.config import LLMSettings
from app.main import create_app
from app.stage_resources import StageResources
from app.submissions.storage import SnapshotStore


@unittest.skipUnless(
    os.environ.get("VIBEEVAL_RUN_DB_TESTS") == "1", "Requires isolated PostgreSQL"
)
class StageResourceTests(unittest.TestCase):
    setUpClass = classmethod(flow_tests.FlowPostgresTests.setUpClass.__func__)
    drop_database = classmethod(flow_tests.FlowPostgresTests.drop_database.__func__)

    def setUp(self):
        flow_tests.FlowPostgresTests.setUp(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.sandbox, self.model = Sandbox(), Model()
        self.snapshots = SnapshotStore(Path(temp.name) / "snapshots")
        self.gateway = StageResources(
            self.factory,
            {"provided": spec()},
            self.sandbox,
            snapshots=self.snapshots,
            settings=LLMSettings(
                "https://fixture.test/v1", "fixture-secret", "fixture/model"
            ),
            client=self.model,
            clock=lambda: self.now,
        )
        # Parent setUp attached a definition already. Replace only this fresh
        # test fixture's aggregate before starting, with the same four phases.
        value = definition().model_dump(mode="json")
        for stage in value["stages"]:
            stage["task_version"], stage["resource_ref"] = "provided-v1", "provided"
        with self.factory.begin() as db:
            from app.db.models import ExamFlowAttempt

            row = db.get(ExamFlowAttempt, self.sid)
            row.state_json = (
                self.flow.store.load(self.sid)
                .model_copy(deep=True)
                .model_dump(mode="json")
            )
            row.state_json["definition"] = value
            for state_stage, spec_stage in zip(
                row.state_json["stages"], value["stages"]
            ):
                state_stage["definition"] = spec_stage
        self.flow = ExamFlowService(
            PostgresFlowStore(self.factory),
            Questions(),
            self.gateway,
            clock=lambda: self.now,
        )

    def new_service(self):
        if not hasattr(self, "gateway"):
            return flow_tests.FlowPostgresTests.new_service(self)
        return ExamFlowService(
            PostgresFlowStore(self.factory),
            Questions(),
            self.gateway,
            clock=lambda: self.now,
        )

    def current(self):
        return self.flow.current(self.sid)

    def start(self, key):
        v = self.current()
        return self.flow.start(
            self.sid, v["current_stage"]["id"], v["state_version"], key
        )

    def submit(self, key):
        v = self.current()
        return self.flow.submit(
            self.sid, v["current_stage"]["id"], v["state_version"], key
        )

    def begin(self):
        v = self.current()
        self.flow.prepare(self.sid, v["state_version"], "prepare")
        return self.start("start")

    def coding(self):
        v = self.begin()
        self.flow.finish_phase(
            self.sid, v["current_phase"]["id"], v["state_version"], "finish"
        )
        self.submit("submit1")
        self.start("start2")
        self.submit("submit2")
        return self.start("start3")

    def test_live_terminal_and_file_api_revoked_on_submit(self):
        active = self.coding()
        phase = active["current_phase"]["id"]
        browser = "b" * 43
        entry = SimpleNamespace(
            factory=self.factory,
            current=lambda token: (
                {"session_id": self.sid} if token == browser else None
            ),
        )
        app = create_app({}, entry_store=entry, exam_flow=self.flow)
        terminals = Terminals()
        app.state.stage_terminal_factory = terminals
        with TestClient(app, headers={"X-VibeEval-Request": "1"}) as client:
            client.cookies.set("vibeeval_exam_flow", browser, path="/api/v2")
            file_path = f"/api/v2/phases/{phase}/file"
            read = client.get(file_path, params={"path": "solution.py"}).json()
            saved = client.put(
                file_path,
                json={
                    "path": "solution.py",
                    "expected_version": read["version"],
                    "content": "saved\n",
                },
            )
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(
                client.put(
                    file_path,
                    json={
                        "path": "solution.py",
                        "expected_version": 1,
                        "content": "stale",
                    },
                ).status_code,
                409,
            )
            with patch.dict(
                "os.environ", {"SANDBOX_TERMINAL_ORIGINS": "http://testserver"}
            ):
                with client.websocket_connect(
                    f"/api/v2/phases/{phase}/terminal",
                    headers={"origin": "http://testserver"},
                ) as socket:
                    socket.send_json({"type": "input", "data": "accepted"})
                    self.assertEqual(socket.receive_json()["data"], "accepted")
                    self.submit("submit3")
                    socket.send_json({"type": "input", "data": "late"})
                    with self.assertRaises(WebSocketDisconnect):
                        socket.receive_json()
            self.assertEqual(terminals.handles[0].writes, [b"accepted"])
            self.assertTrue(terminals.handles[0].closed)
            self.assertEqual(
                client.get(file_path, params={"path": "solution.py"}).status_code, 409
            )

    def conversation(self, phase):
        store = PostgresSessionStore(self.factory, clock=lambda: self.now)
        state = create_conversation(phase)
        store.create(state)
        return state.id

    def test_phase_close_removes_process_binding_and_changes_agent_scope(self):
        active = self.begin()
        phase, workspace = (
            active["current_phase"]["id"],
            active["current_stage"]["workspace_id"],
        )
        cid = self.conversation(phase)
        old_container = self.sandbox.containers[workspace]
        runtime = self.gateway.runtime_for(phase)
        run = runtime.submit(
            phase, cid, RunInput(request_key="turn", text="read", mode="read_only")
        )
        self.model.responses = [
            completion(calls=(("read_file", {"path": "solution.py"}),)),
            completion(),
        ]
        self.assertEqual(
            asyncio.run(runtime.advance(phase, cid, run["run_id"]))["status"],
            "completed",
        )
        retained = PostgresSessionStore(self.factory).load(phase, cid)
        self.assertFalse(retained.turns[0].steps[0].tool_results[0].is_error)
        active = self.current()
        next_phase = self.flow.finish_phase(
            self.sid, phase, active["state_version"], "finish"
        )
        self.assertNotEqual(old_container, self.sandbox.containers[workspace])
        self.assertNotEqual(next_phase["current_phase"]["id"], phase)
        with self.assertRaises(FlowError):
            self.gateway.agent_access(self.sid, phase)
        with self.assertRaises(FlowError):
            self.gateway.agent_access(self.sid, next_phase["current_phase"]["id"])

    def test_native_agent_reads_and_references_the_registered_manifest(self):
        phase = self.begin()["current_phase"]["id"]
        cid = self.conversation(phase)
        runtime = self.gateway.runtime_for(phase)
        ref = runtime.reader.create_reference(
            phase, cid, {"kind": "file", "path": "solution.py"}
        )
        runtime.reader.create_reference(
            phase,
            cid,
            {"kind": "selection", "path": "tests/case.py", "content": "excerpt"},
        )
        with self.assertRaises(ReadingError):
            runtime.reader.create_reference(
                phase, cid, {"kind": "file", "path": "unpublished.py"}
            )
        self.model.responses = [
            completion(
                calls=(
                    ("list_directory", {"path": "."}),
                    ("glob_files", {"pattern": "**/*.py"}),
                    ("search_content", {"query": "baseline"}),
                    ("read_file", {"path": "solution.py", "expected_version": 1}),
                    ("read_reference", {"reference_id": ref["reference_id"]}),
                )
            ),
            completion(),
        ]
        run = runtime.submit(phase, cid, RunInput(request_key="reads", text="read"))
        self.assertEqual(
            asyncio.run(runtime.advance(phase, cid, run["run_id"]))["status"],
            "completed",
        )
        retained = PostgresSessionStore(self.factory).load(phase, cid)
        results = retained.turns[0].steps[0].tool_results
        self.assertEqual(len(results), 5)
        self.assertFalse(any(r.is_error for r in results), results)
        self.assertEqual(json.loads(results[3].content)["text"], FILES["solution.py"])
        self.assertEqual(json.loads(results[4].content)["text"], FILES["solution.py"])

    def test_stage_file_versions_and_snapshot_are_isolated(self):
        active = self.coding()
        phase, workspace = (
            active["current_phase"]["id"],
            active["current_stage"]["workspace_id"],
        )
        read = self.gateway.read(self.sid, phase, "solution.py")
        self.gateway.save(self.sid, phase, "solution.py", read["version"], "fixed\n")
        receipt = self.submit("submit3")
        self.start("start4")
        current = self.current()
        fresh = self.gateway.read(
            self.sid, current["current_phase"]["id"], "solution.py"
        )
        self.assertEqual(fresh["content"], FILES["solution.py"])
        self.assertEqual(fresh["version"], 1)
        self.assertNotEqual(workspace, current["current_stage"]["workspace_id"])
        with self.factory.begin() as db:
            previous = db.get(StageWorkspace, workspace)
            artifact = previous.artifact_json
            self.assertEqual(previous.status, "sealed")
            self.assertIsNone(previous.sandbox_id)
        snapshot = self.snapshots.read(
            artifact["sha256"],
            size_bytes=artifact["size_bytes"],
            file_count=artifact["file_count"],
        )
        self.assertEqual(
            next(f.content for f in snapshot.files if f.path == "solution.py"),
            "fixed\n",
        )
        self.assertEqual(
            self.flow.export_submission(self.sid, receipt["stage_id"])["content"][
                "artifact"
            ]["sha256"],
            artifact["sha256"],
        )

    def test_native_agent_can_edit_registered_non_sample_file(self):
        active = self.coding()
        phase = active["current_phase"]["id"]
        cid = self.conversation(phase)
        self.gateway.tree(self.sid, phase)
        self.model.responses = [
            completion(
                calls=(
                    (
                        "write_file",
                        {
                            "path": "solution.py",
                            "expected_version": 1,
                            "content": "agent edit\n",
                        },
                    ),
                )
            ),
            completion(),
        ]
        runtime = self.gateway.runtime_for(phase)
        run = runtime.submit(
            phase, cid, RunInput(request_key="edit", text="edit", mode="sandbox_auto")
        )
        result = asyncio.run(runtime.advance(phase, cid, run["run_id"]))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            self.gateway.read(self.sid, phase, "solution.py")["content"], "agent edit\n"
        )
        with self.factory.begin() as db:
            self.assertEqual(
                db.get(
                    StageFileRevision,
                    (active["current_stage"]["workspace_id"], "solution.py"),
                ).version,
                2,
            )

    def test_phase_end_cancels_active_agent_command_and_rejects_old_history(self):
        active = self.coding()
        phase = active["current_phase"]["id"]
        cid = self.conversation(phase)
        self.sandbox.instant = False
        self.model.responses = [
            completion(
                calls=(
                    (
                        "run_command",
                        {
                            "argv": ["python", "solution.py"],
                            "expected_versions": {"solution.py": 1},
                        },
                    ),
                )
            )
        ]
        runtime = self.gateway.runtime_for(phase)
        run = runtime.submit(
            phase, cid, RunInput(request_key="run", text="run", mode="sandbox_auto")
        )
        waiting = asyncio.run(runtime.advance(phase, cid, run["run_id"]))
        self.assertEqual(waiting["status"], "waiting_operation")
        self.submit("submit3")
        self.assertTrue(
            all(j["status"] == "cancelled" for j in self.sandbox.jobs.values())
        )
        with self.factory.begin() as db:
            self.assertEqual(db.get(AgentRun, run["run_id"]).status, "interrupted")
            self.assertFalse(
                any(
                    o.holds_workspace
                    for o in db.scalars(
                        select(AgentOperation).where(
                            AgentOperation.exam_session_id == phase
                        )
                    )
                )
            )
        self.start("start4")
        next_phase = self.current()["current_phase"]["id"]
        with self.assertRaises(ConversationNotFound):
            PostgresSessionStore(self.factory).describe(next_phase, cid)

    def test_provider_request_is_cancelled_after_phase_revocation(self):
        active = self.begin()
        phase = active["current_phase"]["id"]
        cid = self.conversation(phase)

        async def exercise():
            entered, cancelled = asyncio.Event(), asyncio.Event()

            class Delayed:
                async def complete(_, payload):
                    entered.set()
                    try:
                        await asyncio.sleep(30)
                    except asyncio.CancelledError:
                        cancelled.set()
                        raise

            runtime = self.gateway.runtime_for(phase)
            runtime.client = Delayed()
            run = runtime.submit(phase, cid, RunInput(request_key="slow", text="slow"))
            task = asyncio.create_task(runtime.advance(phase, cid, run["run_id"]))
            await entered.wait()
            value = self.current()
            await asyncio.to_thread(
                self.flow.finish_phase,
                self.sid,
                phase,
                value["state_version"],
                "finish",
            )
            await asyncio.wait_for(task, 5)
            self.assertTrue(cancelled.is_set())

        asyncio.run(exercise())

    def test_freeze_export_failures_remain_fenced_and_retry_original_workspace(self):
        active = self.coding()
        workspace, phase = (
            active["current_stage"]["workspace_id"],
            active["current_phase"]["id"],
        )
        self.sandbox.fail_close = True
        with self.assertRaises(FlowError):
            self.submit("submit3")
        self.assertEqual(self.current()["capabilities"], [])
        with self.assertRaises(FlowError):
            self.gateway.save(self.sid, phase, "solution.py", 1, "late")
        self.sandbox.fail_close, self.sandbox.fail_export = False, True
        self.now += 5
        self.flow.reconcile(self.sid)
        self.assertEqual(self.current()["current_stage"]["workspace_id"], workspace)
        self.assertEqual(self.current()["error_code"], "RESOURCE_SEAL_FAILED")
        self.sandbox.fail_export = False
        self.now += 5
        self.flow.reconcile(self.sid)
        self.assertEqual(self.current()["current_stage"]["ordinal"], 4)

    def test_http_files_agent_and_terminal_use_v2_identity_and_phase_permissions(self):
        entry = PostgresEntryStore(self.factory)
        token, _ = entry.invite("http-stage", "Candidate", "http-stage", "Exam", 60, 24)
        app = create_app({}, entry_store=entry, exam_flow=self.flow)
        terminals = Terminals()
        app.state.stage_terminal_factory = terminals
        with TestClient(app, headers={"X-VibeEval-Request": "1"}) as client:
            client.get("/api/v1/sessions/current")
            admitted = client.post(
                "/api/v1/sessions/verify-token", json={"token": token}
            ).json()
            sid = admitted["session_id"]
            self.flow.attach(sid, self.flow.store.load(self.sid).definition)
            v = self.flow.current(sid)
            self.flow.prepare(sid, v["state_version"], "prepare")
            v = self.flow.current(sid)
            active = self.flow.start(
                sid, v["current_stage"]["id"], v["state_version"], "start"
            )
            phase = active["current_phase"]["id"]
            self.assertEqual(
                client.post(f"/api/v1/sessions/{sid}/exam-access").status_code, 200
            )
            self.assertEqual(
                client.get(f"/api/v2/phases/{phase}/files").status_code, 200
            )
            self.assertEqual(
                client.put(
                    f"/api/v2/phases/{phase}/file",
                    json={
                        "path": "solution.py",
                        "expected_version": 1,
                        "content": "denied",
                    },
                ).status_code,
                403,
            )
            self.assertEqual(
                client.post(
                    f"/api/v2/phases/{phase}/agent/conversations", json={}
                ).status_code,
                201,
            )
            with patch.dict(
                "os.environ", {"SANDBOX_TERMINAL_ORIGINS": "http://testserver"}
            ):
                with self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect(
                        f"/api/v2/phases/{phase}/terminal",
                        headers={"origin": "http://testserver"},
                    ):
                        pass
            self.assertEqual(terminals.handles, [])
            conversations = client.get(
                f"/api/v2/phases/{phase}/agent/conversations"
            ).json()
            conversation_id = conversations["items"][0]["conversation_id"]
            reference_path = f"/api/v2/phases/{phase}/agent/conversations/{conversation_id}/references"
            for reference in (
                {"kind": "file", "path": "solution.py"},
                {"kind": "selection", "path": "tests/case.py", "content": "excerpt"},
                {"kind": "directory", "path": "tests"},
            ):
                self.assertEqual(
                    client.post(reference_path, json=reference).status_code, 201
                )
            self.assertEqual(
                client.post(
                    reference_path, json={"kind": "file", "path": "unpublished.py"}
                ).status_code,
                404,
            )
            self.assertEqual(
                client.post(
                    reference_path, json={"kind": "file", "path": "../escape.py"}
                ).status_code,
                422,
            )
            stream_path = f"/api/v2/phases/{phase}/agent/conversations/{conversation_id}/events/stream"
            stream = client.get(stream_path, params={"follow": "false"})
            self.assertEqual(stream.status_code, 200)
            self.assertIn("conversation.created", stream.text)
            value = self.flow.current(sid)
            self.flow.finish_phase(sid, phase, value["state_version"], "http-finish")
            self.assertEqual(
                client.get(stream_path, params={"follow": "false"}).status_code, 409
            )
            # Scope identifiers from a different admitted candidate cannot be reused.
            foreign = self.begin()["current_phase"]["id"]
            self.assertNotEqual(sid, self.sid)
            self.assertEqual(
                client.get(f"/api/v2/phases/{foreign}/files").status_code, 409
            )

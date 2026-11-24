"""Real PostgreSQL phase bindings, native Agent runtime and HTTP/WS ownership."""

import asyncio
import json
import os
import secrets
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
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
from app.db.models import (
    AgentOperation,
    AgentRun,
    ExamSession,
    StageFileRevision,
    StageWorkspace,
)
from app.exam_flow import ExamFlowService, FlowError, PostgresFlowStore
from app.exams.postgres import PostgresEntryStore
from app.exams.store import digest
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

    def test_examiner_reads_sealed_text_answers_without_code_snapshot(self):
        from app.admin.accounts import hash_password
        from examples.datasette_exam import export_stage_submission

        active = self.begin()
        phase_id = active["current_phase"]["id"]
        self.flow.save_notes(self.sid, phase_id, 0, "exploration note")
        answering = self.flow.finish_phase(
            self.sid, phase_id, self.current()["state_version"], "finish"
        )
        self.flow.save_answer(
            self.sid, answering["current_phase"]["id"], "item", 0, "saved answer"
        )
        self.submit("submit1")
        exported = export_stage_submission(
            self.flow, self.sid, active["current_stage"]["id"]
        )
        self.assertIsNone(exported["content"]["artifact"])
        self.assertEqual(exported["content"]["notes"]["value"], "exploration note")
        self.assertEqual(
            exported["content"]["phases"][1]["answers"]["item"]["value"], "saved answer"
        )
        app = create_app(
            {
                "teacher": {
                    "name": "Teacher",
                    "password_hash": hash_password("fixture-password"),
                }
            },
            entry_store=SimpleNamespace(factory=self.factory),
            exam_flow=self.flow,
        )
        base = "/api/v1/admin/r2/submissions"
        endpoint = f"{base}/{exported['receipt']['id']}/answer-sheet"
        with TestClient(app, headers={"X-VibeEval-Request": "1"}) as client:
            self.assertEqual(client.get(endpoint).status_code, 401)
            login = client.post(
                "/api/v1/admin/login",
                json={"username": "teacher", "password": "fixture-password"},
            )
            self.assertEqual(login.status_code, 200)
            listing = client.get(base, params={"kind": "text", "exam_id": "external"})
            self.assertIn(
                exported["receipt"]["id"],
                [item["submission_id"] for item in listing.json()["items"]],
            )
            response = client.get(endpoint)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["content"], exported["content"])
            self.assertEqual(response.json()["receipt"], exported["receipt"])

    def test_sealed_answer_creates_one_examiner_job_on_concurrent_retries(self):
        from copy import deepcopy

        from app.admin.router import current_session
        from app.db.models import ExamFlowAttempt, StageGradingJob

        with self.factory.begin() as db:
            row = db.get(ExamFlowAttempt, self.sid)
            state = deepcopy(row.state_json)
            state["definition"]["key"] = "r2-four-stage"
            row.state_json = state
        active = self.begin()
        answering = self.flow.finish_phase(
            self.sid, active["current_phase"]["id"], active["state_version"], "finish"
        )
        self.flow.save_answer(
            self.sid, answering["current_phase"]["id"], "item", 0, "my answer"
        )
        receipt = self.submit("submit1")
        app = create_app(
            {}, entry_store=SimpleNamespace(factory=self.factory), exam_flow=self.flow
        )
        endpoint = "/api/v1/admin/r2/submissions/" + receipt["id"] + "/grading-job"
        with TestClient(app, headers={"X-VibeEval-Request": "1"}) as client:
            self.assertEqual(client.get(endpoint).status_code, 401)
            self.assertEqual(client.post(endpoint).status_code, 401)
            app.dependency_overrides[current_session] = lambda: SimpleNamespace(
                username="examiner"
            )
            self.assertEqual(client.get(endpoint).status_code, 404)
            with ThreadPoolExecutor(2) as pool:
                responses = list(pool.map(lambda _: client.post(endpoint), range(2)))
            for response in responses:
                self.assertEqual(response.status_code, 200, response.text)
            job = client.get(endpoint).json()
            self.assertEqual(job, responses[0].json())
            self.assertEqual(job, responses[1].json())
            self.assertEqual(
                job["basis"]["answers"],
                [
                    {
                        "phase_id": answering["current_phase"]["id"],
                        "question_id": "item",
                        "revision": 1,
                    }
                ],
            )
            with self.factory.begin() as db:
                db.get(StageGradingJob, receipt["id"]).components = {
                    "answers": {"status": "reviewed", "score": 5}
                }
            self.assertEqual(
                client.post(endpoint).json()["components"]["answers"]["score"], 5
            )

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
        with patch.object(
            self.sandbox,
            "read_files",
            side_effect=AssertionError("directory discovery must not read file bodies"),
        ):
            self.assertEqual(
                asyncio.run(runtime.advance(phase, cid, run["run_id"]))["status"],
                "completed",
            )
        retained = PostgresSessionStore(self.factory).load(phase, cid)
        results = retained.turns[0].steps[0].tool_results
        self.assertEqual(len(results), 5)
        self.assertFalse(any(r.is_error for r in results), results)
        glob = json.loads(json.loads(results[1].content)["text"])
        self.assertEqual(
            {entry["path"] for entry in glob["items"]},
            {path for path in FILES if path.endswith(".py")},
        )
        self.assertTrue(all("version" not in entry for entry in glob["items"]))
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

    def test_interview_and_export_share_frozen_evidence_after_retry(self):
        # Extend only this fresh fixture's third stage before its first start.
        from copy import deepcopy

        from app.db.models import ExamFlowAttempt
        from app.exam_flow.types import EvidenceCutoff, PhaseDefinition, PhaseState
        from app.stage_resources.evidence import page

        phase = PhaseDefinition(
            key="interview",
            title="独立问答",
            duration_seconds=600,
            question_ref="questions",
            capabilities={"read_repository", "read_frozen_code", "edit_answers"},
        )
        with self.factory.begin() as db:
            row = db.get(ExamFlowAttempt, self.sid)
            data = deepcopy(row.state_json)
            data["definition"]["key"] = "r2-four-stage"
            for definition in (
                data["definition"]["stages"][2],
                data["stages"][2]["definition"],
            ):
                definition["phases"].append(phase.model_dump(mode="json"))
            data["stages"][2]["phases"].append(
                PhaseState(id="phase_interview", definition=phase).model_dump(
                    mode="json"
                )
            )
            row.state_json = data
        active = self.coding()
        coding, stage = active["current_phase"]["id"], active["current_stage"]["id"]
        self.gateway.save(self.sid, coding, "solution.py", 1, "changed\n")
        from app.db.models import StageEvidence

        with self.factory() as db:
            edited = db.scalar(
                select(StageEvidence).where(
                    StageEvidence.phase_id == coding, StageEvidence.sequence == 1
                )
            ).payload
        started = dict(
            id="run1-start",
            actor="candidate",
            kind="test_run",
            at=self.now,
            data={
                "run_id": "run1",
                "command": "python -m unittest",
                "status": "started",
                "exit_code": None,
                "code_version": "v2",
            },
        )
        self.assertEqual(self.gateway.append_evidence(self.sid, coding, started), 2)
        self.assertEqual(self.gateway.append_evidence(self.sid, coding, started), 2)
        finished = dict(
            started,
            id="run1-end",
            data={**started["data"], "status": "completed", "exit_code": 0},
        )
        with self.assertRaisesRegex(FlowError, "EVIDENCE_ID_CONFLICT"):
            self.gateway.append_evidence(
                self.sid, coding, {**finished, "id": started["id"]}
            )
        self.sandbox.fail_export = True
        failed = self.flow.finish_phase(
            self.sid, coding, self.current()["state_version"], "finish-coding"
        )
        self.assertEqual(failed["error_code"], "RESOURCE_CLOSE_FAILED")
        self.assertEqual(failed["current_phase"]["status"], "closing")
        with self.assertRaisesRegex(FlowError, "PHASE_CLOSED"):
            self.gateway.append_evidence(self.sid, coding, finished)
        self.sandbox.fail_export = False
        self.now = failed["retry_at"]
        self.flow = self.new_service()
        active = self.current()
        interview = active["current_phase"]["id"]
        self.assertEqual(active["current_phase"]["deadline_at"], self.now + 600)
        frozen = self.gateway.frozen_code(self.sid, interview)
        with self.assertRaisesRegex(FlowError, "PHASE_CLOSED"):
            self.gateway.evidence_page(self.sid, coding, None, 50)
        with self.assertRaisesRegex(FlowError, "EVIDENCE_CURSOR_INVALID"):
            self.gateway.evidence_page(self.sid, interview, "another:1", 1)
        browser = "c" * 43
        entry = SimpleNamespace(
            factory=self.factory,
            current=lambda token: (
                {"session_id": self.sid} if token == browser else None
            ),
        )
        app = create_app({}, entry_store=entry, exam_flow=self.flow)
        with TestClient(app, headers={"X-VibeEval-Request": "1"}) as client:
            endpoint = f"/api/v2/phases/{interview}/evidence"
            self.assertEqual(client.get(endpoint).status_code, 401)
            client.cookies.set("vibeeval_exam_flow", browser, path="/api/v2")
            self.assertEqual(
                client.get(f"/api/v2/phases/{interview}/frozen-code").json(), frozen
            )
            first_response = client.get(endpoint, params={"limit": 1})
            self.assertEqual(first_response.status_code, 200, first_response.text)
            view = first_response.json()
            second = client.get(
                endpoint, params={"limit": 1, "cursor": view["next_cursor"]}
            ).json()
        self.assertEqual(
            (view["items"], second["items"], second["next_cursor"]),
            ([edited], [started], None),
        )
        self.assertEqual(second["sha256"], frozen["sha256"])
        self.assertEqual(second["completeness"], "partial")
        self.assertEqual(second["reason"], "CAPTURE_GAP")
        self.sandbox.volumes[frozen["workspace_id"]][
            "solution.py"
        ] = "different after freeze"
        self.assertEqual(
            self.gateway.read(self.sid, interview, "solution.py")["content"],
            "changed\n",
        )
        self.flow.save_answer(self.sid, interview, "item", 0, "a saved answer")
        receipt = self.submit("submit3")
        artifact = self.flow.export_submission(self.sid, stage)["content"]["artifact"]
        self.assertEqual(artifact["sha256"], frozen["sha256"])
        with self.factory() as db:
            items, following = page(
                db, EvidenceCutoff.model_validate(artifact["evidence"])
            )
        self.assertEqual((items, following), ([edited, started], None))
        with self.assertRaisesRegex(FlowError, "PHASE_CLOSED"):
            self.gateway.evidence_page(self.sid, interview, None, 50)
        from app.admin.router import current_session

        app.dependency_overrides[current_session] = lambda: SimpleNamespace(
            username="reviewer"
        )
        url = "/api/v1/admin/r2/submissions/" + receipt["id"] + "/grading-job"
        with TestClient(app, headers={"X-VibeEval-Request": "1"}) as examiner:
            response = examiner.post(url)
            self.assertEqual(response.status_code, 200, response.text)
            job = response.json()
            self.assertEqual(job["basis"]["code_sha256"], frozen["sha256"])
            self.assertEqual(
                job["basis"]["evidence"]["event_counts"], {"edit": 1, "test_run": 1}
            )
            self.assertIsNone(job["components"]["process"]["score"])
            with self.factory.begin() as db:
                db.delete(db.get(StageEvidence, (coding, 2)))
            self.assertEqual(examiner.post(url).status_code, 503)
            self.assertEqual(examiner.get(url).json(), job)
        # Even an internally consistent receipt cannot turn a known saved answer
        # into a normally unanswered question.
        from app.exam_flow.types import digest as content_digest
        from examples.r2_interview_grading import export_interview_submission

        with self.factory.begin() as db:
            attempt = db.get(ExamFlowAttempt, self.sid)
            data = deepcopy(attempt.state_json)
            closed = data["stages"][2]
            closed["submission"]["phases"][-1]["answers"].clear()
            closed["receipt"]["content_sha256"] = content_digest(closed["submission"])
            attempt.state_json = data
        with self.assertRaisesRegex(FlowError, "SEALED_ANSWERS_MISMATCH"):
            export_interview_submission(self.flow, self.sid, stage)

    def test_four_stage_objective_recovers_and_preserves_other_components(self):
        from app.admin.stage_evaluation import grade_job_objective
        from app.db.models import StageGradingJob
        from app.exam_flow.types import digest as content_digest

        stage = "stage_objective"
        sid = "submission_" + "a" * 32
        content = {
            "definition": "r2-four-stage",
            "definition_version": "v1",
            "stage_id": stage,
            "task_version": "bug-v1",
            "phases": [
                {
                    "id": "phase_test_job",
                    "definition": {"key": "coding", "capabilities": []},
                }
            ],
            "artifact": {
                "sha256": "1" * 64,
                "evidence": {
                    "coding_phase_id": "phase_test_job",
                    "sequence": 0,
                    "completeness": "partial",
                    "reason": "CAPTURE_GAP",
                },
            },
        }
        exported = {
            "content": content,
            "files": [],
            "receipt": {
                "id": sid,
                "kind": "code",
                "content_sha256": content_digest(content),
            },
        }
        metadata = {"stage_id": stage, "submission_id": sid, "image": "test"}
        rule = {
            "fingerprint": "rules",
            "rule_version": "v1",
            "failure_cap": 50,
            "criteria": [{"key": "core", "max": 70, "core": True}],
        }
        failure = {"status": "infrastructure_failed", "score": None}
        completed = {
            "status": "completed",
            "score": 0,
            "max": 70,
            "cap": 50,
            "groups": [
                {"key": "core", "max": 70, "core": True, "passed": False, "score": 0}
            ],
        }
        with (
            tempfile.TemporaryDirectory() as temp,
            patch(
                "app.admin.stage_evaluation.evaluate", side_effect=[failure, completed]
            ) as runner,
        ):

            def grade():
                return grade_job_objective(
                    self.gateway, metadata, exported, rule, Path(temp)
                )

            self.assertIsNone(grade()["score"])
            with self.factory.begin() as db:
                job = db.get(StageGradingJob, sid)
                job.components = {
                    **job.components,
                    "answers": {"status": "reviewed", "score": 15},
                }
            report = grade()
            self.assertEqual((report["score"], report["cap"]), (0, 50))
            self.assertEqual(grade(), report)
            # Simulate a restart between the atomic file write and DB commit.
            with self.factory.begin() as db:
                job = db.get(StageGradingJob, sid)
                job.components = {
                    **job.components,
                    "objective": {"status": "pending", "score": None},
                }
            self.assertEqual(grade(), report)
            self.assertEqual(runner.call_count, 2)
            with self.factory() as db:
                components = db.get(StageGradingJob, sid).components
                self.assertEqual(components["objective"], report)
                self.assertEqual(components["answers"]["score"], 15)
                self.assertEqual(components["process"]["status"], "needs_review")
            from fastapi import HTTPException

            with (
                patch.dict(rule, fingerprint="modified-rule"),
                self.assertRaises(HTTPException) as conflict,
            ):
                grade()
            self.assertEqual(conflict.exception.status_code, 409)
            with self.factory.begin() as db:
                job = db.get(StageGradingJob, sid)
                job.components = {
                    **job.components,
                    "objective": {**report, "score": 70},
                }
            with self.assertRaises(HTTPException) as corrupt:
                grade()
            self.assertEqual(corrupt.exception.status_code, 503)
            self.assertEqual(runner.call_count, 2)

    def test_candidate_test_runs_record_server_command_output_and_code_version(self):
        active = self.coding()
        phase = active["current_phase"]["id"]
        self.gateway.save(self.sid, phase, "solution.py", 1, "changed\n")

        result = self.gateway.run_test(self.sid, phase, "python -m unittest")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["evidence_recorded"])
        self.assertEqual(len(result["code_version"]), 64)

        from app.db.models import StageEvidence

        with self.factory() as db:
            items = db.scalars(
                select(StageEvidence)
                .where(StageEvidence.phase_id == phase)
                .order_by(StageEvidence.sequence)
            ).all()
        start, finish = (
            item.payload for item in items if item.payload["kind"] == "test_run"
        )
        self.assertNotEqual(start["id"], finish["id"])
        self.assertEqual(start["data"]["run_id"], finish["data"]["run_id"])
        self.assertEqual(start["data"]["command"], "python -m unittest")
        self.assertEqual(start["data"]["status"], "started")
        self.assertEqual(finish["data"]["status"], "completed")
        self.assertEqual(finish["data"]["exit_code"], 0)
        self.assertEqual(finish["data"]["output"], "Ran: python -m unittest")
        self.assertEqual(finish["data"]["code_version"], result["code_version"])

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

    def test_background_run_completes_and_can_be_stopped_without_browser_advance(self):
        phase = self.begin()["current_phase"]["id"]
        cid = self.conversation(phase)
        runtime = self.gateway.runtime_for(phase)
        browser = secrets.token_urlsafe(32)
        with self.factory.begin() as db:
            db.get(ExamSession, self.sid).browser_hash = digest(browser)
        app = create_app(
            {},
            entry_store=PostgresEntryStore(self.factory),
            exam_flow=self.flow,
            stage_agent_worker=True,
        )
        headers = {"X-VibeEval-Request": "1"}
        cookies = {"vibeeval_exam_flow": browser}
        path = f"/api/v2/phases/{phase}/agent/conversations/{cid}"
        self.model.responses = [completion("计划完成")]

        with TestClient(app, headers=headers, cookies=cookies) as client:
            queued = client.post(
                path + "/turns",
                json={
                    "request_key": "worker-plan",
                    "text": "只给计划",
                    "intent": "plan",
                },
            )
            self.assertEqual(queued.status_code, 202)
            self.assertTrue(queued.json()["server_driven"])
            run_path = path + "/runs/" + queued.json()["run_id"]
            for _ in range(30):
                current = client.get(run_path).json()
                if current.get("status") == "completed":
                    break
                time.sleep(0.05)
            self.assertEqual(current["status"], "completed")
            self.assertTrue(current["server_driven"])

            entered, cancelled = Event(), Event()

            class Delayed:
                async def complete(_, payload):
                    entered.set()
                    try:
                        await asyncio.sleep(30)
                    finally:
                        cancelled.set()

            runtime.client = Delayed()
            queued = client.post(
                path + "/turns",
                json={
                    "request_key": "worker-stop",
                    "text": "只读",
                    "intent": "plan",
                },
            ).json()
            self.assertTrue(entered.wait(2))
            run_path = path + "/runs/" + queued["run_id"]
            self.assertEqual(client.get(run_path).json()["status"], "running")
            self.assertEqual(
                client.post(run_path + "/interrupt").json()["status"], "interrupted"
            )
            self.assertTrue(cancelled.wait(2))
            self.assertEqual(client.get(run_path).json()["status"], "interrupted")

    def test_slow_read_retries_when_a_new_version_is_saved_during_io(self):
        phase = self.coding()["current_phase"]["id"]
        entered, release = Event(), Event()
        read_file = self.sandbox.read_file

        def delayed(view, path):
            content = read_file(view, path)
            if not entered.is_set():
                entered.set()
                release.wait(3)
            return content

        with patch.object(self.sandbox, "read_file", side_effect=delayed):
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending = pool.submit(self.gateway.read, self.sid, phase, "solution.py")
                self.assertTrue(entered.wait(2))
                try:
                    saved = pool.submit(
                        self.gateway.save, self.sid, phase, "solution.py", 1, "new\n"
                    )
                    self.assertEqual(saved.result(2)["new_version"], 2)
                finally:
                    release.set()
                self.assertEqual(pending.result(2)["content"], "new\n")

    def test_slow_read_does_not_return_content_after_phase_closes(self):
        phase = self.coding()["current_phase"]["id"]
        entered, release = Event(), Event()
        read_file = self.sandbox.read_file

        def delayed(view, path):
            content = read_file(view, path)
            entered.set()
            release.wait(3)
            return content

        with (
            patch.object(self.sandbox, "read_file", side_effect=delayed),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            pending = pool.submit(self.gateway.read, self.sid, phase, "solution.py")
            self.assertTrue(entered.wait(2))
            try:
                pool.submit(self.submit, "close-during-read").result(2)
            finally:
                release.set()
            with self.assertRaises(FlowError):
                pending.result(2)

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

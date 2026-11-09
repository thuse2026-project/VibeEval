"""Reading adapters preserve the native model protocol and persistent provenance."""

import asyncio
import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import httpx
import test_db_postgres as database_tests
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.agent import (
    accept_response,
    begin_turn,
    call_model,
    create_conversation,
    interrupt_turn,
    prepare_model_request,
    start_step,
    supply_tool_results,
)
from app.agent.prompts import GLOBAL_AGENT_PROMPT
from app.agent.session_types import AssistantOutput, ModelResponse, StaleStep, ToolCall
from app.agent_integration.material_store import MaterialStore
from app.agent_integration.materials import (
    SourceSnapshot,
    digest,
    encoded,
    fragment,
    selection_bounds,
)
from app.agent_integration.read_tools import ReadToolService
from app.agent_integration.reading_types import (
    LineSelection,
    ReadDecision,
    ReadingApprovalRequired,
    ReadingError,
    Utf16Selection,
)
from app.agent_integration.sources import SourceReader, matches, safe_relative
from app.agent_integration.store import (
    ConversationClosed,
    ConversationNotFound,
    PostgresSessionStore,
)
from app.db.lifecycle import SessionLifecycle
from app.db.models import (
    AgentInputReference,
    AgentMaterial,
    AgentReadObservation,
    ExamSession,
    WorkspaceFileRevision,
)
from app.exams.postgres import PostgresEntryStore
from app.llm.client import LLMClient
from app.llm.config import LLMSettings
from app.main import create_app
from app.workspace.files import ALLOWED_FILES
from app.workspace.service import WorkspaceReader


class MaterialProjectionTests(unittest.TestCase):
    def test_utf16_crlf_and_inclusive_lines_keep_precise_offsets(self):
        text = "a😀b\r\n第二行\n"
        selection = Utf16Selection(
            start_line=1, start_column=2, end_line=1, end_column=4
        )
        result = fragment(SourceSnapshot("file", "x", "1", text), selection=selection)
        self.assertEqual(result["text"], "😀")
        self.assertEqual((result["start_offset"], result["end_offset"]), (1, 2))
        self.assertEqual((result["byte_start"], result["byte_end"]), (1, 5))
        self.assertEqual(result["returned_sha256"], digest("😀"))
        self.assertEqual(
            selection_bounds(text, LineSelection(start_line=1, end_line=2)),
            (0, len(text)),
        )
        with self.assertRaisesRegex(ReadingError, "SPLITS_UTF16"):
            selection_bounds(
                text,
                Utf16Selection(start_line=1, start_column=3, end_line=1, end_column=4),
            )
        with self.assertRaisesRegex(ReadingError, "OUT_OF_RANGE"):
            selection_bounds(text, LineSelection(start_line=8, end_line=9))
        self.assertEqual(
            selection_bounds(
                "one\vline\nsecond", LineSelection(start_line=2, end_line=2)
            ),
            (9, 15),
        )

    def test_snapshot_is_deeply_immutable_and_roundtrips_identity(self):
        metadata = {"nested": ["original"]}
        snapshot = SourceSnapshot("file", "x", "1", "", metadata)
        key = snapshot.key
        metadata["nested"].append("changed")
        snapshot.metadata["nested"].append("changed again")
        self.assertEqual(snapshot.key, key)
        self.assertEqual(snapshot.metadata, {"nested": ["original"]})
        restored = SourceSnapshot(
            "file", "x", "1", "", snapshot.metadata, snapshot.source_digest
        )
        self.assertEqual(restored.key, key)
        self.assertEqual(fragment(snapshot)["text"], "")
        self.assertFalse(fragment(snapshot)["has_more"])

    def test_bounded_json_unicode_and_invalid_sources(self):
        snapshot = SourceSnapshot("file", "x", "1", "\x01" * 16000)
        result = fragment(snapshot, max_chars=16000)
        self.assertLess(len(encoded(result)), 64000)
        self.assertTrue(result["has_more"])
        self.assertEqual(
            fragment(snapshot, offset=result["next_offset"])["start_offset"],
            result["end_offset"],
        )
        for options in ({"offset": -1}, {"offset": 16001}, {"max_chars": 16001}):
            with self.assertRaises(ReadingError):
                fragment(snapshot, **options)
        for text in ("\x00", "\ud800", "a" * (8 * 1024 * 1024 + 1)):
            with self.assertRaises(ReadingError):
                SourceSnapshot("file", "x", "1", text)
        with self.assertRaises(ReadingError):
            SourceSnapshot("query", "x", "1", "", {"query": "\x00"})
        with self.assertRaises(ReadingError):
            digest("\ud800")

    def test_patterns_never_resolve_host_paths(self):
        for path in ("../x", "/tmp/x", "C:/x", "src\\x", "src//x"):
            with self.assertRaises(ReadingError):
                safe_relative(path, pattern=True)
        self.assertTrue(matches("README.md", "**/*"))
        self.assertTrue(matches("src/main.py", "**/*.py"))


class WorkspaceFixture:
    def __init__(self):
        self.contents = {path: "fixture\n" for path in ALLOWED_FILES}
        self.contents["src/main.py"] = "first\nneedle 😀\nlast\n"
        self.version = 1
        self.calls = 0

    def read(self, scope, path):
        self.calls += 1
        return {"path": path, "content": self.contents[path], "version": self.version}

    def tree(self, scope):
        self.calls += 1
        return {
            "files": [
                {
                    "path": path,
                    "is_dir": False,
                    "version": self.version,
                    "readonly": True,
                }
                for path in sorted(self.contents)
            ]
        }


@unittest.skipUnless(
    os.environ.get("VIBEEVAL_RUN_DB_TESTS") == "1", "Requires isolated PostgreSQL"
)
class AgentIntegrationReadingTests(unittest.TestCase):
    setUpClass = classmethod(database_tests.PostgreSQLTests.setUpClass.__func__)
    drop_test_database = classmethod(
        database_tests.PostgreSQLTests.drop_test_database.__func__
    )

    def setUp(self):
        database_tests.PostgreSQLTests.setUp(self)
        self.entry = PostgresEntryStore(self.factory)
        token, _ = self.entry.invite("reader", "考生", "read-exam", "阅读验收", 60, 24)
        self.browser = "r" * 43
        self.scope = self.entry.admit(token, self.browser)["session_id"]
        self.conversations = PostgresSessionStore(self.factory)
        self.initial = create_conversation(self.scope)
        self.conversations.create(self.initial)
        self.materials = MaterialStore(self.factory)
        self.workspace = WorkspaceFixture()
        self.reader = SourceReader(self.materials, workspace=self.workspace)
        self.tools = ReadToolService(self.reader)

    def save(self, before, after):
        self.conversations.save(after, expected_revision=before.revision)
        return after

    def pending(self, name, arguments, *, state=None):
        state = state or self.conversations.load(self.scope, self.initial.id)
        if not state.active_turn:
            state = self.save(
                state,
                begin_turn(state, "读取", request_key="turn-" + str(state.revision)),
            )
        step_id = "step-" + str(state.revision)
        state = self.save(state, start_step(state, step_id=step_id))
        call = ToolCall(id="call", name=name, arguments=encoded(arguments))
        state = self.save(
            state,
            accept_response(
                state,
                step_id,
                ModelResponse(
                    model="fixture",
                    message=AssistantOutput(tool_calls=(call,)),
                    finish_reason="tool_calls",
                ),
            ),
        )
        return state, step_id

    def execute(self, name, arguments, *, tools=None):
        state, step = self.pending(name, arguments)
        result = (tools or self.tools).execute(self.scope, state.id, step, "call")
        self.save(state, supply_tool_results(state, step, [result]))
        return result, json.loads(result.content)

    def count(self, model):
        with self.factory() as db:
            return db.scalar(select(func.count()).select_from(model))

    def test_reference_is_pointer_not_read_and_unsaved_selection_only_retains_excerpt(
        self,
    ):
        ref = self.reader.create_reference(
            self.scope, self.initial.id, {"kind": "file", "path": "src/main.py"}
        )
        self.assertIsNone(ref["material_id"])
        self.assertEqual(self.workspace.calls, 0)
        self.assertEqual(self.count(AgentReadObservation), 0)
        buffer = self.reader.create_reference(
            self.scope,
            self.initial.id,
            {
                "kind": "selection",
                "path": "src/main.py",
                "base_version": 1,
                "content": "unselected-secret\nselected😀\nother-secret",
                "selection": {"kind": "lines", "start_line": 2, "end_line": 2},
            },
        )
        self.assertNotIn("content", buffer["reference"])
        snapshot = self.materials.material(self.scope, buffer["material_id"])
        self.assertEqual(snapshot.text, "selected😀\n")
        self.assertNotIn("secret", encoded(snapshot.metadata))
        self.assertEqual(self.count(AgentReadObservation), 0)
        _, body = self.execute(
            "read_reference", {"reference_id": buffer["reference_id"]}
        )
        self.assertEqual(body["text"], "selected😀\n")
        self.assertEqual(self.workspace.calls, 0)
        self.assertEqual(
            self.conversations.load(self.scope, self.initial.id).revision, 4
        )

    def test_versioned_read_is_cached_then_new_call_and_old_material_are_independent(
        self,
    ):
        state, step = self.pending("read_file", {"path": "src/main.py", "max_chars": 5})
        first = self.tools.execute(self.scope, state.id, step, "call")
        original = json.loads(first.content)
        self.workspace.version = 2
        self.workspace.contents["src/main.py"] = "new body"
        self.assertEqual(self.tools.execute(self.scope, state.id, step, "call"), first)
        self.assertEqual(self.workspace.calls, 1)
        self.assertEqual(self.count(AgentReadObservation), 1)
        self.save(state, supply_tool_results(state, step, [first]))
        _, mismatch = self.execute(
            "read_file", {"path": "src/main.py", "expected_version": 1}
        )
        self.assertEqual(mismatch["code"], "SOURCE_VERSION_CHANGED")
        _, changed = self.execute("read_file", {"path": "src/main.py"})
        self.assertEqual(changed["source_version"], "2")
        self.assertNotEqual(changed["material_id"], original["material_id"])
        _, old = self.execute(
            "read_material", {"material_id": original["material_id"], "offset": 5}
        )
        self.assertEqual(old["source_version"], "1")
        self.assertEqual(old["text"], "\nneedle 😀\nlast\n")
        self.assertEqual(self.count(AgentMaterial), 2)

    def test_queries_and_actual_selected_reads_have_distinct_records(self):
        _, tree = self.execute("list_directory", {"path": "."})
        self.assertIn("src", tree["text"])
        _, glob = self.execute("glob_files", {"pattern": "**/*.py", "limit": 1})
        self.assertTrue(json.loads(glob["text"])["has_more"])
        _, search = self.execute("search_content", {"query": "needle"})
        hit = json.loads(search["text"])["items"][0]
        self.assertEqual((hit["path"], hit["line"]), ("src/main.py", 2))
        _, read = self.execute(
            "read_file",
            {
                "path": "src/main.py",
                "selection": {"kind": "lines", "start_line": 2, "end_line": 2},
            },
        )
        self.assertEqual(read["text"], "needle 😀\n")
        kinds = [
            item["action_kind"]
            for item in self.materials.observations(self.scope, self.initial.id)[
                "items"
            ]
        ]
        self.assertCountEqual(kinds, ["query", "query", "query", "read"])
        self.assertEqual(self.count(AgentInputReference), 0)

    def test_ask_and_deny_happen_before_io_and_forged_calls_cannot_dispatch(self):
        state, step = self.pending("read_file", {"path": "src/main.py"})
        ask = ReadToolService(
            self.reader, policy=lambda *a: ReadDecision(decision="ask")
        )
        with self.assertRaises(ReadingApprovalRequired):
            ask.execute(self.scope, state.id, step, "call")
        self.assertEqual(self.workspace.calls, 0)
        self.assertEqual(self.count(AgentReadObservation), 0)
        with self.assertRaises(StaleStep):
            self.tools.execute(self.scope, state.id, step, "forged")
        deny = ReadToolService(
            self.reader,
            policy=lambda *a: ReadDecision(decision="deny", reason_code="READ_DENIED"),
        )
        result = deny.execute(self.scope, state.id, step, "call")
        self.assertTrue(result.is_error)
        self.assertEqual(self.workspace.calls, 0)
        self.assertEqual(self.count(AgentMaterial), 0)
        self.assertEqual(
            self.materials.observations(self.scope, state.id)["items"][0]["status"],
            "denied",
        )
        self.assertEqual(self.tools.execute(self.scope, state.id, step, "call"), result)

    def test_interruption_during_read_discards_late_record(self):
        state, step = self.pending("read_file", {"path": "src/main.py"})
        original = self.workspace.read

        def interrupt(scope, path):
            self.save(state, interrupt_turn(state, state.active_turn.id))
            return original(scope, path)

        with patch.object(self.workspace, "read", side_effect=interrupt):
            with self.assertRaises(StaleStep):
                self.tools.execute(self.scope, state.id, step, "call")
        self.assertEqual(self.count(AgentReadObservation), 0)
        self.assertEqual(self.count(AgentMaterial), 0)

    def test_concurrent_read_returns_one_durable_result_and_event(self):
        state, step = self.pending("read_file", {"path": "src/main.py"})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _: self.tools.execute(self.scope, state.id, step, "call"),
                    range(2),
                )
            )
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.count(AgentReadObservation), 1)
        self.assertEqual(self.count(AgentMaterial), 1)
        events = self.conversations.events(self.scope, state.id)["items"]
        self.assertEqual(sum(e["kind"] == "tool.read.completed" for e in events), 1)

    def test_read_and_event_roll_back_together(self):
        state, step = self.pending("read_file", {"path": "src/main.py"})
        with patch.object(
            self.materials, "_event", side_effect=RuntimeError("fixture")
        ):
            with self.assertRaises(RuntimeError):
                self.tools.execute(self.scope, state.id, step, "call")
        self.assertEqual(self.count(AgentMaterial), 0)
        self.assertEqual(self.count(AgentReadObservation), 0)
        self.assertEqual(self.conversations.load(self.scope, state.id), state)

    def test_cross_conversation_paging_is_versioned_and_omits_private_configuration(
        self,
    ):
        other = create_conversation(self.scope)
        self.conversations.create(other)
        ready = self.save(other, begin_turn(other, "历史问题", request_key="history"))
        pending = self.save(ready, start_step(ready, step_id="history-step"))
        done = self.save(
            pending,
            accept_response(
                pending,
                "history-step",
                ModelResponse(
                    model="fixture",
                    finish_reason="stop",
                    message=AssistantOutput(
                        content="历史回答",
                        continuation_json='{"reasoning_details":[{"data":"private-opaque"}]}',
                    ),
                ),
            ),
        )
        _, history = self.execute(
            "read_conversation",
            {
                "conversation_id": other.id,
                "message_limit": 1,
                "expected_revision": done.revision,
            },
        )
        self.assertIn("历史问题", history["text"])
        self.assertNotIn("private-opaque", history["text"])
        self.assertTrue(history["source_metadata"]["has_more"])
        self.save(done, begin_turn(done, "新问题", request_key="next"))
        _, stale = self.execute(
            "read_conversation",
            {
                "conversation_id": other.id,
                "expected_revision": done.revision,
                "first_message": 1,
            },
        )
        self.assertEqual(stale["code"], "SOURCE_VERSION_CHANGED")
        _, preserved = self.execute(
            "read_material", {"material_id": history["material_id"]}
        )
        self.assertEqual(preserved["text"], history["text"])
        _, summary = self.execute(
            "read_conversation", {"conversation_id": other.id, "view": "summary"}
        )
        self.assertIsNone(json.loads(summary["text"])["summary"])
        with self.assertRaises(ConversationNotFound):
            self.materials.observations("other-exam", self.initial.id)
        _, unavailable = self.execute(
            "read_conversation", {"conversation_id": "missing-other-student"}
        )
        self.assertEqual(unavailable["code"], "SOURCE_UNAVAILABLE")

    def test_registered_source_is_server_scoped_and_versions_checked(self):
        provider = Mock()
        provider.resolve.side_effect = lambda scope, source: (
            SourceSnapshot("dependency", source, "v2", "dependency text")
            if scope == self.scope
            else (_ for _ in ()).throw(ReadingError("SOURCE_UNAVAILABLE"))
        )
        tools = ReadToolService(SourceReader(self.materials, text_sources=provider))
        names = {s["function"]["name"] for s in tools.registry.specifications()}
        self.assertNotIn("read_file", names)
        _, body = self.execute(
            "read_source",
            {"source_id": "dependency:python", "expected_version": "v2"},
            tools=tools,
        )
        self.assertEqual(body["text"], "dependency text")
        provider.resolve.assert_called_with(self.scope, "dependency:python")
        _, stale = self.execute(
            "read_source",
            {"source_id": "dependency:python", "expected_version": "v1"},
            tools=tools,
        )
        self.assertEqual(stale["code"], "SOURCE_VERSION_CHANGED")

    def test_real_workspace_adapter_uses_database_revisions_and_sandbox_port(self):
        with self.factory.begin() as db:
            exam = db.get(ExamSession, self.scope)
            exam.status = "in_progress"
            exam.sandbox_id = "fake-sandbox"
            exam.started_at = exam.admitted_at
            exam.deadline_at = exam.admitted_at + 3600
        sandbox = Mock()
        sandbox.read_file.side_effect = lambda view, path: self.workspace.contents[path]
        workspace = WorkspaceReader(SessionLifecycle(self.factory), sandbox)
        tools = ReadToolService(SourceReader(self.materials, workspace=workspace))
        _, first = self.execute("read_file", {"path": "src/main.py"}, tools=tools)
        self.workspace.contents["src/main.py"] = "terminal changed it"
        _, second = self.execute("read_file", {"path": "src/main.py"}, tools=tools)
        self.assertEqual(
            (first["source_version"], second["source_version"]), ("1", "2")
        )
        self.assertEqual(sandbox.read_file.call_count, 2)
        with self.factory() as db:
            self.assertEqual(
                db.get(WorkspaceFileRevision, (self.scope, "src/main.py")).version, 2
            )

    def test_http_reference_material_and_read_apis_cannot_forge_agent_results(self):
        app = create_app(
            {}, entry_store=self.entry, agent_enabled=True, agent_reading=self.reader
        )
        prefix = f"/api/v1/sessions/{self.scope}/agent/conversations/{self.initial.id}"
        with TestClient(
            app,
            headers={"X-VibeEval-Request": "1"},
            cookies={"vibeeval_candidate": self.browser},
        ) as client:
            response = client.post(
                prefix + "/references",
                json={"kind": "selection", "path": "src/main.py", "content": "inline"},
            )
            self.assertEqual(response.status_code, 201, response.text)
            ref = response.json()
            self.assertEqual(client.get(prefix + "/references").json()["items"], [ref])
            self.assertEqual(
                client.get(prefix + "/references/" + ref["reference_id"]).json(), ref
            )
            response = client.get(prefix + "/materials/" + ref["material_id"])
            self.assertEqual(response.json()["text"], "inline")
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(client.get(prefix + "/reads").json()["items"], [])
            self.assertEqual(
                client.post(
                    prefix + "/reads",
                    json={"tool_call_id": "fake", "content": "success"},
                ).status_code,
                405,
            )
            self.assertEqual(
                client.post(
                    prefix + "/references", json={"kind": "file", "path": "../private"}
                ).status_code,
                422,
            )
            self.assertEqual(client.get(prefix + "/materials/missing").status_code, 404)
            self.assertEqual(
                client.get(
                    prefix + "/materials/" + ref["material_id"] + "?offset=999"
                ).status_code,
                422,
            )
            self.assertEqual(client.get(prefix + "/reads?limit=101").status_code, 422)
        self.assertEqual(self.count(AgentReadObservation), 0)
        with TestClient(app, headers={"X-VibeEval-Request": "1"}) as client:
            self.assertEqual(client.get(prefix + "/references").status_code, 404)
        with TestClient(app, cookies={"vibeeval_candidate": self.browser}) as client:
            self.assertEqual(client.get(prefix + "/reads").status_code, 403)

    def test_closed_exam_rejects_new_reads_but_retained_material_can_be_inspected(self):
        ref = self.reader.create_reference(
            self.scope,
            self.initial.id,
            {"kind": "selection", "path": "src/main.py", "content": "retained"},
        )
        state, step = self.pending("read_file", {"path": "src/main.py"})
        with self.factory.begin() as db:
            exam = db.get(ExamSession, self.scope)
            exam.status = "submitting"
        with self.assertRaises(ConversationClosed):
            self.tools.execute(self.scope, state.id, step, "call")
        self.assertEqual(self.workspace.calls, 0)
        self.assertEqual(
            self.materials.material_view(self.scope, state.id, ref["material_id"])[
                "text"
            ],
            "retained",
        )

    def test_database_rejects_a_cross_exam_material_reference(self):
        ref = self.reader.create_reference(
            self.scope,
            self.initial.id,
            {"kind": "selection", "path": "src/main.py", "content": "private"},
        )
        token, _ = self.entry.invite(
            "other", "其他考生", "read-exam", "阅读验收", 60, 24
        )
        other_scope = self.entry.admit(token, "s" * 43)["session_id"]
        other = create_conversation(other_scope)
        self.conversations.create(other)
        with self.assertRaises(ReadingError):
            self.materials.material(other_scope, ref["material_id"])
        with self.assertRaises(IntegrityError), self.factory.begin() as db:
            db.add(
                AgentInputReference(
                    reference_id="forged",
                    conversation_id=other.id,
                    exam_session_id=other_scope,
                    material_id=ref["material_id"],
                    reference_json={"kind": "material"},
                    created_at=1,
                )
            )

    def test_file_reference_keeps_selection_and_directory_reference_is_query(self):
        ref = self.reader.create_reference(
            self.scope,
            self.initial.id,
            {
                "kind": "file",
                "path": "src/main.py",
                "expected_version": 1,
                "selection": {"kind": "lines", "start_line": 2, "end_line": 2},
            },
        )
        _, body = self.execute("read_reference", {"reference_id": ref["reference_id"]})
        self.assertEqual(body["text"], "needle 😀\n")
        self.assertEqual(body["source_kind"], "file_selection")
        _, material = self.execute(
            "read_material", {"material_id": body["material_id"]}
        )
        self.assertEqual(material["text"], "needle 😀\n")
        self.workspace.version = 2
        _, stale = self.execute("read_reference", {"reference_id": ref["reference_id"]})
        self.assertEqual(stale["code"], "SOURCE_VERSION_CHANGED")
        directory = self.reader.create_reference(
            self.scope, self.initial.id, {"kind": "directory", "path": "src"}
        )
        _, listing = self.execute(
            "read_reference", {"reference_id": directory["reference_id"]}
        )
        self.assertEqual(listing["source_kind"], "query")
        ref = self.reader.create_reference(
            self.scope,
            self.initial.id,
            {"kind": "conversation", "conversation_id": self.initial.id},
        )
        _, history = self.execute(
            "read_reference", {"reference_id": ref["reference_id"]}
        )
        self.assertEqual(history["source_kind"], "conversation")

    def test_reading_a_recorded_tool_result_preserves_its_source_identity(self):
        state, step = self.pending("read_file", {"path": "src/main.py"})
        result = self.tools.execute(self.scope, state.id, step, "call")
        self.save(state, supply_tool_results(state, step, [result]))
        _, body = self.execute(
            "read_conversation",
            {
                "conversation_id": state.id,
                "view": "tool_result",
                "step_id": step,
                "tool_call_id": "call",
            },
        )
        self.assertEqual(json.loads(body["text"]), result.model_dump(mode="json"))
        self.assertEqual(body["source_metadata"]["step_id"], step)
        _, missing = self.execute(
            "read_conversation",
            {
                "conversation_id": state.id,
                "view": "tool_result",
                "step_id": "missing-step",
                "tool_call_id": "call",
            },
        )
        self.assertEqual(missing["code"], "SOURCE_UNAVAILABLE")

    def test_default_http_wiring_uses_shared_database_and_reports_unconfigured_sources(
        self,
    ):
        app = create_app({}, entry_store=self.entry, agent_enabled=True)
        prefix = f"/api/v1/sessions/{self.scope}/agent/conversations/{self.initial.id}"
        with TestClient(
            app,
            headers={"X-VibeEval-Request": "1"},
            cookies={"vibeeval_candidate": self.browser},
        ) as client:
            response = client.post(
                prefix + "/references", json={"kind": "file", "path": "src/main.py"}
            )
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(self.count(AgentInputReference), 1)
            response = client.post(
                prefix + "/references",
                json={"kind": "source", "source_id": "unconfigured"},
            )
            self.assertEqual(response.status_code, 503, response.text)
            self.assertEqual(self.count(AgentInputReference), 1)
            response = client.post(
                prefix + "/references", json={"kind": "directory", "path": "missing"}
            )
            self.assertEqual(response.status_code, 404)

    def test_native_model_roundtrip_reuses_existing_prompt_parser_and_tool_results(
        self,
    ):
        settings = LLMSettings(
            "https://fixture.test/v1", "fixture-only-key", "fixture/model"
        )
        requests = []

        def handle(request):
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "native-read",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path":"src/main.py"}',
                            },
                        }
                    ],
                }
                reason = "tool_calls"
            else:
                message, reason = {
                    "role": "assistant",
                    "content": "已读取并回答",
                }, "stop"
            return httpx.Response(
                200,
                json={
                    "model": "fixture/model",
                    "choices": [{"message": message, "finish_reason": reason}],
                },
            )

        async def roundtrip():
            state = self.save(
                self.initial,
                begin_turn(self.initial, "读取 main", request_key="native"),
            )
            state = self.save(state, start_step(state, step_id="native1"))
            client = LLMClient(settings, transport=httpx.MockTransport(handle))
            prepared = prepare_model_request(
                state, tools=self.tools.registry, settings=settings
            )
            response = await call_model(
                prepared, tools=self.tools.registry, client=client
            )
            state = self.save(state, accept_response(state, "native1", response))
            result = self.tools.execute(self.scope, state.id, "native1", "native-read")
            self.assertEqual(
                self.conversations.load(self.scope, state.id).active_turn.status,
                "awaiting_tools",
            )
            state = self.save(state, supply_tool_results(state, "native1", [result]))
            state = self.save(state, start_step(state, step_id="native2"))
            prepared = prepare_model_request(
                state, tools=self.tools.registry, settings=settings
            )
            response = await call_model(
                prepared, tools=self.tools.registry, client=client
            )
            return self.save(state, accept_response(state, "native2", response))

        state = asyncio.run(roundtrip())
        self.assertEqual(state.turns[0].status, "completed")
        self.assertEqual(requests[0]["messages"][0]["content"], GLOBAL_AGENT_PROMPT)
        self.assertEqual(requests[0]["tools"], self.tools.registry.specifications())
        self.assertEqual(requests[1]["messages"][-1]["tool_call_id"], "native-read")
        self.assertIn("needle", requests[1]["messages"][-1]["content"])
        self.assertEqual(self.count(AgentReadObservation), 1)

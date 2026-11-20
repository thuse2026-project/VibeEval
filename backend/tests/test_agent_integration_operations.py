"""Real PostgreSQL command ledger; replace only Docker I/O and model transport."""

import asyncio
import base64
import json
import time
import unittest
from unittest.mock import patch

import test_agent_integration_runtime as runtime_tests
from fastapi.testclient import TestClient

from app.agent import create_conversation
from app.agent_integration.reading_types import ReadingError
from app.agent_integration.runtime import RuntimeCoordinator
from app.agent_integration.runtime_types import RunInput, RunPolicy
from app.agent_integration.sources import SourceReader
from app.agent_integration.store import ConversationNotFound
from app.db.lifecycle import SessionBusy, SessionLifecycle
from app.db.models import ExamSession
from app.main import create_app
from app.sandbox.runtime import SandboxOperationFailed
from app.workspace.service import WorkspaceReader


class FileSandbox:
    def __init__(self, files):
        self.files, self.saves = files, 0

    def read_files(self, view):
        return dict(self.files)

    def read_file(self, view, path):
        return self.files[path]

    def save_file(self, view, path, expected_digest, content):
        self.saves += 1
        self.files[path] = content
        return {"saved": True}


class ExecutionDouble:
    def __init__(self):
        self.jobs, self.starts, self.prepares, self.cancels = {}, 0, 0, 0
        self.instant, self.lose_start, self.before_start_timeout = True, False, False
        self.exit_code = 0
        self.output = b"command output\n"

    def prepare_execution(self, view, config):
        key = config["operation_id"]
        if key not in self.jobs:
            self.prepares += 1
            self.jobs[key] = {
                "config": dict(config),
                "status": "created",
                "time": time.time(),
                "output": b"",
            }
        return {"handle": key}

    def start_execution(self, view, config, handle):
        self.starts += 1
        job = self.jobs[handle]
        if job["status"] != "created":
            raise AssertionError("a command was started twice")
        if self.before_start_timeout:
            raise TimeoutError("start acceptance unknown")
        job["status"], job["output"] = "running", b"partial\n"
        if self.instant:
            self.complete(handle, output=self.output)
        if self.lose_start:
            raise TimeoutError("daemon accepted start; RPC response lost")

    def complete(self, handle, *, status=None, output=None):
        job = self.jobs[handle]
        job["status"] = status or ("succeeded" if self.exit_code == 0 else "failed")
        job["output"] = output if output is not None else job["output"] + b"done\n"
        job["finished"] = time.time()

    def inspect_execution(self, view, config, handle=None):
        handle = handle or config["operation_id"]
        if handle not in self.jobs:
            return {
                "status": "unknown",
                "processes_stopped": bool(handle),
                "reason_code": "EXECUTION_NOT_FOUND",
            }
        job = self.jobs[handle]
        if job["status"] == "created":
            return {"status": "running", "processes_stopped": False}
        stopped = job["status"] != "running"
        return {
            "version": 1,
            "operation_id": config["operation_id"],
            "request_sha256": config["request_sha256"],
            "status": job["status"],
            "processes_stopped": stopped,
            "started_at": job["time"],
            "command_started_at": job["time"],
            "finished_at": job.get("finished"),
            "exit_code": self.exit_code if stopped else None,
            "stdout_b64": base64.b64encode(job["output"]).decode(),
            "stderr_b64": "",
            "stdout_bytes": len(job["output"]),
            "stderr_bytes": 0,
            "output_complete": stopped,
        }

    def cancel_execution(self, view, config, handle=None):
        self.cancels += 1
        handle = handle or config["operation_id"]
        if handle not in self.jobs:
            return {"status": "unknown", "processes_stopped": True}
        if self.jobs[handle]["status"] == "created":
            return {
                "status": "cancelled",
                "processes_stopped": True,
                "reason_code": "CANCELLED_BEFORE_START",
            }
        if self.jobs[handle]["status"] == "running":
            self.complete(handle, status="cancelled")
        return self.inspect_execution(view, config, handle)


COMMAND = runtime_tests.completion(
    calls=(("run_command", {"argv": ["python", "-c", "print('ok')"]}),)
)


@unittest.skipUnless(
    runtime_tests.os.environ.get("VIBEEVAL_RUN_DB_TESTS") == "1",
    "Requires isolated PostgreSQL",
)
class AgentIntegrationOperationTests(unittest.TestCase):
    setUpClass = classmethod(
        runtime_tests.AgentIntegrationRuntimeTests.setUpClass.__func__
    )
    drop_test_database = classmethod(
        runtime_tests.AgentIntegrationRuntimeTests.drop_test_database.__func__
    )

    def setUp(self):
        runtime_tests.AgentIntegrationRuntimeTests.setUp(self)
        with self.factory.begin() as db:
            row = db.get(ExamSession, self.scope)
            row.status, row.sandbox_id = "in_progress", "bound-command-sandbox"
            row.sandbox_resource_key = "resource-" + self.scope
            row.sandbox_image_ref, row.sandbox_backend_ref = (
                "fixture-image",
                "fixture-runsc",
            )
            row.task_version_id = "fixture-task-v1"
            row.started_at, row.deadline_at = row.admitted_at, row.admitted_at + 3600
        self.file_sandbox = FileSandbox(self.workspace.contents)
        self.workspace_service = WorkspaceReader(
            SessionLifecycle(self.factory), self.file_sandbox
        )
        self.reader = SourceReader(self.materials, workspace=self.workspace_service)
        self.backend = ExecutionDouble()

    def coordinator(self, responses=None, *, mode="sandbox_auto", decision="allow"):
        service = runtime_tests.AgentIntegrationRuntimeTests.coordinator(
            self,
            responses or [COMMAND, runtime_tests.completion()],
            policy=RunPolicy(mode=mode, action_decision=decision),
        )
        service.operations.backend = self.backend
        return service

    def submit(self, service, *, mode="sandbox_auto"):
        return service.submit(
            self.scope,
            self.initial.id,
            RunInput(request_key="command-request", text="运行检查", mode=mode),
        )

    def advance(self, service, run):
        return asyncio.run(service.advance(self.scope, self.initial.id, run["run_id"]))

    def operation(self, service):
        return service.operations.list(self.scope, self.initial.id)["items"][0]

    def approve(self, service, run, decision="approve"):
        item = service.store.interactions(self.scope, self.initial.id, run["run_id"])[
            "items"
        ][0]
        return service.store.resolve(
            self.scope,
            self.initial.id,
            run["run_id"],
            item["interaction_id"],
            kind="approval",
            answer={"decision": decision},
        )

    def test_auto_command_has_one_persistent_execution_and_returns_material_receipt(
        self,
    ):
        service = self.coordinator()
        run = self.submit(service)
        done = self.advance(service, run)
        self.assertEqual(done["status"], "completed")
        self.assertEqual((self.backend.prepares, self.backend.starts), (1, 1))
        op = self.operation(service)
        self.assertEqual(op["status"], "succeeded")
        self.assertEqual(op["exit_code"], 0)
        content = json.loads(self.requests[-1]["messages"][-1]["content"])
        self.assertEqual(content["execution_id"], op["operation_id"])
        self.assertEqual(content["stdout"], "command output\n")
        self.assertTrue(content["stdout_material_id"])
        self.assertEqual(content["starting_versions"]["src/main.py"], 1)
        parts = service.preview(self.scope, self.initial.id)["materials"]
        self.assertEqual(
            {part["source_kind"] for part in parts},
            {"execution_stdout", "execution_stderr"},
        )
        self.assertEqual(
            service.operations.read_output(
                self.scope, self.initial.id, op["operation_id"], "stdout"
            )["text"],
            "command output\n",
        )
        self.advance(service, run)
        self.assertEqual(self.backend.starts, 1)

    def test_confirm_actions_waits_before_creation_and_binds_versions(self):
        service = self.coordinator(mode="confirm_actions")
        run = self.submit(service, mode="confirm_actions")
        self.assertEqual(self.advance(service, run)["status"], "waiting_approval")
        self.assertEqual(self.backend.prepares, 0)
        op = self.operation(service)
        self.assertEqual(op["status"], "waiting_approval")
        self.assertEqual(op["expected_versions"]["src/main.py"], 1)
        self.approve(service, run)
        self.assertEqual(self.advance(service, run)["status"], "completed")
        self.assertEqual(self.backend.starts, 1)

    def test_reject_never_creates_command_container(self):
        service = self.coordinator(mode="confirm_actions")
        run = self.submit(service, mode="confirm_actions")
        self.advance(service, run)
        self.approve(service, run, "reject")
        self.assertEqual(self.advance(service, run)["status"], "completed")
        self.assertEqual(self.backend.prepares, 0)
        self.assertEqual(self.operation(service)["status"], "denied")

    def test_file_change_invalidates_approval_without_dispatch(self):
        service = self.coordinator(mode="confirm_actions")
        run = self.submit(service, mode="confirm_actions")
        self.advance(service, run)
        self.approve(service, run)
        self.file_sandbox.files["src/main.py"] = "changed after approval"
        self.assertEqual(self.advance(service, run)["status"], "completed")
        self.assertEqual(self.backend.starts, 0)
        self.assertEqual(
            self.operation(service)["reason_code"], "SOURCE_VERSION_CHANGED"
        )

    def test_running_operation_resumes_after_new_coordinator_without_start_replay(self):
        self.backend.instant = False
        service = self.coordinator()
        run = self.submit(service)
        self.assertEqual(self.advance(service, run)["status"], "waiting_operation")
        op = self.operation(service)
        self.assertIn(
            "partial",
            service.operations.read_output(
                self.scope, self.initial.id, op["operation_id"], "stdout"
            )["text"],
        )
        restarted = RuntimeCoordinator(
            self.reader,
            settings=runtime_tests.SETTINGS,
            client=service.client,
            policy=service.policy,
            execution_backend=self.backend,
        )
        self.backend.complete(op["operation_id"])
        self.assertEqual(self.advance(restarted, run)["status"], "completed")
        self.assertEqual(self.backend.starts, 1)
        self.assertIn("done", self.requests[-1]["messages"][-1]["content"])

    def test_nonzero_exit_is_a_real_program_receipt(self):
        self.backend.exit_code = 7
        service = self.coordinator()
        run = self.submit(service)
        self.assertEqual(self.advance(service, run)["status"], "completed")
        body = json.loads(self.requests[-1]["messages"][-1]["content"])
        self.assertEqual((body["status"], body["exit_code"]), ("failed", 7))
        state = self.conversations.load(self.scope, self.initial.id)
        self.assertFalse(state.turns[0].steps[0].tool_results[0].is_error)

    def test_lost_start_rpc_is_inspected_and_not_restarted(self):
        self.backend.lose_start = True
        service = self.coordinator()
        run = self.submit(service)
        self.assertEqual(self.advance(service, run)["status"], "completed")
        self.assertEqual(self.backend.starts, 1)

    def test_unconfirmed_start_is_never_retried_even_when_helper_still_created(self):
        self.backend.before_start_timeout = True
        service = self.coordinator()
        run = self.submit(service)
        self.assertEqual(self.advance(service, run)["status"], "waiting_operation")
        self.assertEqual(self.advance(service, run)["status"], "waiting_operation")
        self.assertEqual(self.backend.starts, 1)
        op = self.operation(service)
        self.assertEqual(
            service.operations.cancel(self.scope, self.initial.id, op["operation_id"])[
                "status"
            ],
            "cancelled",
        )
        self.assertEqual(self.advance(service, run)["status"], "completed")
        self.assertEqual(self.backend.starts, 1)

    def test_cancel_and_interrupt_stop_the_bound_execution(self):
        self.backend.instant = False
        service = self.coordinator()
        run = self.submit(service)
        self.advance(service, run)
        self.assertEqual(
            service.interrupt(self.scope, self.initial.id, run["run_id"])["status"],
            "interrupted",
        )
        self.assertEqual(self.backend.cancels, 1)
        self.assertEqual(self.operation(service)["status"], "cancelled")
        self.advance(service, run)
        self.assertEqual(len(self.requests), 1)

    def test_timed_out_receipt_is_returned_before_model_continues(self):
        self.backend.instant = False
        service = self.coordinator()
        run = self.submit(service)
        self.advance(service, run)
        op = self.operation(service)
        self.backend.complete(op["operation_id"], status="timed_out")
        self.assertEqual(self.advance(service, run)["status"], "completed")
        self.assertEqual(
            json.loads(self.requests[-1]["messages"][-1]["content"])["status"],
            "timed_out",
        )

    def test_workspace_holder_blocks_manual_save_and_another_conversation_command(self):
        self.backend.instant = False
        service = self.coordinator()
        run = self.submit(service)
        self.advance(service, run)
        with self.assertRaises(SessionBusy):
            self.workspace_service.save(self.scope, "src/booking.py", 1, "new")
        other = create_conversation(self.scope)
        self.conversations.create(other)
        other_service = self.coordinator()
        other_run = other_service.submit(
            self.scope,
            other.id,
            RunInput(request_key="second-command", text="run", mode="sandbox_auto"),
        )
        done = asyncio.run(
            other_service.advance(self.scope, other.id, other_run["run_id"])
        )
        self.assertEqual(done["status"], "completed")
        self.assertEqual(self.backend.starts, 1)
        service.operations.cancel(
            self.scope, self.initial.id, self.operation(service)["operation_id"]
        )
        self.workspace_service.save(self.scope, "src/booking.py", 1, "new")
        self.assertEqual(self.file_sandbox.saves, 1)

    def test_command_output_is_queryable_by_native_read_and_old_material(self):
        service = self.coordinator()
        run = self.submit(service)
        self.advance(service, run)
        op = self.operation(service)
        read = runtime_tests.completion(
            calls=(("read_execution_output", {"execution_id": op["operation_id"]}),)
        )
        second = self.coordinator([read, runtime_tests.completion()])
        next_run = second.submit(
            self.scope,
            self.initial.id,
            RunInput(request_key="read-output", text="查看执行输出"),
        )
        self.assertEqual(self.advance(second, next_run)["status"], "completed")
        content = json.loads(self.requests[-1]["messages"][-1]["content"])
        self.assertEqual(content["source_kind"], "execution_stdout")
        self.assertEqual(content["text"], "command output\n")

    def test_http_operations_are_owned_and_never_expose_daemon_handles(self):
        self.backend.instant = False
        service = self.coordinator()
        run = self.submit(service)
        self.advance(service, run)
        op = self.operation(service)
        app = create_app(
            {}, entry_store=self.entry, agent_enabled=True, agent_runtime=service
        )
        prefix = f"/api/v1/sessions/{self.scope}/agent/conversations/{self.initial.id}/operations"
        with TestClient(
            app,
            headers={"X-VibeEval-Request": "1"},
            cookies={"vibeeval_candidate": self.browser},
        ) as client:
            response = client.get(prefix + "/" + op["operation_id"])
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("bound-command-sandbox", response.text)
            self.assertNotIn("handle", response.json())
            self.assertEqual(
                client.get(prefix).json()["items"][0]["operation_id"],
                op["operation_id"],
            )
            self.assertIn(
                "partial",
                client.get(prefix + "/" + op["operation_id"] + "/output").json()[
                    "text"
                ],
            )
            self.assertEqual(
                client.post(prefix + "/" + op["operation_id"] + "/cancel").status_code,
                200,
            )
            self.assertEqual(client.get(prefix + "/missing").status_code, 404)
        with TestClient(app, headers={"X-VibeEval-Request": "1"}) as client:
            self.assertEqual(client.get(prefix).status_code, 404)
        with self.assertRaises(ConversationNotFound):
            service.operations.observe(
                "other-scope", self.initial.id, op["operation_id"]
            )

    def test_output_version_is_explicit_and_stale_reads_do_not_mix_captures(self):
        self.backend.instant = False
        service = self.coordinator()
        run = self.submit(service)
        self.advance(service, run)
        before = self.operation(service)
        self.backend.complete(before["operation_id"])
        after = service.operations.observe(
            self.scope, self.initial.id, before["operation_id"]
        )
        self.assertGreater(after["revision"], before["revision"])
        with self.assertRaises(ReadingError):
            service.operations.read_output(
                self.scope,
                self.initial.id,
                before["operation_id"],
                "stdout",
                expected_revision=before["revision"],
            )
        events = self.conversations.events(self.scope, self.initial.id, limit=200)[
            "items"
        ]
        chunks = [e for e in events if e["kind"] == "operation.output"]
        self.assertEqual(
            [c["data"]["byte_offset"] for c in chunks], [0, len(b"partial\n")]
        )

    def test_server_deny_creates_no_execution_and_expected_version_conflict_is_not_run(
        self,
    ):
        service = self.coordinator(decision="deny")
        run = self.submit(service)
        self.assertEqual(self.advance(service, run)["status"], "completed")
        self.assertEqual(self.backend.starts, 0)
        self.assertEqual(self.operation(service)["status"], "denied")

    def test_cancel_before_approval_wakes_same_turn_without_running_command(self):
        service = self.coordinator(mode="confirm_actions")
        run = self.submit(service, mode="confirm_actions")
        self.advance(service, run)
        op = self.operation(service)
        self.assertEqual(
            service.operations.cancel(self.scope, self.initial.id, op["operation_id"])[
                "status"
            ],
            "cancelled",
        )
        self.assertEqual(self.advance(service, run)["status"], "completed")
        self.assertEqual(self.backend.starts, 0)

    def test_lost_create_reply_recovers_preparation_without_dispatching_twice(self):
        service = self.coordinator()
        run = self.submit(service)
        prepare = self.backend.prepare_execution

        def lost_reply(view, config):
            prepare(view, config)
            raise TimeoutError("prepared helper reply lost")

        with patch.object(self.backend, "prepare_execution", side_effect=lost_reply):
            self.assertEqual(self.advance(service, run)["status"], "waiting_operation")
        self.assertEqual(self.backend.starts, 0)
        self.assertEqual(self.advance(service, run)["status"], "completed")
        self.assertEqual((self.backend.prepares, self.backend.starts), (1, 1))

    def test_definite_preparation_failure_releases_workspace_and_returns_tool_error(
        self,
    ):
        service = self.coordinator()
        run = self.submit(service)
        with patch.object(
            self.backend,
            "prepare_execution",
            side_effect=SandboxOperationFailed("fixture failure"),
        ):
            self.assertEqual(self.advance(service, run)["status"], "completed")
        self.assertEqual(
            self.operation(service)["reason_code"], "EXECUTION_PREPARE_FAILED"
        )
        self.assertEqual(self.backend.starts, 0)
        self.workspace_service.save(
            self.scope, "src/booking.py", 1, "editable after failure"
        )

    def test_unreachable_execution_is_not_replayed_and_retains_holder_until_stop_proof(
        self,
    ):
        self.backend.instant = False
        service = self.coordinator()
        run = self.submit(service)
        with (
            patch.object(
                self.backend,
                "inspect_execution",
                side_effect=SandboxOperationFailed("unreachable"),
            ),
            patch.object(
                self.backend,
                "cancel_execution",
                side_effect=SandboxOperationFailed("unreachable"),
            ),
        ):
            self.assertEqual(self.advance(service, run)["status"], "unknown")
        op = self.operation(service)
        self.assertFalse(op["processes_stopped"])
        with self.assertRaises(SessionBusy):
            self.workspace_service.save(self.scope, "src/booking.py", 1, "blocked")
        service.operations.cancel(self.scope, self.initial.id, op["operation_id"])
        self.workspace_service.save(self.scope, "src/booking.py", 1, "safe after stop")
        self.assertEqual(self.advance(service, run)["status"], "unknown")
        self.assertEqual(self.backend.starts, 1)
        self.assertEqual(len(self.requests), 1)

    def test_expired_waiting_run_stops_existing_operation(self):
        self.backend.instant = False
        service = self.coordinator()
        run = self.submit(service)
        self.advance(service, run)
        with patch.object(service.store, "clock", return_value=time.time() + 1000):
            self.assertEqual(self.advance(service, run)["status"], "expired")
        self.assertEqual(self.backend.cancels, 1)
        self.assertTrue(self.operation(service)["processes_stopped"])

    def test_another_conversation_can_read_owned_execution_without_merging_histories(
        self,
    ):
        service = self.coordinator()
        run = self.submit(service)
        self.advance(service, run)
        op = self.operation(service)
        other = create_conversation(self.scope)
        self.conversations.create(other)
        second = self.coordinator(
            [
                runtime_tests.completion(
                    calls=(
                        ("read_execution_output", {"execution_id": op["operation_id"]}),
                    )
                ),
                runtime_tests.completion(),
            ]
        )
        next_run = second.submit(
            self.scope,
            other.id,
            RunInput(request_key="cross-output", text="查看另一会话的输出"),
        )
        done = asyncio.run(second.advance(self.scope, other.id, next_run["run_id"]))
        self.assertEqual(done["status"], "completed")
        self.assertIn("command output", self.requests[-1]["messages"][-1]["content"])
        original = self.conversations.load(self.scope, self.initial.id)
        self.assertEqual(len(original.turns), 1)

    def test_server_deny_overrides_confirmation_mode_without_asking(self):
        service = self.coordinator(mode="confirm_actions", decision="deny")
        run = self.submit(service, mode="confirm_actions")
        self.assertEqual(self.advance(service, run)["status"], "completed")
        self.assertEqual(self.operation(service)["status"], "denied")
        self.assertEqual(self.backend.prepares, 0)
        self.assertEqual(
            service.store.interactions(self.scope, self.initial.id, run["run_id"])[
                "items"
            ],
            [],
        )

    def test_interrupt_waiting_approval_cancels_operation_as_well_as_interaction(self):
        service = self.coordinator(mode="confirm_actions")
        run = self.submit(service, mode="confirm_actions")
        self.advance(service, run)
        service.interrupt(self.scope, self.initial.id, run["run_id"])
        self.assertEqual(self.operation(service)["status"], "cancelled")
        self.assertEqual(self.backend.starts, 0)

    def test_busy_preflight_becomes_explicit_tool_conflict_without_dispatch(self):
        service = self.coordinator()
        run = self.submit(service)
        with patch.object(
            self.workspace_service,
            "tree",
            side_effect=SessionBusy("workspace lock busy"),
        ):
            self.assertEqual(self.advance(service, run)["status"], "completed")
        self.assertEqual(self.operation(service)["reason_code"], "WORKSPACE_BUSY")
        self.assertEqual(self.backend.prepares, 0)

    def test_expired_approval_cannot_dispatch_command(self):
        service = self.coordinator(mode="confirm_actions")
        run = self.submit(service, mode="confirm_actions")
        self.advance(service, run)
        with patch.object(service.store, "clock", return_value=time.time() + 301):
            self.assertEqual(self.advance(service, run)["status"], "completed")
        self.assertEqual(self.operation(service)["status"], "denied")
        self.assertEqual(self.backend.prepares, 0)

    def test_save_between_preflight_and_holder_invalidates_even_aba_content(self):
        service = self.coordinator()
        run = self.submit(service)
        hold = service.operations.hold
        original = self.file_sandbox.files["src/booking.py"]

        def changed_between_checks(*args):
            self.workspace_service.save(
                self.scope, "src/booking.py", 1, "temporary edit"
            )
            self.workspace_service.save(self.scope, "src/booking.py", 2, original)
            return hold(*args)

        with patch.object(
            service.operations, "hold", side_effect=changed_between_checks
        ):
            self.assertEqual(self.advance(service, run)["status"], "completed")
        self.assertEqual(
            self.operation(service)["reason_code"], "SOURCE_VERSION_CHANGED"
        )
        self.assertEqual(self.backend.starts, 0)

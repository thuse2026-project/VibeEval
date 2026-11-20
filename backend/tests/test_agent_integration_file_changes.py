"""Real PostgreSQL/native tools; only Docker and provider transport are replaced."""

import base64
import copy
import json
import os
import time
import unittest
from unittest.mock import patch

import test_agent_integration_operations as operations_tests
import test_agent_integration_runtime as runtime_tests
from fastapi.testclient import TestClient

from app.agent_integration.file_changes import finish_change
from app.agent_integration.materials import digest
from app.agent_integration.store import ConversationNotFound
from app.db.models import ExamSession
from app.main import create_app

PATH = "src/booking.py"
AFTER = "# agent write\r\nvalue = '中文🙂'\r\n"


class EditBackend(operations_tests.ExecutionDouble):
    supports_file_edits = True

    def __init__(self, files):
        super().__init__()
        self.files, self.paused, self.resumes, self.writes = files, False, 0, 0
        self.lose_receipt = False

    def start_execution(self, view, config, handle):
        if config.get("kind") == "file_edit":
            self.paused = True
            self.jobs[handle]["pause_owned"] = True
        super().start_execution(view, config, handle)

    def complete(self, handle, *, status=None, output=None):
        job = self.jobs[handle]
        config = job["config"]
        if config.get("kind") != "file_edit":
            return super().complete(handle, status=status, output=output)
        status = status or "succeeded"
        if status == "succeeded":
            if any(
                digest(self.files[p]) != sha
                for p, sha in config["expected_digests"].items()
            ):
                status, job["reason"] = "conflict", "SOURCE_VERSION_CHANGED"
            else:
                edit = config["edit"]
                text = base64.b64decode(edit["content_b64"]).decode("utf-8")
                changed = self.files[edit["path"]] != text
                self.files[edit["path"]] = text
                self.writes += int(changed)
                job["change"] = {
                    key: edit[key] for key in ("path", "before_sha256", "after_sha256")
                }
                job["change"]["changed"] = changed
        elif status == "cancelled":
            job["reason"] = "CANCELLED_BEFORE_REPLACE"
        super().complete(handle, status=status, output=b"")

    def inspect_execution(self, view, config, handle=None):
        value = super().inspect_execution(view, config, handle)
        job = self.jobs.get(handle or config["operation_id"], {})
        if config.get("kind") == "file_edit":
            value.update(
                pause_owned=job.get("pause_owned", False),
                file_change=job.get("change"),
                reason_code=job.get("reason"),
            )
            if self.lose_receipt and value["processes_stopped"]:
                value.update(
                    status="unknown",
                    file_change=None,
                    reason_code="RECEIPT_UNAVAILABLE",
                )
        return value

    def cancel_execution(self, view, config, handle=None):
        owned = self.jobs.get(handle or config["operation_id"], {}).get(
            "pause_owned", False
        )
        return {**super().cancel_execution(view, config, handle), "pause_owned": owned}

    def read_file(self, view, path):
        return self.files[path]

    def resume_workspace(self, view):
        if view.status != "in_progress":
            raise AssertionError("a frozen exam was resumed")
        self.resumes += 1
        self.paused = False


@unittest.skipUnless(
    os.environ.get("VIBEEVAL_RUN_DB_TESTS") == "1", "Requires isolated PostgreSQL"
)
class AgentIntegrationFileChangeTests(unittest.TestCase):
    setUpClass = classmethod(
        operations_tests.AgentIntegrationOperationTests.setUpClass.__func__
    )
    drop_test_database = classmethod(
        operations_tests.AgentIntegrationOperationTests.drop_test_database.__func__
    )
    coordinator = operations_tests.AgentIntegrationOperationTests.coordinator
    submit = operations_tests.AgentIntegrationOperationTests.submit
    advance = operations_tests.AgentIntegrationOperationTests.advance
    operation = operations_tests.AgentIntegrationOperationTests.operation
    approve = operations_tests.AgentIntegrationOperationTests.approve

    def setUp(self):
        operations_tests.AgentIntegrationOperationTests.setUp(self)
        self.backend = EditBackend(self.file_sandbox.files)

    def start(self, *, name="write_file", args=None, mode="sandbox_auto"):
        args = args or {"path": PATH, "expected_version": 1, "content": AFTER}
        service = self.coordinator(
            [
                runtime_tests.completion(calls=((name, args),)),
                runtime_tests.completion("操作反馈已收到。"),
            ],
            mode=mode,
        )
        return service, self.submit(service, mode=mode)

    def result_body(self):
        body = json.loads(self.requests[-1]["messages"][-1]["content"])
        return json.loads(body["content"]) if body.get("status") == "error" else body

    def test_auto_write_records_actual_version_and_immutable_proposals(self):
        before = self.file_sandbox.files[PATH]
        service, run = self.start()
        self.assertEqual(self.advance(service, run)["status"], "completed")
        body = self.result_body()
        self.assertTrue(body["change"]["applied"])
        self.assertEqual(body["change"]["current_version"], 2)
        self.assertEqual(
            (self.backend.starts, self.backend.writes, self.backend.resumes), (1, 1, 1)
        )
        self.assertFalse(self.backend.paused)
        self.assertEqual(self.file_sandbox.files[PATH], AFTER)
        op = self.operation(service)
        self.assertEqual(op["kind"], "file_edit")
        self.assertEqual(
            service.operations.read_change(
                self.scope, self.initial.id, op["operation_id"], "before"
            )["text"],
            before,
        )
        self.assertEqual(
            service.operations.read_change(
                self.scope, self.initial.id, op["operation_id"], "after"
            )["text"],
            AFTER,
        )
        self.assertEqual(self.workspace_service.read(self.scope, PATH)["version"], 2)
        manifest = service.preview(self.scope, self.initial.id)["materials"]
        self.assertEqual(
            {item["window_mode"] for item in manifest}, {"locator", "full_material"}
        )
        self.advance(service, run)
        self.assertEqual(self.backend.starts, 1)

    def test_exact_patch_preserves_crlf_and_reports_diff(self):
        self.file_sandbox.files[PATH] = "alpha=1\r\n中文=2\r\n"
        service, run = self.start(
            name="apply_patch",
            args={
                "path": PATH,
                "expected_version": 1,
                "edits": [{"old_text": "alpha=1", "new_text": "alpha=3"}],
            },
        )
        self.assertEqual(self.advance(service, run)["status"], "completed")
        self.assertEqual(self.file_sandbox.files[PATH], "alpha=3\r\n中文=2\r\n")
        self.assertIn("+alpha=3\r\n", self.result_body()["change"]["diff_preview"])

    def test_noop_does_not_bump_version_or_rewrite_file(self):
        self.file_sandbox.files[PATH] = AFTER
        service, run = self.start()
        self.advance(service, run)
        self.assertFalse(self.result_body()["change"]["changed"])
        self.assertEqual(self.result_body()["change"]["current_version"], 1)
        self.assertEqual(self.backend.writes, 0)

    def test_confirmation_exposes_stable_diff_before_creating_helper(self):
        service, run = self.start(mode="confirm_actions")
        self.assertEqual(self.advance(service, run)["status"], "waiting_approval")
        op = self.operation(service)
        request = service.store.interactions(
            self.scope, self.initial.id, run["run_id"]
        )["items"][0]["request"]
        self.assertEqual(request["change"]["after_sha256"], digest(AFTER))
        self.assertEqual(self.backend.prepares, 0)
        self.assertFalse(self.backend.paused)
        diff = service.operations.read_change(
            self.scope, self.initial.id, op["operation_id"], "diff"
        )
        self.assertIn("+value = '中文🙂'", diff["text"])
        self.approve(service, run)
        self.assertEqual(self.advance(service, run)["status"], "completed")
        self.assertEqual(self.backend.writes, 1)

    def test_rejected_proposal_keeps_materials_without_writing(self):
        before = self.file_sandbox.files[PATH]
        service, run = self.start(mode="confirm_actions")
        self.advance(service, run)
        self.approve(service, run, "reject")
        self.advance(service, run)
        self.assertFalse(self.result_body()["change"]["applied"])
        self.assertEqual(self.file_sandbox.files[PATH], before)
        self.assertEqual(self.backend.prepares, 0)

    def test_approval_cannot_cover_a_newer_file_version(self):
        service, run = self.start(mode="confirm_actions")
        self.advance(service, run)
        self.approve(service, run)
        self.workspace_service.save(self.scope, PATH, 1, "human edit")
        self.advance(service, run)
        self.assertEqual(
            self.operation(service)["reason_code"], "SOURCE_VERSION_CHANGED"
        )
        self.assertEqual(self.file_sandbox.files[PATH], "human edit")
        self.assertEqual(self.backend.starts, 0)

    def test_ambiguous_patch_has_no_partial_effect_or_helper(self):
        self.file_sandbox.files[PATH] = "repeat repeat"
        service, run = self.start(
            name="apply_patch",
            args={
                "path": PATH,
                "expected_version": 1,
                "edits": [{"old_text": "repeat", "new_text": "replace"}],
            },
        )
        self.advance(service, run)
        self.assertEqual(
            self.operation(service)["reason_code"], "PATCH_CONTEXT_AMBIGUOUS"
        )
        self.assertEqual(self.file_sandbox.files[PATH], "repeat repeat")
        self.assertEqual(self.backend.prepares, 0)

    def test_lost_start_reply_does_not_repeat_edit(self):
        self.backend.lose_start = True
        service, run = self.start()
        self.assertEqual(self.advance(service, run)["status"], "completed")
        self.assertTrue(self.result_body()["change"]["applied"])
        self.assertEqual((self.backend.starts, self.backend.writes), (1, 1))

    def test_cancel_created_after_pause_disarms_start_and_restores_original(self):
        self.backend.before_start_timeout = True
        service, run = self.start()
        self.assertEqual(self.advance(service, run)["status"], "waiting_operation")
        self.assertTrue(self.backend.paused)
        op = self.operation(service)
        service.operations.cancel(self.scope, self.initial.id, op["operation_id"])
        self.assertFalse(self.backend.paused)
        self.assertEqual(self.backend.writes, 0)
        self.assertEqual(self.advance(service, run)["status"], "completed")

    def test_missing_receipt_does_not_infer_success_from_matching_file_bytes(self):
        self.backend.lose_receipt = True
        service, run = self.start()
        self.advance(service, run)
        body = self.result_body()
        self.assertEqual(body["status"], "unknown")
        self.assertIsNone(body["change"]["applied"])
        self.assertTrue(body["change"]["current_matches_proposal"])
        self.assertEqual(self.backend.writes, 1)

    def test_interrupt_running_edit_restores_only_its_owned_pause(self):
        self.backend.instant = False
        service, run = self.start()
        self.assertEqual(self.advance(service, run)["status"], "waiting_operation")
        service.interrupt(self.scope, self.initial.id, run["run_id"])
        self.assertFalse(self.backend.paused)
        self.assertEqual(self.backend.writes, 0)

    def test_submission_fence_prevents_original_resume(self):
        self.backend.instant = False
        service, run = self.start()
        self.advance(service, run)
        with self.factory.begin() as db:
            db.get(ExamSession, self.scope).status = "submitting"
        op = self.operation(service)
        service.operations.cancel(self.scope, self.initial.id, op["operation_id"])
        self.assertTrue(self.backend.paused)
        self.assertEqual(self.backend.resumes, 0)

    def test_database_rollback_after_effect_recovers_receipt_without_rewriting(self):
        service, run = self.start()

        def failed_commit(*args):
            finish_change(*args)
            raise RuntimeError("injected transaction failure after resume")

        with patch(
            "app.agent_integration.operations.finish_change", side_effect=failed_commit
        ):
            with self.assertRaises(RuntimeError):
                self.advance(service, run)
        self.assertEqual((self.backend.starts, self.backend.writes), (1, 1))
        self.file_sandbox.files[PATH] = "later human change"
        with patch.object(service.store, "clock", return_value=time.time() + 181):
            self.assertEqual(self.advance(service, run)["status"], "completed")
        self.assertTrue(self.result_body()["change"]["applied"])
        self.assertFalse(self.result_body()["change"]["current_matches_proposal"])
        self.assertEqual(self.file_sandbox.files[PATH], "later human change")
        self.assertEqual(self.backend.writes, 1)

    def test_invalid_trusted_receipt_cannot_claim_different_edit(self):
        service, run = self.start()
        inspect = self.backend.inspect_execution

        def altered(*args):
            value = copy.deepcopy(inspect(*args))
            value["file_change"]["after_sha256"] = "0" * 64
            return value

        with patch.object(self.backend, "inspect_execution", side_effect=altered):
            done = self.advance(service, run)
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["error_code"], "EXECUTION_RECEIPT_INVALID")
        self.assertEqual(self.backend.starts, 1)

    def test_http_change_preview_is_owned_and_paginated_without_applying(self):
        service, run = self.start(mode="confirm_actions")
        self.advance(service, run)
        op = self.operation(service)
        app = create_app(
            {}, entry_store=self.entry, agent_enabled=True, agent_runtime=service
        )
        url = f"/api/v1/sessions/{self.scope}/agent/conversations/{self.initial.id}/operations/{op['operation_id']}/change"
        with TestClient(
            app,
            headers={"X-VibeEval-Request": "1"},
            cookies={"vibeeval_candidate": self.browser},
        ) as client:
            response = client.get(url, params={"part": "after", "max_chars": 4})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["text"], AFTER[:4])
            self.assertTrue(response.json()["has_more"])
            self.assertEqual(
                client.get(url, params={"part": "invalid"}).status_code, 422
            )
        with self.assertRaises(ConversationNotFound):
            service.operations.read_change(
                "other-exam", self.initial.id, op["operation_id"], "diff"
            )
        self.assertEqual(self.backend.prepares, 0)

"""Meaningful flow races and failure recovery without any authored exam content."""

import unittest
from concurrent.futures import ThreadPoolExecutor

from exam_flow_support import MemoryFlowStore, Questions, Resources, definition

from app.exam_flow import ExamFlowService, FlowError
from app.exam_flow.types import FlowState, digest


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.store, self.questions, self.resources = (
            MemoryFlowStore(),
            Questions(),
            Resources(),
        )
        self.service = ExamFlowService(
            self.store, self.questions, self.resources, clock=lambda: self.now
        )
        self.service.attach("owned", definition())

    def view(self):
        return self.service.current("owned")

    def ready(self):
        v = self.view()
        return self.service.prepare("owned", v["state_version"], "prepare")

    def start(self, key="start"):
        v = self.view()
        return self.service.start(
            "owned", v["current_stage"]["id"], v["state_version"], key
        )

    def finish(self, key="finish"):
        v = self.view()
        return self.service.finish_phase(
            "owned", v["current_phase"]["id"], v["state_version"], key
        )

    def submit(self, key="submit", summary=None):
        v = self.view()
        return self.service.submit(
            "owned", v["current_stage"]["id"], v["state_version"], key, summary
        )

    def answering(self):
        self.ready()
        self.start()
        return self.finish()

    def assertCode(self, code, callback):
        with self.assertRaises(FlowError) as error:
            callback()
        self.assertEqual(error.exception.code, code)

    def test_preparation_is_untimed_and_definition_is_pinned(self):
        original = self.view()
        self.assertEqual(original["status"], "admitted")
        self.assertIsNone(original["current_phase"]["deadline_at"])
        self.assertEqual(self.service.attach("owned", definition()), original)
        changed = definition().model_copy(update={"version": "v2"})
        self.assertCode(
            "DEFINITION_PINNED", lambda: self.service.attach("owned", changed)
        )
        ready = self.ready()
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["capabilities"], [])
        self.now += 500
        self.assertIsNone(self.view()["current_phase"]["deadline_at"])

    def test_full_four_stage_flow_with_external_questions_and_artifacts(self):
        self.ready()
        first = self.start()
        explore = first["current_phase"]["id"]
        self.service.save_notes("owned", explore, 0, "personal notes")
        first = self.finish()
        answer = first["current_phase"]["id"]
        self.assertEqual(self.service.notes("owned", answer)["value"], "personal notes")
        self.assertCode(
            "CAPABILITY_DENIED",
            lambda: self.service.save_notes("owned", answer, 1, "changed"),
        )
        self.assertEqual(self.service.get_questions("owned", answer)[0]["id"], "item")
        saved = self.service.save_answer("owned", answer, "item", 0, "provided answer")
        self.assertEqual(saved["revision"], 1)
        receipt = self.submit()
        exported = self.service.export_submission("owned", receipt["stage_id"])
        self.assertEqual(digest(exported["content"]), receipt["content_sha256"])
        self.assertEqual(exported["content"]["notes"]["value"], "personal notes")
        self.assertEqual(self.view()["transition_deadline_at"], self.now + 60)
        self.assertCode(
            "PHASE_CLOSED", lambda: self.service.get_questions("owned", answer)
        )
        self.start("start2")
        phase = self.view()["current_phase"]["id"]
        self.service.save_answer("owned", phase, "item", 0, {"reports": []})
        self.submit("submit2")
        self.start("start3")
        stage3 = self.view()["current_stage"]
        self.service.save_summary(
            "owned", stage3["id"], 0, "implementation and verification"
        )
        receipt3 = self.submit("submit3")
        self.start("start4")
        stage4 = self.view()["current_stage"]
        self.assertNotEqual(stage3["workspace_id"], stage4["workspace_id"])
        self.assertIsNone(self.store.load("owned").stage.summary.value)
        receipt4 = self.submit("submit4", "root cause and fix")
        final = self.view()
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["capabilities"], [])
        self.assertEqual(len({receipt["id"], receipt3["id"], receipt4["id"]}), 3)
        self.assertNotIn("provided-resource", str(final))
        self.assertNotIn("provided answer", str(final))
        events = self.service.events("owned")
        self.assertEqual(
            [e["sequence"] for e in events["items"]],
            list(range(1, events["head_sequence"] + 1)),
        )
        self.assertEqual(events["items"][-1]["kind"], "exam.completed")

    def test_old_draft_and_state_requests_cannot_overwrite_new_data(self):
        view = self.answering()
        phase = view["current_phase"]["id"]
        self.service.save_answer("owned", phase, "item", 0, "new")
        self.assertCode(
            "REVISION_CONFLICT",
            lambda: self.service.save_answer("owned", phase, "item", 0, "old"),
        )
        self.assertEqual(self.service.answers("owned", phase)["item"]["value"], "new")
        self.assertCode(
            "STATE_VERSION_CONFLICT",
            lambda: self.service.submit(
                "owned", view["current_stage"]["id"], view["state_version"], "stale"
            ),
        )
        self.assertEqual(self.store.load("owned").phase.status, "active")

    def test_early_exploration_finish_does_not_transfer_time_or_leak_questions(self):
        self.ready()
        initial = self.start()
        old = initial["current_phase"]["id"]
        self.assertCode(
            "QUESTIONS_NOT_AVAILABLE", lambda: self.service.get_questions("owned", old)
        )
        self.assertEqual(self.questions.calls, [])
        self.now += 2
        answering = self.finish()
        self.assertEqual(answering["current_phase"]["deadline_at"], self.now + 15)
        self.assertNotIn("use_agent", answering["capabilities"])
        self.assertCode(
            "PHASE_CLOSED", lambda: self.service.authorize("owned", old, "use_agent")
        )

    def test_offline_deadline_transition_and_automatic_next_stage_start(self):
        self.ready()
        initial = self.start()
        self.now = initial["current_phase"]["deadline_at"]
        self.service.run_due()
        answering = self.view()
        self.assertEqual(answering["current_phase"]["key"], "answer")
        self.now = answering["current_phase"]["deadline_at"]
        self.service.run_due()
        self.assertEqual(self.view()["current_stage"]["ordinal"], 2)
        self.assertEqual(
            self.store.load("owned").stages[0].receipt.close_reason, "expired"
        )
        self.now = self.view()["transition_deadline_at"]
        self.service.run_due()
        started = self.view()
        self.assertEqual(started["current_phase"]["status"], "active")
        self.now += 1
        self.assertEqual(
            self.view()["current_phase"]["deadline_at"],
            started["current_phase"]["deadline_at"],
        )

    def test_every_tool_authorization_checks_deadline_without_scheduler(self):
        self.ready()
        active = self.start()
        phase = active["current_phase"]
        context = self.service.authorize("owned", phase["id"], "use_agent")
        opened = next(c for c in self.resources.calls if c[0] == "open")
        self.assertEqual(context.permission_epoch, opened[1].permission_epoch)
        self.assertCode(
            "AUTHORIZATION_REVOKED",
            lambda: self.service.authorize(
                "owned", phase["id"], "use_agent", permission_epoch=0
            ),
        )
        self.now = phase["deadline_at"]
        self.assertCode(
            "PHASE_CLOSED",
            lambda: self.service.authorize("owned", phase["id"], "use_agent"),
        )

    def test_prepare_and_phase_activation_failure_do_not_start_clock(self):
        self.resources.fail["prepare"] = 1
        view = self.ready()
        self.assertEqual(view["error_code"], "RESOURCE_PREPARE_FAILED")
        self.assertIsNone(view["current_phase"]["deadline_at"])
        self.assertCode("STAGE_NOT_READY", lambda: self.start())
        self.now += 5
        self.service.run_due()
        self.resources.fail["open"] = 1
        pending = self.start()
        self.assertIsNone(pending["current_phase"]["deadline_at"])
        self.assertEqual(pending["capabilities"], [])
        self.now += 5
        self.service.run_due()
        self.assertEqual(self.view()["current_phase"]["started_at"], self.now)

    def test_close_failure_fences_old_agent_and_delays_answering(self):
        self.ready()
        old = self.start()["current_phase"]["id"]
        self.resources.fail["close"] = 1
        pending = self.finish()
        self.assertEqual(pending["current_phase"]["status"], "closing")
        self.assertEqual(pending["capabilities"], [])
        self.assertCode(
            "PHASE_CLOSED", lambda: self.service.authorize("owned", old, "use_agent")
        )
        self.now += 5
        self.service.run_due()
        self.assertEqual(self.view()["current_phase"]["started_at"], self.now)

    def test_export_failure_stays_frozen_and_recovers_after_restart(self):
        self.answering()
        self.resources.fail["seal"] = 1
        self.assertCode("SUBMISSION_IN_PROGRESS", self.submit)
        blocked = self.view()
        self.assertEqual(blocked["current_stage"]["status"], "sealing")
        self.assertIsNone(blocked["transition_deadline_at"])
        self.assertEqual(blocked["capabilities"], [])
        pending_id = self.store.load("owned").operation.id
        restored = FlowState.model_validate_json(
            self.store.load("owned").model_dump_json()
        )
        self.store.values["owned"] = restored
        self.service = ExamFlowService(
            self.store, self.questions, self.resources, clock=lambda: self.now
        )
        self.now += 5
        self.service.run_due()
        self.assertEqual(self.view()["current_stage"]["ordinal"], 2)
        self.assertEqual(len({c[2] for c in self.resources.calls if c[0] == "seal"}), 1)
        self.assertEqual(self.resources.calls[-2][2], pending_id)

    def test_idempotent_commands_and_submissions_reject_changed_payload(self):
        self.ready()
        active = self.start()
        again = self.service.start("owned", active["current_stage"]["id"], 0, "start")
        self.assertEqual(
            again["current_phase"]["deadline_at"],
            active["current_phase"]["deadline_at"],
        )
        self.finish()
        receipt = self.submit(summary="same")
        self.assertEqual(
            self.service.submit("owned", receipt["stage_id"], 0, "submit", "same"),
            receipt,
        )
        self.assertCode(
            "IDEMPOTENCY_CONFLICT",
            lambda: self.service.submit(
                "owned", receipt["stage_id"], 0, "submit", "changed"
            ),
        )

    def test_concurrent_submit_returns_one_receipt_and_one_committed_event(self):
        view = self.answering()
        stage, version = view["current_stage"]["id"], view["state_version"]

        def submit(index):
            try:
                return self.service.submit("owned", stage, version, f"parallel{index}")
            except FlowError as error:
                self.assertIn(
                    error.code,
                    (
                        "SUBMISSION_IN_PROGRESS",
                        "STATE_VERSION_CONFLICT",
                        "PHASE_CLOSED",
                    ),
                )
                return None

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(submit, range(16)))
        receipt = self.service.receipt("owned", stage)
        self.assertTrue(all(r is None or r == receipt for r in results))
        state = self.store.load("owned")
        self.assertEqual(sum(e["kind"] == "stage.submitted" for e in state.events), 1)
        self.assertEqual(len({c[2] for c in self.resources.calls if c[0] == "seal"}), 1)

    def test_wrong_workspace_artifact_cannot_complete_code_submission(self):
        self.answering()
        self.submit()
        self.start("start2")
        self.submit("submit2")
        self.start("start3")
        self.resources.wrong_workspace = True
        self.assertCode("SUBMISSION_IN_PROGRESS", lambda: self.submit("submit3"))
        self.assertEqual(self.view()["error_code"], "RESOURCE_SEAL_FAILED")
        self.assertEqual(self.view()["current_stage"]["ordinal"], 3)

    def test_invalid_answers_roll_back_and_future_phase_is_unreadable(self):
        active = self.answering()
        phase = active["current_phase"]["id"]
        self.assertCode(
            "ANSWER_INVALID",
            lambda: self.service.save_answer("owned", phase, "item", 0, None),
        )
        self.assertEqual(self.service.answers("owned", phase), {})
        future = self.store.load("owned").stages[1].phases[0].id
        self.assertCode(
            "PHASE_CLOSED", lambda: self.service.get_questions("owned", future)
        )

    def test_invalid_configuration_and_incomplete_adapters_are_rejected(self):
        with self.assertRaises(TypeError):
            ExamFlowService(self.store, self.questions, object())
        value = definition().model_dump(mode="json")
        value["stages"][0]["phases"][0]["duration_seconds"] = 0
        with self.assertRaises(ValueError):
            self.service.attach("other", value)

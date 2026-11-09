"""Opt-in real PostgreSQL transactions, competing workers and process recovery."""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from exam_flow_support import Questions, Resources, definition
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.config import get_database_url
from app.db.lifecycle import LifecycleConflict, SessionLifecycle
from app.db.models import ExamFlowAttempt, ExamSession, Invitation
from app.exam_flow import ExamFlowService, FlowError, PostgresFlowStore, PublicQuestion
from app.exam_flow.types import digest


@unittest.skipUnless(
    os.environ.get("VIBEEVAL_RUN_DB_TESTS") == "1", "Enable isolated PostgreSQL tests"
)
class FlowPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = get_database_url()
        cls.admin = create_engine(
            url.set(database="postgres"), isolation_level="AUTOCOMMIT"
        )
        cls.addClassCleanup(cls.admin.dispose)
        cls.name = "vibeeval_flow_test_" + uuid4().hex
        with cls.admin.connect() as conn:
            conn.exec_driver_sql(f'CREATE DATABASE "{cls.name}"')
        cls.addClassCleanup(cls.drop_database)
        cls.engine = create_engine(url.set(database=cls.name), hide_parameters=True)
        cls.addClassCleanup(cls.engine.dispose)
        cls.factory = sessionmaker(cls.engine, expire_on_commit=False)
        config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        config.attributes["database_url"] = url.set(database=cls.name)
        command.upgrade(config, "head")

    @classmethod
    def drop_database(cls):
        with cls.admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE "{cls.name}"')

    def setUp(self):
        self.now = 2000000000
        self.sid = "session_" + uuid4().hex
        with self.factory.begin() as db:
            token = uuid4().hex + uuid4().hex
            db.add(
                Invitation(
                    token_hash=token,
                    candidate_id=self.sid,
                    candidate_name="Candidate",
                    exam_id="external",
                    exam_title="External",
                    duration_minutes=60,
                    expires_at=self.now + 10000,
                )
            )
            db.flush()
            db.add(
                ExamSession(
                    session_id=self.sid,
                    invitation_hash=token,
                    browser_hash=uuid4().hex + uuid4().hex,
                    admitted_at=self.now,
                )
            )
        self.resources, self.questions = Resources(), Questions()
        self.flow = self.new_service()
        self.flow.attach(self.sid, definition())

    def new_service(self):
        return ExamFlowService(
            PostgresFlowStore(self.factory),
            self.questions,
            self.resources,
            clock=lambda: self.now,
        )

    def answering(self):
        v = self.flow.current(self.sid)
        v = self.flow.prepare(self.sid, v["state_version"], "prepare")
        v = self.flow.start(
            self.sid, v["current_stage"]["id"], v["state_version"], "start"
        )
        return self.flow.finish_phase(
            self.sid, v["current_phase"]["id"], v["state_version"], "finish"
        )

    def test_restart_restores_answers_deadline_and_frozen_definition(self):
        v = self.answering()
        self.flow.save_answer(
            self.sid, v["current_phase"]["id"], "item", 0, "persisted"
        )
        restarted = self.new_service()
        self.assertEqual(
            restarted.current(self.sid)["current_phase"]["deadline_at"],
            v["current_phase"]["deadline_at"],
        )
        self.assertEqual(
            restarted.answers(self.sid, v["current_phase"]["id"])["item"]["value"],
            "persisted",
        )
        self.assertEqual(
            restarted.attach(self.sid, definition())["attempt_id"], v["attempt_id"]
        )
        with self.assertRaises(FlowError):
            restarted.attach(
                self.sid, definition().model_copy(update={"version": "v2"})
            )

    def test_competing_database_writers_cannot_overwrite_new_draft(self):
        v = self.answering()

        def save(index):
            try:
                self.new_service().save_answer(
                    self.sid, v["current_phase"]["id"], "item", 0, str(index)
                )
                return "saved"
            except FlowError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(save, range(12)))
        self.assertEqual(results.count("saved"), 1)
        self.assertEqual(results.count("REVISION_CONFLICT"), 11)

    def test_competing_workers_commit_one_submission_and_resume_next_stage(self):
        v = self.answering()
        stage = v["current_stage"]["id"]

        def submit(index):
            try:
                return self.new_service().submit(
                    self.sid, stage, v["state_version"], "submit" + str(index)
                )
            except FlowError as error:
                self.assertIn(
                    error.code,
                    (
                        "STATE_VERSION_CONFLICT",
                        "PHASE_CLOSED",
                        "SUBMISSION_IN_PROGRESS",
                    ),
                )
                return None

        with ThreadPoolExecutor(max_workers=6) as executor:
            receipts = list(executor.map(submit, range(12)))
        final = self.flow.receipt(self.sid, stage)
        self.assertTrue(all(r is None or r == final for r in receipts))
        state = self.flow.store.load(self.sid)
        self.assertEqual(sum(e["kind"] == "stage.submitted" for e in state.events), 1)
        self.assertEqual(state.stage_index, 1)

    def test_resource_failure_is_durable_and_due_worker_retries_same_operation(self):
        v = self.answering()
        self.resources.fail["seal"] = 1
        with self.assertRaises(FlowError):
            self.flow.submit(
                self.sid, v["current_stage"]["id"], v["state_version"], "submit"
            )
        state = self.flow.store.load(self.sid)
        self.assertEqual(state.stage.status, "sealing")
        self.assertEqual(state.error_code, "RESOURCE_SEAL_FAILED")
        self.now += 5
        self.new_service().run_due()
        self.assertEqual(self.flow.current(self.sid)["current_stage"]["ordinal"], 2)
        self.assertEqual(len({c[2] for c in self.resources.calls if c[0] == "seal"}), 1)

    def test_legacy_lifecycle_cannot_operate_after_mode_attachment(self):
        with self.factory.begin() as db:
            self.assertEqual(db.get(ExamSession, self.sid).exam_mode, "multi_stage")
            self.assertIsNotNone(
                db.scalar(
                    select(ExamFlowAttempt).where(
                        ExamFlowAttempt.session_id == self.sid
                    )
                )
            )
        with self.assertRaises(LifecycleConflict):
            with SessionLifecycle(self.factory).lock_session(self.sid):
                self.fail("legacy operations must be fenced inside the admission lock")

    def test_frozen_question_snapshot_survives_provider_change_and_restart(self):
        active = self.answering()
        phase = active["current_phase"]["id"]
        original = self.flow.get_questions(self.sid, phase)
        self.questions.questions = lambda *_: [
            PublicQuestion(id="new", title="Changed", body="changed")
        ]
        restored = self.new_service()
        self.assertEqual(restored.get_questions(self.sid, phase), original)
        restored.save_answer(self.sid, phase, "item", 0, "answer")
        view = restored.current(self.sid)
        receipt = restored.submit(
            self.sid, view["current_stage"]["id"], view["state_version"], "submit"
        )
        content = restored.export_submission(self.sid, receipt["stage_id"])["content"]
        self.assertEqual(content["phases"][1]["questions"], original)

    def test_delayed_transition_is_durable_across_workers(self):
        view = self.answering()
        self.flow.submit(
            self.sid, view["current_stage"]["id"], view["state_version"], "submit"
        )
        scheduled = self.flow.current(self.sid)["transition_deadline_at"]
        self.now = scheduled + 5
        self.new_service().run_due()
        phase = self.flow.current(self.sid)["current_phase"]
        self.assertEqual(phase["started_at"], scheduled)
        self.assertEqual(phase["deadline_at"], scheduled + 20)

    def test_legacy_json_defaults_do_not_rewrite_submitted_material(self):
        view = self.answering()
        receipt = self.flow.submit(
            self.sid, view["current_stage"]["id"], view["state_version"], "submit"
        )
        with self.factory.begin() as db:
            row = db.get(ExamFlowAttempt, self.sid)
            value = deepcopy(row.state_json)
            for stage in value["stages"]:
                for phase in stage["phases"]:
                    phase.pop("questions", None)
                if stage["submission"] is not None:
                    for phase in stage["submission"]["phases"]:
                        phase.pop("questions", None)
                    stage["receipt"]["content_sha256"] = digest(stage["submission"])
            row.state_json = value
            original = value["stages"][0]["submission"]
        exported = self.new_service().export_submission(self.sid, receipt["stage_id"])
        self.assertEqual(exported["content"], original)
        self.assertEqual(exported["receipt"]["content_sha256"], digest(original))

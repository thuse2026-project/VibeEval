"""Opt-in acceptance tests create and remove their own isolated PostgreSQL database."""

import os
import unittest
import uuid
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.config import get_database_url
from app.db.models import Base, ExamSession, Invitation, Snapshot, Submission
from app.db.session import create_db_engine, session_scope


@unittest.skipUnless(
    os.environ.get("VIBEEVAL_RUN_DB_TESTS") == "1",
    "Set VIBEEVAL_RUN_DB_TESTS=1 to run isolated PostgreSQL tests",
)
class PostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        url = get_database_url()
        cls.admin = create_engine(
            url.set(database="postgres"),
            isolation_level="AUTOCOMMIT",
            hide_parameters=True,
        )
        cls.addClassCleanup(cls.admin.dispose)
        cls.database_name = "vibeeval_test_" + uuid.uuid4().hex
        with cls.admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{cls.database_name}"')
        cls.addClassCleanup(cls.drop_test_database)
        cls.engine = create_db_engine(url.set(database=cls.database_name))
        cls.addClassCleanup(cls.engine.dispose)
        cls.factory = sessionmaker(cls.engine, expire_on_commit=False)
        cls.migration = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        cls.migration.attributes["database_url"] = url.set(database=cls.database_name)
        command.upgrade(cls.migration, "head")

    @classmethod
    def drop_test_database(cls):
        with cls.admin.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{cls.database_name}"')

    def setUp(self):
        with self.engine.begin() as connection:
            connection.execute(Submission.__table__.delete())
            connection.execute(Snapshot.__table__.delete())
            connection.execute(ExamSession.__table__.delete())
            connection.execute(Invitation.__table__.delete())

    @staticmethod
    def invitation(**changes):
        values = dict(
            token_hash="1" * 64,
            candidate_id="candidate-001",
            candidate_name="测试考生",
            exam_id="exam-001",
            exam_title="测试场次",
            duration_minutes=60,
            expires_at=2200003600,
        )
        return Invitation(**(values | changes))

    @staticmethod
    def exam_session(**changes):
        values = dict(
            session_id="ses_" + uuid.uuid4().hex,
            invitation_hash="1" * 64,
            browser_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            admitted_at=2200000000,
        )
        return ExamSession(**(values | changes))

    def save_invitation(self, **changes):
        with session_scope(self.factory) as session:
            session.add(self.invitation(**changes))

    def test_migration_matches_models(self):
        self.assertEqual(
            set(inspect(self.engine).get_table_names()),
            {
                "invitations",
                "exam_sessions",
                "snapshots",
                "submissions",
                "alembic_version",
            },
        )
        with self.engine.connect() as connection:
            context = MigrationContext.configure(
                connection, opts={"compare_type": True}
            )
            self.assertEqual(compare_metadata(context, Base.metadata), [])

    def test_migration_can_roll_back_and_upgrade_again(self):
        command.downgrade(self.migration, "base")
        try:
            self.assertFalse(inspect(self.engine).has_table("invitations"))
            self.assertFalse(inspect(self.engine).has_table("exam_sessions"))
        finally:
            command.upgrade(self.migration, "head")

    def test_admission_does_not_start_exam(self):
        self.save_invitation()
        record = self.exam_session()
        with session_scope(self.factory) as session:
            session.add(record)
        with session_scope(self.factory) as session:
            restored = session.get(ExamSession, record.session_id)
            self.assertEqual(restored.status, "admitted")
            self.assertEqual(restored.admitted_at, 2200000000)
            self.assertIsNone(restored.started_at)
            self.assertIsNone(restored.deadline_at)
            self.assertIsNone(restored.submitted_at)
            self.assertEqual(restored.invitation.candidate_name, "测试考生")

    def test_full_timeline_is_persisted(self):
        self.save_invitation()
        record = self.exam_session(
            started_at=2200000010,
            deadline_at=2200003610,
            submitted_at=2200001000,
            status="submitted",
        )
        with session_scope(self.factory) as session:
            session.add(record)
        with session_scope(self.factory) as session:
            restored = session.get(ExamSession, record.session_id)
            self.assertEqual(
                (restored.started_at, restored.deadline_at, restored.submitted_at),
                (2200000010, 2200003610, 2200001000),
            )

    def test_candidate_can_have_only_one_invitation_per_exam(self):
        self.save_invitation()
        with self.assertRaises(IntegrityError):
            self.save_invitation(token_hash="2" * 64)

    def test_session_requires_an_existing_invitation(self):
        with self.assertRaises(IntegrityError), session_scope(self.factory) as session:
            session.add(self.exam_session())

    def test_invitation_cannot_create_two_sessions(self):
        self.save_invitation()
        with session_scope(self.factory) as session:
            session.add(self.exam_session())
        with self.assertRaises(IntegrityError), session_scope(self.factory) as session:
            session.add(self.exam_session())

    def test_browser_cannot_claim_two_sessions(self):
        self.save_invitation()
        self.save_invitation(token_hash="2" * 64, candidate_id="candidate-002")
        browser_hash = "a" * 64
        with session_scope(self.factory) as session:
            session.add(self.exam_session(browser_hash=browser_hash))
        with self.assertRaises(IntegrityError), session_scope(self.factory) as session:
            session.add(
                self.exam_session(invitation_hash="2" * 64, browser_hash=browser_hash)
            )

    def test_invalid_invitation_values_are_rejected(self):
        cases = (
            {"token_hash": "plaintext-token"},
            {"candidate_name": " "},
            {"duration_minutes": 0},
            {"duration_minutes": 1441},
            {"expires_at": -1},
        )
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(IntegrityError):
                self.save_invitation(**changes)

    def test_invalid_session_values_and_time_order_are_rejected(self):
        self.save_invitation()
        cases = (
            {"browser_hash": "plaintext-browser"},
            {"session_id": " "},
            {"status": " "},
            {"admitted_at": -1},
            {"started_at": 2200000010},
            {"deadline_at": 2200003610},
            {"started_at": 2199999999, "deadline_at": 2200003610},
            {"started_at": 2200000010, "deadline_at": 2200000010},
            {"submitted_at": 2200000100},
            {
                "started_at": 2200000010,
                "deadline_at": 2200003610,
                "submitted_at": 2200000009,
            },
        )
        for changes in cases:
            with (
                self.subTest(changes=changes),
                self.assertRaises(IntegrityError),
                session_scope(self.factory) as session,
            ):
                session.add(self.exam_session(**changes))

    def test_transaction_rolls_back_when_work_fails(self):
        with self.assertRaisesRegex(RuntimeError, "simulated failure"):
            with session_scope(self.factory) as session:
                session.add(self.invitation())
                session.flush()
                raise RuntimeError("simulated failure")
        with session_scope(self.factory) as session:
            self.assertEqual(
                session.scalar(select(func.count()).select_from(Invitation)), 0
            )
        self.save_invitation()
        with session_scope(self.factory) as session:
            self.assertEqual(
                session.scalar(select(func.count()).select_from(Invitation)), 1
            )


if __name__ == "__main__":
    unittest.main()

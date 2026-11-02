import os
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from app.db.config import get_database_url
from app.db.models import ExamSession, Invitation, Snapshot, Submission
from app.db.session import create_db_engine, session_scope
from app.submissions.service import (
    InvalidSubmissionState,
    SubmissionNotFound,
    SubmissionService,
)
from app.submissions.storage import (
    SnapshotFile,
    SnapshotStorageError,
    SnapshotStore,
    SnapshotValidationError,
)
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker


@unittest.skipUnless(
    os.environ.get("VIBEEVAL_RUN_DB_TESTS") == "1",
    "Set VIBEEVAL_RUN_DB_TESTS=1 for PostgreSQL tests",
)
class SubmissionPersistenceTests(unittest.TestCase):
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
        migration = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        migration.attributes["database_url"] = url.set(database=cls.database_name)
        command.upgrade(migration, "head")

    @classmethod
    def drop_test_database(cls):
        with cls.admin.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{cls.database_name}"')

    def setUp(self):
        with self.engine.begin() as connection:
            for model in (Submission, Snapshot, ExamSession, Invitation):
                connection.execute(model.__table__.delete())
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "snapshots"
        self.store = SnapshotStore(self.root)
        self.service = SubmissionService(self.store, self.factory, clock=lambda: 2000)
        self.seed()

    def seed(self, suffix="1"):
        with session_scope(self.factory) as session:
            session.add(
                Invitation(
                    token_hash=suffix * 64,
                    candidate_id="candidate-" + suffix,
                    candidate_name="冻结前姓名",
                    exam_id="exam-001",
                    exam_title="冻结前场次",
                    duration_minutes=60,
                    expires_at=5000,
                )
            )
            session.flush()
            session.add(
                ExamSession(
                    session_id="ses_" + suffix,
                    invitation_hash=suffix * 64,
                    browser_hash=suffix * 64,
                    status="submitting",
                    admitted_at=1000,
                    started_at=1010,
                    deadline_at=4610,
                )
            )

    def persist(self, **changes):
        arguments = dict(
            session_id="ses_1",
            idempotency_key=str(uuid.uuid4()),
            files=[SnapshotFile("src/main.py", "return 1\n")],
            public_test_output="2 passed\n",
        )
        return self.service.persist_frozen_submission(**(arguments | changes))

    def assert_record_counts(self, expected):
        with session_scope(self.factory) as session:
            for model in (Submission, Snapshot):
                self.assertEqual(
                    session.scalar(select(func.count()).select_from(model)), expected
                )

    def test_receipt_snapshot_and_session_are_persisted_together(self):
        receipt = self.persist()
        detail = self.service.get_submission(receipt["submission_id"])
        self.assertEqual(receipt["status"], "frozen")
        self.assertEqual(detail["snapshot_id"], receipt["snapshot_id"])
        self.assertEqual(
            detail["files"], [{"path": "src/main.py", "content": "return 1\n"}]
        )
        self.assertEqual(detail["public_test_output"], "2 passed\n")
        self.assertEqual(
            self.service.list_submissions()[0]["submission_id"],
            receipt["submission_id"],
        )
        with session_scope(self.factory) as session:
            record = session.get(ExamSession, "ses_1")
            self.assertEqual((record.status, record.submitted_at), ("submitted", 2000))
        self.assert_record_counts(1)

    def test_retries_with_same_or_new_key_return_the_first_submission(self):
        receipt = self.persist(idempotency_key="first-key")

        def invalid_new_export():
            raise AssertionError("A replay must not request another snapshot")
            yield

        for key in ("first-key", "another-key"):
            self.assertEqual(
                self.persist(idempotency_key=key, files=invalid_new_export()), receipt
            )
        self.assert_record_counts(1)
        self.assertEqual(len(list(self.root.rglob("*.json"))), 1)

    def test_concurrent_submissions_produce_one_final_answer(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(
                executor.map(
                    lambda number: self.persist(
                        files=[SnapshotFile("a.py", str(number))]
                    ),
                    range(8),
                )
            )
        self.assertTrue(all(result == results[0] for result in results))
        self.assert_record_counts(1)
        self.assertEqual(len(list(self.root.rglob("*.json"))), 1)
        content = self.service.get_submission(results[0]["submission_id"])["files"][0][
            "content"
        ]
        self.assertIn(content, [str(number) for number in range(8)])

    def test_unknown_session_and_submission_are_not_found(self):
        with self.assertRaises(SubmissionNotFound):
            self.persist(session_id="ses_missing")
        self.assertIsNone(self.service.get_submission("sub_missing"))
        self.assertFalse(self.root.exists())

    def test_active_or_unstarted_sessions_cannot_be_persisted(self):
        for status in (
            "admitted",
            "ready",
            "in_progress",
            "provision_failed",
            "submission_failed",
            "submitted",
        ):
            with self.subTest(status=status):
                with session_scope(self.factory) as session:
                    session.get(ExamSession, "ses_1").status = status
                with self.assertRaises(InvalidSubmissionState):
                    self.persist()
        with session_scope(self.factory) as session:
            record = session.get(ExamSession, "ses_1")
            record.status, record.started_at, record.deadline_at = (
                "submitting",
                None,
                None,
            )
        with self.assertRaises(InvalidSubmissionState):
            self.persist()
        self.assert_record_counts(0)

    def test_invalid_keys_and_invalid_snapshot_do_not_commit(self):
        for key in ("", "a" * 129, "invalid/key", "line\nbreak"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.persist(idempotency_key=key)
        with self.assertRaises(SnapshotValidationError):
            self.persist(files=[SnapshotFile("../unsafe", "text")])
        self.assert_record_counts(0)

    def test_storage_failure_does_not_finalize_the_session(self):
        with patch.object(
            self.store, "write", side_effect=SnapshotStorageError("write failed")
        ):
            with self.assertRaises(SnapshotStorageError):
                self.persist()
        self.assert_record_counts(0)
        with session_scope(self.factory) as session:
            record = session.get(ExamSession, "ses_1")
            self.assertEqual((record.status, record.submitted_at), ("submitting", None))
        self.persist()
        self.assert_record_counts(1)

    def test_commit_failure_rolls_back_records_and_allows_reusing_the_object(self):
        class BrokenSession(Session):
            pass

        def fail_commit(session):
            raise RuntimeError("simulated commit failure")

        event.listen(BrokenSession, "before_commit", fail_commit)
        factory = sessionmaker(
            self.engine, class_=BrokenSession, expire_on_commit=False
        )
        broken = SubmissionService(self.store, factory, clock=lambda: 2000)
        files = [SnapshotFile("src/main.py", "return 1\n")]
        with self.assertRaisesRegex(RuntimeError, "simulated commit failure"):
            broken.persist_frozen_submission(
                "ses_1", "key", files, public_test_output="2 passed\n"
            )
        self.assert_record_counts(0)
        self.assertEqual(self.service.list_submissions(), [])
        self.assertEqual(len(list(self.root.rglob("*.json"))), 1)
        self.persist()
        self.assert_record_counts(1)
        self.assertEqual(len(list(self.root.rglob("*.json"))), 1)

    def test_reader_uses_frozen_identity_and_does_not_expose_private_fields(self):
        receipt = self.persist()
        with session_scope(self.factory) as session:
            invitation = session.get(Invitation, "1" * 64)
            invitation.candidate_name, invitation.exam_title = "后改姓名", "后改场次"
        reader = SubmissionService(SnapshotStore(self.root), self.factory)
        detail = reader.get_submission(receipt["submission_id"])
        self.assertEqual(
            (detail["candidate_name"], detail["exam_title"]),
            ("冻结前姓名", "冻结前场次"),
        )
        for private in (
            "idempotency_key",
            "content_sha256",
            "browser_hash",
            "invitation_hash",
            "storage_path",
        ):
            self.assertNotIn(private, detail)
            self.assertNotIn(private, reader.list_submissions()[0])

    def test_corrupted_snapshot_is_not_replaced_by_live_or_new_files(self):
        receipt = self.persist()
        next(self.root.rglob("*.json")).write_bytes(b"corrupted")
        with self.assertRaises(SnapshotStorageError):
            self.service.get_submission(receipt["submission_id"])
        self.assertEqual(
            self.persist(files=[SnapshotFile("new.py", "new content")]), receipt
        )
        self.assertEqual(next(self.root.rglob("*.json")).read_bytes(), b"corrupted")

    def test_snapshot_cannot_be_attached_to_another_session(self):
        receipt = self.persist()
        self.seed("2")
        with self.assertRaises(IntegrityError), session_scope(self.factory) as session:
            session.add(
                Submission(
                    submission_id="sub_wrong_owner",
                    session_id="ses_2",
                    snapshot_id=receipt["snapshot_id"],
                    idempotency_key="key",
                    candidate_id="candidate-2",
                    candidate_name="test",
                    exam_id="exam-001",
                    exam_title="test",
                    admitted_at=1000,
                    started_at=1010,
                    submitted_at=2000,
                )
            )

    def test_empty_snapshot_and_empty_output_are_valid(self):
        receipt = self.persist(files=[], public_test_output="")
        detail = self.service.get_submission(receipt["submission_id"])
        self.assertEqual(detail["files"], [])
        self.assertEqual(detail["public_test_output"], "")


if __name__ == "__main__":
    unittest.main()

"""Persistent revision observations reuse the caller's PostgreSQL session lock."""

import multiprocessing
import os
import unittest
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import patch

import test_db_postgres as database_tests
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import event, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.lifecycle import SessionBusy, SessionLifecycle
from app.db.models import ExamSession, WorkspaceFileRevision
from app.db.session import create_db_engine, session_scope
from app.exams.postgres import PostgresEntryStore
from app.main import create_app
from app.workspace.files import ALLOWED_FILES
from app.workspace.router import router

PATH = "src/booking.py"
FIRST = sha256(b"first").hexdigest()
SECOND = sha256(b"second").hexdigest()
HEADERS = {"X-VibeEval-Request": "1"}


def observe_in_child(url, session_id, digest, writer):
    engine = create_db_engine(url)
    try:
        lifecycle = SessionLifecycle(sessionmaker(engine, expire_on_commit=False))
        try:
            with lifecycle.lock_session(session_id) as locked:
                version = locked.observe_file_revision(PATH, digest)
            writer.send(("committed", version))
        except SessionBusy:
            writer.send(("busy", None))
    finally:
        engine.dispose()
        writer.close()


@unittest.skipUnless(
    os.environ.get("VIBEEVAL_RUN_DB_TESTS") == "1", "Requires PostgreSQL"
)
class WorkspaceFileRevisionTests(unittest.TestCase):
    setUpClass = classmethod(database_tests.PostgreSQLTests.setUpClass.__func__)
    drop_test_database = classmethod(
        database_tests.PostgreSQLTests.drop_test_database.__func__
    )

    def setUp(self):
        database_tests.PostgreSQLTests.setUp(self)
        self.seed("1")
        self.lifecycle = SessionLifecycle(self.factory, clock=lambda: 2200000010)

    def seed(self, suffix):
        with session_scope(self.factory) as session:
            session.add(
                database_tests.PostgreSQLTests.invitation(
                    token_hash=suffix * 64, candidate_id="candidate-" + suffix
                )
            )
            session.flush()
            session.add(
                database_tests.PostgreSQLTests.exam_session(
                    session_id="ses_" + suffix,
                    invitation_hash=suffix * 64,
                    browser_hash=suffix * 64,
                )
            )

    def saved(self, session_id="ses_1", path=PATH):
        with session_scope(self.factory) as session:
            row = session.get(WorkspaceFileRevision, (session_id, path))
            return None if row is None else (row.version, row.content_sha256)

    def child(self, digest):
        ctx = multiprocessing.get_context("spawn")
        reader, writer = ctx.Pipe(duplex=False)
        process = ctx.Process(
            target=observe_in_child,
            args=(
                self.engine.url.render_as_string(hide_password=False),
                "ses_1",
                digest,
                writer,
            ),
        )
        process.start()
        writer.close()
        try:
            self.assertTrue(reader.poll(20), "Revision worker did not finish")
            result = reader.recv()
            process.join(timeout=10)
            self.assertEqual(process.exitcode, 0)
            return result
        finally:
            reader.close()
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            process.close()

    def test_first_same_changed_and_reverted_content_in_one_transaction(self):
        with self.lifecycle.lock_session("ses_1") as locked:
            self.assertEqual(locked.observe_file_revision(PATH, FIRST), 1)
            self.assertEqual(locked.observe_file_revision(PATH, FIRST), 1)
            self.assertEqual(locked.observe_file_revision(PATH, SECOND), 2)
            self.assertEqual(locked.observe_file_revision(PATH, SECOND), 2)
            self.assertEqual(locked.observe_file_revision(PATH, FIRST), 3)
        self.assertEqual(self.saved(), (3, FIRST))

    def test_revision_survives_new_engine_and_lifecycle_instances(self):
        with self.lifecycle.lock_session("ses_1") as locked:
            self.assertEqual(locked.observe_file_revision(PATH, FIRST), 1)
        engine = create_db_engine(self.engine.url)
        try:
            restarted = SessionLifecycle(sessionmaker(engine, expire_on_commit=False))
            with restarted.lock_session("ses_1") as locked:
                self.assertEqual(locked.observe_file_revision(PATH, FIRST), 1)
                self.assertEqual(locked.observe_file_revision(PATH, SECOND), 2)
        finally:
            engine.dispose()
        self.assertEqual(self.saved(), (2, SECOND))

    def test_observations_do_not_commit_or_acquire_another_row_lock(self):
        statements = []

        def capture(connection, cursor, statement, parameters, context, many):
            statements.append(statement.upper())

        event.listen(self.engine, "before_cursor_execute", capture)
        try:
            with self.lifecycle.lock_session("ses_1") as locked:
                with patch.object(
                    locked._session, "commit", side_effect=AssertionError("Own commit")
                ):
                    locked.observe_file_revision(PATH, FIRST)
                    locked.observe_file_revision(PATH, SECOND)
                self.assertIsNone(self.saved())
        finally:
            event.remove(self.engine, "before_cursor_execute", capture)
        self.assertEqual(sum("FOR UPDATE" in sql for sql in statements), 1)
        self.assertEqual(self.saved(), (2, SECOND))

    def test_new_and_changed_rows_roll_back_with_the_outer_transaction(self):
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with self.lifecycle.lock_session("ses_1") as locked:
                locked.observe_file_revision(PATH, FIRST)
                raise RuntimeError("abort")
        self.assertIsNone(self.saved())
        with self.lifecycle.lock_session("ses_1") as locked:
            locked.observe_file_revision(PATH, FIRST)
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with self.lifecycle.lock_session("ses_1") as locked:
                locked.observe_file_revision(PATH, SECOND)
                locked.observe_file_revision("tests/test_candidate.py", SECOND)
                raise RuntimeError("abort")
        self.assertEqual(self.saved(), (1, FIRST))
        self.assertIsNone(self.saved(path="tests/test_candidate.py"))

    def test_repeated_observations_work_with_autoflush_disabled(self):
        lifecycle = SessionLifecycle(
            sessionmaker(self.engine, expire_on_commit=False, autoflush=False)
        )
        with lifecycle.lock_session("ses_1") as locked:
            self.assertEqual(locked.observe_file_revision(PATH, FIRST), 1)
            self.assertEqual(locked.observe_file_revision(PATH, FIRST), 1)
            self.assertEqual(locked.observe_file_revision(PATH, SECOND), 2)
        self.assertEqual(self.saved(), (2, SECOND))

    def test_paths_and_sessions_have_independent_revisions(self):
        self.seed("2")
        with self.lifecycle.lock_session("ses_1") as locked:
            locked.observe_file_revision(PATH, FIRST)
            locked.observe_file_revision(PATH, SECOND)
            self.assertEqual(locked.observe_file_revision("README.md", FIRST), 1)
        with self.lifecycle.lock_session("ses_2") as locked:
            self.assertEqual(locked.observe_file_revision(PATH, SECOND), 1)
        self.assertEqual(self.saved(), (2, SECOND))
        self.assertEqual(self.saved("ses_2"), (1, SECOND))
        self.assertEqual(self.saved(path="README.md"), (1, FIRST))

    def test_closed_lock_context_cannot_observe_or_update(self):
        with self.lifecycle.lock_session("ses_1") as locked:
            locked.observe_file_revision(PATH, FIRST)
        with self.assertRaises(RuntimeError):
            locked.observe_file_revision(PATH, SECOND)
        self.assertEqual(self.saved(), (1, FIRST))

    def test_invalid_path_and_digest_fail_without_modifying_the_revision(self):
        with self.lifecycle.lock_session("ses_1") as locked:
            locked.observe_file_revision(PATH, FIRST)
            for path in (
                "",
                "/src/a.py",
                "../a.py",
                "a/./b",
                "a//b",
                "a/",
                "a\\b",
                "C:a.py",
                "a\x00b",
                "a\nb",
                "a\x7fb",
                "x" * 501,
                None,
            ):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    locked.observe_file_revision(path, SECOND)
            for digest in ("", "a" * 63, "g" * 64, FIRST.upper(), FIRST + "\n", None):
                with self.subTest(digest=digest), self.assertRaises(ValueError):
                    locked.observe_file_revision(PATH, digest)
        self.assertEqual(self.saved(), (1, FIRST))

    def test_other_process_is_busy_then_observes_the_committed_version(self):
        with self.lifecycle.lock_session("ses_1") as locked:
            locked.observe_file_revision(PATH, FIRST)
            self.assertEqual(self.child(SECOND), ("busy", None))
        self.assertEqual(self.child(FIRST), ("committed", 1))
        self.assertEqual(self.child(SECOND), ("committed", 2))
        self.assertEqual(self.saved(), (2, SECOND))

    def test_database_rejects_missing_session_bad_values_and_duplicate_key(self):
        for changes in (
            {"session_id": "missing"},
            {"path": ""},
            {"version": 0},
            {"version": -1},
            {"content_sha256": "bad"},
        ):
            values = dict(
                session_id="ses_1", path=PATH, version=1, content_sha256=FIRST
            )
            values.update(changes)
            with self.subTest(changes=changes), self.assertRaises(IntegrityError):
                with session_scope(self.factory) as session:
                    session.add(WorkspaceFileRevision(**values))
        with self.lifecycle.lock_session("ses_1") as locked:
            locked.observe_file_revision(PATH, FIRST)
        with self.assertRaises(IntegrityError), session_scope(self.factory) as session:
            session.add(
                WorkspaceFileRevision(
                    session_id="ses_1", path=PATH, version=2, content_sha256=SECOND
                )
            )

    def test_deleting_session_cascades_only_its_revision_rows(self):
        self.seed("2")
        for sid in ("ses_1", "ses_2"):
            with self.lifecycle.lock_session(sid) as locked:
                locked.observe_file_revision(PATH, FIRST)
        with session_scope(self.factory) as session:
            session.delete(session.get(ExamSession, "ses_1"))
        self.assertIsNone(self.saved())
        self.assertEqual(self.saved("ses_2"), (1, FIRST))

    def test_migration_round_trip_preserves_existing_session_and_deadline(self):
        image = "sha256:" + "a" * 64
        self.lifecycle.prepare_sandbox("ses_1", image)
        with self.lifecycle.lock_session("ses_1") as locked:
            locked.bind_sandbox("original-container", image_ref=image)
            locked.mark_ready()
        before = self.lifecycle.start_exam("ses_1")
        with self.lifecycle.lock_session("ses_1") as locked:
            locked.observe_file_revision(PATH, FIRST)
        command.downgrade(self.migration, "0004")
        try:
            self.assertFalse(inspect(self.engine).has_table("workspace_file_revisions"))
            self.assertEqual(self.lifecycle.get_session("ses_1"), before)
        finally:
            command.upgrade(self.migration, "head")
        self.assertEqual(self.lifecycle.get_session("ses_1"), before)
        self.assertIsNone(self.saved())
        with self.lifecycle.lock_session("ses_1") as locked:
            self.assertEqual(locked.observe_file_revision(PATH, FIRST), 1)


@unittest.skipUnless(
    os.environ.get("VIBEEVAL_RUN_DB_TESTS") == "1", "Requires PostgreSQL"
)
class WorkspaceReadRevisionIntegrationTests(unittest.TestCase):
    setUpClass = classmethod(database_tests.PostgreSQLTests.setUpClass.__func__)
    drop_test_database = classmethod(
        database_tests.PostgreSQLTests.drop_test_database.__func__
    )

    def setUp(self):
        database_tests.PostgreSQLTests.setUp(self)
        entry = PostgresEntryStore(self.factory)
        token, _ = entry.invite("c1", "考生", "e1", "考试", 60, 24)
        self.contents = {path: "" for path in ALLOWED_FILES}
        self.sandbox = SimpleNamespace(
            read_file=lambda view, path: self.contents[path],
            read_files=lambda view: dict(self.contents),
        )
        app = create_app({}, entry_store=entry, submission_sandbox=self.sandbox)
        app.include_router(router)
        self.client = TestClient(app, headers=HEADERS)
        self.addCleanup(self.client.close)
        self.client.get("/api/v1/sessions/current")
        response = self.client.post(
            "/api/v1/sessions/verify-token", json={"token": token}
        )
        self.assertEqual(response.status_code, 200)
        self.sid = response.json()["session_id"]
        self.base = "/api/v1/sessions/" + self.sid
        self.lifecycle = SessionLifecycle(self.factory)
        image = "sha256:" + "b" * 64
        self.lifecycle.prepare_sandbox(self.sid, image)
        with self.lifecycle.lock_session(self.sid) as locked:
            locked.bind_sandbox("read-container", image_ref=image)
            locked.mark_ready()
        self.lifecycle.start_exam(self.sid)

    def test_existing_read_routes_return_durable_versions_after_terminal_changes(self):
        first = self.client.get(self.base + "/files")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(len(first.json()["files"]), 6)
        self.assertTrue(all(item["version"] == 1 for item in first.json()["files"]))
        self.assertEqual(self.client.get(self.base + "/files").json(), first.json())
        self.contents[PATH] = "# terminal edit\n"
        changed = self.client.get(self.base + "/file", params={"path": PATH})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(
            changed.json(), dict(path=PATH, content=self.contents[PATH], version=2)
        )
        self.assertEqual(
            self.client.get(self.base + "/file", params={"path": PATH}).json(),
            changed.json(),
        )
        with session_scope(self.factory) as session:
            row = session.get(WorkspaceFileRevision, (self.sid, PATH))
            self.assertEqual(
                (row.version, row.content_sha256),
                (2, sha256(self.contents[PATH].encode()).hexdigest()),
            )
        self.lifecycle.begin_submission(self.sid)
        self.assertEqual(self.client.get(self.base + "/files").status_code, 409)

    def test_tree_failure_rolls_back_every_observed_file(self):
        self.contents["src/main.py"] = "\ud800"
        response = self.client.get(self.base + "/files")
        self.assertEqual(response.status_code, 503)
        with session_scope(self.factory) as session:
            self.assertEqual(
                session.scalar(select(func.count()).select_from(WorkspaceFileRevision)),
                0,
            )
        self.contents["src/main.py"] = "fixed"
        response = self.client.get(self.base + "/files")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(all(item["version"] == 1 for item in response.json()["files"]))

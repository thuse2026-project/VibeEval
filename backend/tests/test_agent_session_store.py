import concurrent.futures
import unittest

from app.agent import MemorySessionStore, begin_turn, create_conversation, start_step
from app.agent.session_types import SessionConflict, SessionError


class AgentSessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = MemorySessionStore()
        self.initial = create_conversation("scope")
        self.store.create(self.initial)

    def test_create_load_and_scope_isolation(self):
        self.assertEqual(self.store.load("scope", self.initial.id), self.initial)
        with self.assertRaises(SessionError):
            self.store.load("other-scope", self.initial.id)
        with self.assertRaises(SessionConflict):
            self.store.create(self.initial)

    def test_save_checks_revision_and_same_write_is_idempotent(self):
        updated = begin_turn(self.initial, "hello", request_key="one")
        self.store.save(updated, expected_revision=0)
        self.store.save(updated, expected_revision=0)
        stale = begin_turn(self.initial, "different", request_key="two")
        with self.assertRaises(SessionConflict):
            self.store.save(stale, expected_revision=0)
        self.assertEqual(self.store.load("scope", self.initial.id), updated)

    def test_save_rejects_skipping_a_revision_and_unknown_conversation(self):
        updated = start_step(begin_turn(self.initial, "hello", request_key="one"))
        with self.assertRaises(SessionConflict):
            self.store.save(updated, expected_revision=0)
        with self.assertRaises(SessionError):
            self.store.save(create_conversation("unknown"), expected_revision=0)

    def test_concurrent_saves_have_one_winner(self):
        candidates = [
            begin_turn(self.initial, text, request_key=text) for text in ("one", "two")
        ]

        def save(state):
            try:
                self.store.save(state, expected_revision=0)
                return "saved"
            except SessionConflict:
                return "conflict"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(
                list(pool.map(save, candidates)), ["saved", "conflict"]
            )

    def test_store_instances_do_not_share_global_history(self):
        with self.assertRaises(SessionError):
            MemorySessionStore().load("scope", self.initial.id)

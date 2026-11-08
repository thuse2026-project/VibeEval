"""Exact edit semantics, immutable proposals and native capability declarations."""

import unittest

from pydantic import ValidationError

from app.agent_integration.file_changes import (
    ApplyPatch,
    WriteFile,
    proposed_text,
    unified_diff,
)
from app.agent_integration.reading_types import ReadingError
from app.agent_integration.runtime import runtime_registry
from app.agent_integration.sources import SourceReader


class FileChangeTests(unittest.TestCase):
    def patch(self, *edits):
        return ApplyPatch(path="src/booking.py", expected_version=1, edits=edits)

    def test_schema_pins_one_writable_file_without_sandbox_or_permission_fields(self):
        for extra in (
            {"path": "src/main.py"},
            {"path": "../outside"},
            {"expected_version": True},
            {"sandbox_id": "foreign"},
            {"approved": True},
            {"content": "bad\x00file"},
            {"content": "\ud800"},
        ):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                WriteFile.model_validate(
                    {
                        "path": "src/booking.py",
                        "expected_version": 1,
                        "content": "",
                        **extra,
                    }
                )

    def test_full_write_can_empty_file_and_preserves_unicode_crlf(self):
        args = WriteFile(path="src/booking.py", expected_version=1, content="")
        self.assertEqual(proposed_text("old", args), "")
        after = "中文🙂\r\nnext\r\n"
        self.assertEqual(
            proposed_text("old", args.model_copy(update={"content": after})), after
        )

    def test_patch_uses_ordered_exact_matches_and_preserves_untouched_bytes(self):
        before = "head\r\nvalue = '中文🙂'\r\ntail"
        args = self.patch(
            {"old_text": "value = '中文🙂'", "new_text": "value = 2"},
            {"old_text": "value = 2", "new_text": "value = 3"},
        )
        self.assertEqual(proposed_text(before, args), "head\r\nvalue = 3\r\ntail")
        self.assertEqual(before, "head\r\nvalue = '中文🙂'\r\ntail")

    def test_missing_or_ambiguous_context_never_partially_applies(self):
        for before, old, code in (
            ("abc", "missing", "PATCH_CONTEXT_NOT_FOUND"),
            ("aba aba", "aba", "PATCH_CONTEXT_AMBIGUOUS"),
            ("aaa", "aa", "PATCH_CONTEXT_AMBIGUOUS"),
        ):
            with self.subTest(before=before), self.assertRaises(ReadingError) as raised:
                proposed_text(
                    before, self.patch({"old_text": old, "new_text": "replacement"})
                )
            self.assertEqual(raised.exception.code, code)
        with self.assertRaises(ReadingError):
            proposed_text(
                "abc",
                self.patch(
                    {"old_text": "a", "new_text": "x"},
                    {"old_text": "missing", "new_text": "y"},
                ),
            )

    def test_patch_does_not_normalize_whitespace_and_empty_context_is_invalid(self):
        with self.assertRaises(ReadingError):
            proposed_text("a\r\nb", self.patch({"old_text": "a\nb", "new_text": "x"}))
        with self.assertRaises(ValidationError):
            self.patch({"old_text": "", "new_text": "insert"})

    def test_proposed_file_cannot_exceed_workspace_limit(self):
        with self.assertRaises(ReadingError) as raised:
            proposed_text(
                "x" * 1048575 + "a", self.patch({"old_text": "a", "new_text": "bb"})
            )
        self.assertEqual(raised.exception.code, "FILE_TOO_LARGE")

    def test_diff_marks_missing_final_newline_and_noop_is_empty(self):
        self.assertEqual(unified_diff("same", "same", "src/booking.py"), "")
        diff = unified_diff("before", "after", "src/booking.py")
        self.assertIn("--- a/src/booking.py\n", diff)
        self.assertIn("-before\n\\ No newline at end of file\n", diff)
        self.assertIn("+after\n\\ No newline at end of file\n", diff)

    def test_edit_tools_require_both_action_mode_and_backend_capability(self):
        reader = SourceReader(None)

        def names(mode, available):
            return {
                entry["function"]["name"]
                for entry in runtime_registry(
                    reader, mode, True, available
                ).specifications()
            }

        self.assertNotIn("write_file", names("read_only", True))
        self.assertNotIn("write_file", names("sandbox_auto", False))
        self.assertTrue({"write_file", "apply_patch"} <= names("confirm_actions", True))

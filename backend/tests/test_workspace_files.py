import unittest

from app.workspace.files import (
    InvalidFilePath,
    ReadOnlyFile,
    VersionConflict,
    encode_content,
    observe_revision,
    require_version,
    validate_path,
)


class WorkspacePolicyTests(unittest.TestCase):
    def test_reject_nonmanifest_paths(self):
        for path in [
            "../src/booking.py",
            "/workspace/src/booking.py",
            "src\\booking.py",
            "src/./booking.py",
            "src/%2e%2e/a",
            "src/booking.py\x00",
            "maintainer/answer.py",
        ]:
            with self.subTest(path=path), self.assertRaises(InvalidFilePath):
                validate_path(path)

    def test_public_files_readable_but_not_writable(self):
        for path in [
            "README.md",
            "requirements.txt",
            "src/main.py",
            "tests/test_public.py",
        ]:
            self.assertEqual(validate_path(path), path)
            with self.assertRaises(ReadOnlyFile):
                validate_path(path, write=True)
        for path in ["src/booking.py", "tests/test_candidate.py"]:
            self.assertEqual(validate_path(path, write=True), path)

    def test_size_limit_counts_utf8_bytes(self):
        with self.assertRaises(ValueError):
            encode_content("中" * 400000)
        self.assertEqual(encode_content(""), b"")

    def test_observed_shell_edit_invalidates_old_version(self):
        first = observe_revision(None, "old")
        self.assertEqual(observe_revision(first, "old"), first)
        changed = observe_revision(first, "shell edit")
        self.assertEqual(changed.version, 2)
        with self.assertRaises(VersionConflict):
            require_version(changed, 1)
        require_version(changed, 2)

    def test_boolean_is_not_a_version(self):
        with self.assertRaises(ValueError):
            require_version(observe_revision(None, ""), True)

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

from app.submissions.storage import (
    SnapshotFile,
    SnapshotStorageError,
    SnapshotStore,
    SnapshotValidationError,
    _windows_comparison_path,
)


class SnapshotStorageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "snapshots"
        self.store = SnapshotStore(self.root)

    def object_path(self, stored):
        return self.root / stored.content_sha256[:2] / (stored.content_sha256 + ".json")

    def read(self, stored, **changes):
        return self.store.read(
            stored.content_sha256,
            **(
                {"size_bytes": stored.size_bytes, "file_count": stored.file_count}
                | changes
            ),
        )

    def test_unicode_and_empty_files_round_trip_in_stable_order(self):
        stored = self.store.write(
            [
                SnapshotFile("src/中文.py", "print('你好')\n"),
                SnapshotFile("README.md", ""),
            ],
            "2 passed\n",
        )
        content = self.read(stored)
        self.assertEqual(
            [entry.path for entry in content.files], ["README.md", "src/中文.py"]
        )
        self.assertEqual(content.files[1].content, "print('你好')\n")
        self.assertEqual(content.public_test_output, "2 passed\n")
        self.assertEqual(self.object_path(stored).stat().st_size, stored.size_bytes)

    def test_reordered_inputs_reuse_the_same_object_without_overwriting(self):
        files = [SnapshotFile("b.py", "b"), SnapshotFile("a.py", "a")]
        first = self.store.write(files)
        original_modified_at = self.object_path(first).stat().st_mtime_ns
        second = self.store.write(reversed(files))
        self.assertEqual(first, second)
        self.assertEqual(
            self.object_path(first).stat().st_mtime_ns, original_modified_at
        )
        self.assertEqual(len(list(self.root.rglob("*.json"))), 1)
        self.assertEqual(list(self.root.rglob(".tmp-*")), [])

    def test_null_and_empty_public_test_output_remain_distinct(self):
        absent = self.store.write([], None)
        empty = self.store.write([], "")
        self.assertNotEqual(absent.content_sha256, empty.content_sha256)
        self.assertEqual(self.read(absent).files, ())
        self.assertIsNone(self.read(absent).public_test_output)
        self.assertEqual(self.read(empty).public_test_output, "")

    def test_unsafe_or_ambiguous_paths_are_rejected(self):
        for path in (
            "",
            "/root.py",
            "../root.py",
            "a/../b",
            "a//b",
            "./b",
            "a/",
            "C:/b",
            "a\\b",
            "a\x00b",
            "a\nb",
            "a" * 501,
        ):
            with self.subTest(path=path), self.assertRaises(SnapshotValidationError):
                self.store.write([SnapshotFile(path, "content")])
        self.assertFalse(self.root.exists())

    def test_duplicate_and_file_directory_conflicts_are_rejected(self):
        for files in (
            [SnapshotFile("a.py", "1"), SnapshotFile("a.py", "2")],
            [SnapshotFile("src", "file"), SnapshotFile("src/a.py", "nested")],
        ):
            with self.subTest(files=files), self.assertRaises(SnapshotValidationError):
                self.store.write(files)

    def test_non_text_and_invalid_unicode_are_rejected(self):
        for files in (
            [SnapshotFile("a.py", b"binary")],
            [SnapshotFile("a.py", "\ud800")],
            [{"path": "a.py", "content": "x"}],
        ):
            with self.subTest(files=files), self.assertRaises(SnapshotValidationError):
                self.store.write(files)
        with self.assertRaises(SnapshotValidationError):
            self.store.write([], public_test_output=123)

    def test_file_count_and_utf8_source_byte_limits_are_enforced(self):
        store = SnapshotStore(self.root, max_files=1, max_source_bytes=2)
        with self.assertRaises(SnapshotValidationError):
            store.write([SnapshotFile("a.py", ""), SnapshotFile("b.py", "")])
        with self.assertRaises(SnapshotValidationError):
            store.write([SnapshotFile("a.py", "中")])

    def test_output_and_serialized_size_limits_are_enforced(self):
        with self.assertRaises(SnapshotValidationError):
            SnapshotStore(self.root, max_output_bytes=2).write([], "中")
        with self.assertRaises(SnapshotValidationError):
            SnapshotStore(self.root, max_blob_bytes=20).write([])

    def test_corruption_is_detected_and_never_overwritten(self):
        files = [SnapshotFile("a.py", "return 1")]
        stored = self.store.write(files)
        path = self.object_path(stored)
        corrupted = b"!" * stored.size_bytes
        path.write_bytes(corrupted)
        with self.assertRaises(SnapshotStorageError):
            self.read(stored)
        with self.assertRaises(SnapshotStorageError):
            self.store.write(files)
        self.assertEqual(path.read_bytes(), corrupted)

    def test_missing_object_and_metadata_mismatch_fail_explicitly(self):
        stored = self.store.write([SnapshotFile("a.py", "x")])
        with self.assertRaises(SnapshotStorageError):
            self.read(stored, file_count=2)
        with self.assertRaises(SnapshotStorageError):
            self.read(stored, size_bytes=stored.size_bytes + 1)
        self.object_path(stored).unlink()
        with self.assertRaises(SnapshotStorageError):
            self.read(stored)

    def test_invalid_object_references_cannot_select_arbitrary_paths(self):
        for digest in ("../secret", "C:/secret", "a" * 63, "A" * 64):
            with self.subTest(digest=digest), self.assertRaises(SnapshotStorageError):
                self.store.read(digest, size_bytes=1, file_count=0)

    def test_fsync_failure_leaves_no_published_object_or_temporary_file(self):
        with patch(
            "app.submissions.storage.os.fsync", side_effect=OSError("private/path")
        ):
            with self.assertRaises(SnapshotStorageError) as raised:
                self.store.write([SnapshotFile("a.py", "x")])
        self.assertNotIn("private/path", str(raised.exception))
        self.assertEqual(list(self.root.rglob("*.json")), [])
        self.assertEqual(list(self.root.rglob(".tmp-*")), [])

    def test_concurrent_writers_publish_one_complete_object(self):
        def write(_):
            return self.store.write([SnapshotFile("a.py", "complete content")])

        with ThreadPoolExecutor(max_workers=16) as executor:
            results = list(executor.map(write, range(128)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(self.read(results[0]).files[0].content, "complete content")
        self.assertEqual(len(list(self.root.rglob("*.json"))), 1)
        self.assertEqual(list(self.root.rglob(".tmp-*")), [])

    def test_hard_link_leaf_namespace_alias_does_not_reject_a_valid_object(self):
        original_resolve = Path.resolve

        def windows_hard_link_alias(path, *args, **kwargs):
            if path.suffix == ".json":
                return self.root.parent / "alternate-hard-link-name.json"
            return original_resolve(path, *args, **kwargs)

        with patch("app.submissions.storage.Path.resolve", new=windows_hard_link_alias):
            stored = self.store.write([SnapshotFile("a.py", "safe content")])
            self.assertEqual(self.read(stored).files[0].content, "safe content")

    def test_directory_escape_is_still_rejected(self):
        original_resolve = Path.resolve

        def escaped_directory(path, *args, **kwargs):
            if path.parent == self.root:
                return self.root.parent / "outside"
            return original_resolve(path, *args, **kwargs)

        with patch("app.submissions.storage.Path.resolve", new=escaped_directory):
            with self.assertRaises(SnapshotStorageError):
                self.store.write([SnapshotFile("a.py", "safe content")])
        self.assertFalse((self.root.parent / "outside").exists())

    def test_windows_dos_and_extended_namespaces_have_the_same_containment(self):
        for ordinary, extended in (
            (r"C:\cache\snapshots", r"\\?\C:\cache\snapshots"),
            (r"\\server\share\snapshots", r"\\?\UNC\server\share\snapshots"),
        ):
            with self.subTest(ordinary=ordinary):
                root = _windows_comparison_path(PureWindowsPath(ordinary))
                self.assertEqual(
                    root, _windows_comparison_path(PureWindowsPath(extended))
                )
                self.assertTrue(
                    _windows_comparison_path(
                        PureWindowsPath(extended) / "ab"
                    ).is_relative_to(root)
                )
                self.assertFalse(
                    _windows_comparison_path(
                        PureWindowsPath(extended).parent / "outside"
                    ).is_relative_to(root)
                )

    def test_leaf_symbolic_link_is_still_rejected(self):
        stored = self.store.write([SnapshotFile("a.py", "safe content")])
        destination = self.object_path(stored)
        outside = self.root.parent / "outside.json"
        outside.write_bytes(destination.read_bytes())
        destination.unlink()
        try:
            destination.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Creating symbolic links is unavailable for this account")
        with self.assertRaises(SnapshotStorageError):
            self.read(stored)

    def test_invalid_limit_configuration_is_rejected(self):
        for limit in (0, -1, True, "100"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                SnapshotStore(self.root, max_blob_bytes=limit)

    def test_temporary_cleanup_failure_has_no_internal_path_in_error(self):
        with patch(
            "app.submissions.storage.Path.unlink",
            side_effect=OSError("private/internal/path"),
        ):
            with self.assertRaises(SnapshotStorageError) as raised:
                self.store.write([SnapshotFile("a.py", "x")])
        self.assertNotIn("private/internal/path", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

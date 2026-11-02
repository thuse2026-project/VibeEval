"""Content-addressed UTF-8 snapshots; existing objects are never overwritten."""

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath


def _windows_comparison_path(path: PureWindowsPath) -> PureWindowsPath:
    """Use one namespace for lexical containment; keep actual I/O paths unchanged."""
    value = str(path)
    if value.startswith("\\\\?\\"):
        return path
    if value.startswith("\\\\"):
        return PureWindowsPath("\\\\?\\UNC\\" + value[2:])
    if path.is_absolute() and len(path.drive) == 2 and path.drive.endswith(":"):
        return PureWindowsPath("\\\\?\\" + value)
    return path


class SnapshotValidationError(ValueError):
    pass


class SnapshotStorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class SnapshotFile:
    path: str
    content: str


@dataclass(frozen=True)
class SnapshotContent:
    files: tuple[SnapshotFile, ...]
    public_test_output: str | None


@dataclass(frozen=True)
class StoredSnapshot:
    content_sha256: str
    size_bytes: int
    file_count: int


class SnapshotStore:
    def __init__(
        self,
        root: Path | str | None = None,
        *,
        max_files: int = 1000,
        max_source_bytes: int = 10 * 1024 * 1024,
        max_output_bytes: int = 1024 * 1024,
        max_blob_bytes: int = 16 * 1024 * 1024,
    ):
        self.root = (
            Path(root)
            if root is not None
            else Path(__file__).resolve().parents[3] / "runtime/snapshots"
        )
        self.root = self.root.resolve()
        self.max_files = max_files
        self.max_source_bytes = max_source_bytes
        self.max_output_bytes = max_output_bytes
        self.max_blob_bytes = max_blob_bytes
        if any(
            type(value) is not int or value <= 0
            for value in (max_files, max_source_bytes, max_output_bytes, max_blob_bytes)
        ):
            raise ValueError("Snapshot limits must be positive integers")

    def _canonical(
        self, files: Iterable[SnapshotFile], output: str | None
    ) -> tuple[bytes, SnapshotContent]:
        if output is not None and not isinstance(output, str):
            raise SnapshotValidationError("Public test output must be text or null")
        try:
            if (
                output is not None
                and len(output.encode("utf-8")) > self.max_output_bytes
            ):
                raise SnapshotValidationError(
                    "Public test output exceeds the size limit"
                )
            entries = []
            paths = set()
            source_bytes = 0
            for entry in files:
                if not isinstance(entry, SnapshotFile):
                    raise SnapshotValidationError(
                        "Snapshot entries must be SnapshotFile values"
                    )
                path = entry.path
                if (
                    not isinstance(path, str)
                    or not 1 <= len(path) <= 500
                    or any(char in path for char in ("\\", ":"))
                    or any(ord(char) < 32 or ord(char) == 127 for char in path)
                    or any(part in ("", ".", "..") for part in path.split("/"))
                ):
                    raise SnapshotValidationError(
                        "Snapshot paths must be relative POSIX file paths"
                    )
                if path in paths:
                    raise SnapshotValidationError(
                        "Snapshot contains duplicate file paths"
                    )
                if not isinstance(entry.content, str):
                    raise SnapshotValidationError(
                        "Snapshot file content must be UTF-8 text"
                    )
                paths.add(path)
                entries.append(entry)
                source_bytes += len(entry.content.encode("utf-8"))
                if (
                    len(entries) > self.max_files
                    or source_bytes > self.max_source_bytes
                ):
                    raise SnapshotValidationError(
                        "Snapshot files exceed the configured limits"
                    )
            for path in paths:
                parts = path.split("/")
                if any("/".join(parts[:end]) in paths for end in range(1, len(parts))):
                    raise SnapshotValidationError(
                        "A file path cannot also be a directory"
                    )
            entries.sort(key=lambda entry: entry.path)
            payload = json.dumps(
                {
                    "version": 1,
                    "files": [
                        {"path": item.path, "content": item.content} for item in entries
                    ],
                    "public_test_output": output,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except UnicodeError:
            raise SnapshotValidationError(
                "Snapshot contains invalid UTF-8 text"
            ) from None
        if len(payload) > self.max_blob_bytes:
            raise SnapshotValidationError("Serialized snapshot exceeds the size limit")
        return payload, SnapshotContent(tuple(entries), output)

    def _path(self, digest: str) -> Path:
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise SnapshotStorageError("Invalid snapshot content reference")
        path = self.root / digest[:2] / (digest + ".json")
        # A published leaf is a hard link. Windows may resolve it using a
        # transient \\?\ spelling while another writer removes a temporary link.
        # Resolve the directory boundary instead; reject leaf symlinks separately.
        parent = path.parent.resolve()
        root = self.root
        if os.name == "nt":
            parent = _windows_comparison_path(PureWindowsPath(parent))
            root = _windows_comparison_path(PureWindowsPath(root))
        if not parent.is_relative_to(root) or path.is_symlink():
            raise SnapshotStorageError("Invalid snapshot content location")
        return path

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        if os.name != "nt":
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _verified_bytes(self, digest: str, size: int) -> bytes:
        try:
            with self._path(digest).open("rb") as stream:
                payload = stream.read(self.max_blob_bytes + 1)
        except OSError:
            raise SnapshotStorageError("Snapshot object is unavailable") from None
        if (
            len(payload) != size
            or len(payload) > self.max_blob_bytes
            or hashlib.sha256(payload).hexdigest() != digest
        ):
            raise SnapshotStorageError("Snapshot integrity verification failed")
        return payload

    def write(
        self, files: Iterable[SnapshotFile], public_test_output: str | None = None
    ) -> StoredSnapshot:
        payload, content = self._canonical(files, public_test_output)
        digest = hashlib.sha256(payload).hexdigest()
        temporary = None
        try:
            destination = self._path(digest)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".tmp-", dir=destination.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                pass
            self._verified_bytes(digest, len(payload))
            for directory in (destination.parent, self.root, self.root.parent):
                self._sync_directory(directory)
        except OSError:
            raise SnapshotStorageError("Snapshot could not be persisted") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    raise SnapshotStorageError(
                        "Snapshot temporary file could not be removed"
                    ) from None
        return StoredSnapshot(digest, len(payload), len(content.files))

    def read(self, digest: str, *, size_bytes: int, file_count: int) -> SnapshotContent:
        payload = self._verified_bytes(digest, size_bytes)
        try:
            data = json.loads(payload)
            if (
                not isinstance(data, dict)
                or set(data) != {"version", "files", "public_test_output"}
                or type(data["version"]) is not int
                or data["version"] != 1
                or not isinstance(data["files"], list)
            ):
                raise ValueError("Invalid snapshot format")
            files = []
            for item in data["files"]:
                if not isinstance(item, dict) or set(item) != {"path", "content"}:
                    raise ValueError("Invalid snapshot file")
                files.append(SnapshotFile(item["path"], item["content"]))
            _, content = self._canonical(files, data["public_test_output"])
            if len(content.files) != file_count:
                raise ValueError("Snapshot file count mismatch")
            return content
        except (ValueError, TypeError, UnicodeError):
            raise SnapshotStorageError("Snapshot content is invalid") from None

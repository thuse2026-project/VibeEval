"""Release 1.0 booking workspace policy, independent of HTTP and persistence."""

from dataclasses import dataclass
from hashlib import sha256

from app.sandbox.booking_template import EDITABLE, FILES

MAX_FILE_BYTES = 1024 * 1024
ALLOWED_FILES = frozenset(FILES) | frozenset(EDITABLE)


class InvalidFilePath(ValueError):
    pass


class ReadOnlyFile(PermissionError):
    pass


class VersionConflict(ValueError):
    pass


def validate_path(path, *, write=False):
    # Exact release manifest also rejects Windows paths, .., alternate spellings,
    # NUL, URL-encoded traversal and unexpected files without normalization.
    if not isinstance(path, str) or path not in ALLOWED_FILES:
        raise InvalidFilePath("file is not in the published task")
    if write and path not in EDITABLE:
        raise ReadOnlyFile("published file is read-only")
    return path


def encode_content(content):
    if not isinstance(content, str):
        raise ValueError("UTF-8 text required")
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_FILE_BYTES:
        raise ValueError("file exceeds 1 MiB")
    return encoded


@dataclass(frozen=True)
class FileRevision:
    version: int
    digest: str


def observe_revision(previous, content):
    """Pure decision only; caller must persist under the shared session lock.

    This detects observed terminal edits, not every intermediate shell write.
    It is not by itself a compare-and-swap implementation.
    """
    digest = sha256(encode_content(content)).hexdigest()
    if previous is None:
        return FileRevision(1, digest)
    if previous.digest == digest:
        return previous
    return FileRevision(previous.version + 1, digest)


def require_version(current, expected):
    if type(expected) is not int or expected < 1:
        raise ValueError("expected_version must be a positive integer")
    if current.version != expected:
        raise VersionConflict("file changed; reload before saving")

"""Immutable source snapshots and precise, bounded text projections."""

import hashlib
import json
import re
from dataclasses import dataclass

from .reading_types import LineSelection, ReadingError

MAX_MATERIAL_BYTES = 8 * 1024 * 1024


def digest(text: str) -> str:
    try:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        raise ReadingError("TEXT_ENCODING_INVALID") from None


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def validate_json_text(value):
    """PostgreSQL text/JSONB cannot represent NUL or lone surrogates."""
    if isinstance(value, str):
        if "\x00" in value:
            raise ReadingError("TEXT_ENCODING_INVALID")
        digest(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            validate_json_text(key)
            validate_json_text(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            validate_json_text(item)


def selection_bounds(text, selection):
    if selection is None:
        return 0, len(text)
    # Editor line numbers split CR/LF, not Python's additional Unicode/control
    # separators (e.g. a literal vertical tab inside a line).
    parts = re.split(r"(\r\n|\r|\n)", text)
    lines = [parts[i] + parts[i + 1] for i in range(0, len(parts) - 1, 2)]
    lines.append(parts[-1])
    if selection.start_line > len(lines) or selection.end_line > len(lines):
        raise ReadingError("SELECTION_OUT_OF_RANGE")
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    if isinstance(selection, LineSelection):
        return offsets[selection.start_line - 1], offsets[selection.end_line]

    def column_offset(line_number, column):
        line = lines[line_number - 1].rstrip("\r\n")
        wanted, units = column - 1, 0
        for index, character in enumerate(line):
            if units == wanted:
                return offsets[line_number - 1] + index
            units += 2 if ord(character) > 0xFFFF else 1
            if units > wanted:
                raise ReadingError("SELECTION_SPLITS_UTF16_CHARACTER")
        if units != wanted:
            raise ReadingError("SELECTION_OUT_OF_RANGE")
        return offsets[line_number - 1] + len(line)

    return column_offset(selection.start_line, selection.start_column), column_offset(
        selection.end_line, selection.end_column
    )


@dataclass(frozen=True, init=False)
class SourceSnapshot:
    kind: str
    source_id: str
    version: str
    text: str
    _metadata_json: str
    source_digest: str

    def __init__(
        self, kind, source_id, version, text, metadata=None, source_digest=None
    ):
        validate_json_text((kind, source_id, version, metadata or {}))
        if not (
            0 < len(kind) <= 40
            and 0 < len(source_id) <= 512
            and 0 < len(version) <= 200
        ):
            raise ReadingError("SOURCE_IDENTITY_INVALID")
        try:
            size = len(text.encode("utf-8"))
        except UnicodeEncodeError:
            raise ReadingError("TEXT_ENCODING_INVALID") from None
        if "\x00" in text:
            raise ReadingError("TEXT_ENCODING_INVALID")
        if size > MAX_MATERIAL_BYTES:
            raise ReadingError("MATERIAL_TOO_LARGE")
        metadata_json = encoded(metadata or {})
        if len(metadata_json) > 8000 or not isinstance(metadata or {}, dict):
            raise ReadingError("SOURCE_METADATA_INVALID")
        source_digest = source_digest or digest(text)
        if not re.fullmatch(r"[0-9a-f]{64}", source_digest):
            raise ReadingError("SOURCE_DIGEST_INVALID")
        for name, value in (
            ("kind", kind),
            ("source_id", source_id),
            ("version", version),
            ("text", text),
            ("_metadata_json", metadata_json),
            ("source_digest", source_digest),
        ):
            object.__setattr__(self, name, value)

    @property
    def metadata(self):
        return json.loads(self._metadata_json)

    @property
    def content_digest(self):
        return digest(self.text)

    @property
    def key(self):
        return digest(
            encoded(
                {
                    "kind": self.kind,
                    "id": self.source_id,
                    "version": self.version,
                    "metadata": self.metadata,
                    "source_digest": self.source_digest,
                    "content": self.content_digest,
                }
            )
        )


def fragment(snapshot, *, offset=0, max_chars=12000, selection=None):
    start, end = selection_bounds(snapshot.text, selection)
    if type(offset) is not int or offset < 0 or offset > end - start:
        raise ReadingError("OFFSET_OUT_OF_RANGE")
    if type(max_chars) is not int or not 1 <= max_chars <= 16000:
        raise ReadingError("READ_LIMIT_INVALID")
    actual_start = start + offset
    actual_end = min(end, actual_start + max_chars)
    # JSON escaping may expand a character sixfold. Keep the native result below
    # its existing 64k contract, including provenance and material ID overhead.
    while len(encoded(snapshot.text[actual_start:actual_end])) > 50000:
        actual_end = actual_start + (actual_end - actual_start) // 2
    content = snapshot.text[actual_start:actual_end]
    return {
        "source_kind": snapshot.kind,
        "source_id": snapshot.source_id,
        "source_version": snapshot.version,
        "source_sha256": snapshot.source_digest or snapshot.content_digest,
        "material_sha256": snapshot.content_digest,
        "returned_sha256": digest(content),
        "source_metadata": snapshot.metadata,
        "text": content,
        "offset_unit": "unicode_codepoint",
        "start_offset": actual_start,
        "end_offset": actual_end,
        "byte_start": len(snapshot.text[:actual_start].encode("utf-8")),
        "byte_end": len(snapshot.text[:actual_end].encode("utf-8")),
        "next_offset": actual_end - start,
        "has_more": actual_end < end,
        "truncated": actual_end < end,
        "selection": selection.model_dump(mode="json") if selection else None,
    }

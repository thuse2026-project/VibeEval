"""Scoped source adapters. No host filesystem access and no model protocol."""

import fnmatch
from itertools import islice
from typing import Protocol

from app.agent.session_types import StaleStep
from app.workspace.files import ALLOWED_FILES, validate_path

from .materials import SourceSnapshot, digest, encoded, fragment, selection_bounds
from .reading_types import (
    REFERENCE_ADAPTER,
    ListDirectory,
    ReadConversation,
    ReadFile,
    ReadingError,
    ReadSource,
)


class TextSourceProvider(Protocol):
    """Trusted server adapter for question/dependency/output text, scoped by exam.

    Resolve only configured logical IDs; never interpret a model ID as a host
    path or URL. The returned snapshot must be immutable for its version.
    """

    def resolve(self, scope_id: str, source_id: str) -> SourceSnapshot: ...


def safe_relative(value, *, pattern=False):
    if (
        not value
        or "\\" in value
        or ":" in value
        or value.startswith("/")
        or any(part in ("", "..") for part in value.split("/"))
        or (not pattern and any(c in value for c in "*?[]"))
    ):
        raise ReadingError("PATH_INVALID")
    return value


def matches(path, pattern):
    # '**/*' includes root files; fnmatch alone requires a slash in that case.
    return fnmatch.fnmatchcase(path, pattern) or (
        pattern.startswith("**/") and matches(path, pattern[3:])
    )


def public_messages(state):
    """Retained source history, including compacted turns, without vendor data."""
    for turn in state.turns:
        yield {
            "message_id": turn.id + ":user",
            "role": "user",
            "content": turn.user_message,
            "input_context": turn.input_context,
        }
        for step in turn.steps:
            if not step.response:
                continue
            message = step.response.message
            yield {
                "message_id": step.id + ":assistant",
                "role": "assistant",
                "content": message.content,
                "tool_calls": [
                    call.model_dump(mode="json") for call in message.tool_calls
                ],
            }
            for result in step.tool_results:
                yield {
                    "message_id": step.id + ":tool:" + result.tool_call_id,
                    "role": "tool",
                    **result.model_dump(mode="json"),
                }


class SourceReader:
    def __init__(
        self, store, *, workspace=None, text_sources: TextSourceProvider | None = None
    ):
        self.store = store
        self.workspace = workspace
        self.text_sources = text_sources

    def _workspace(self):
        if self.workspace is None:
            raise ReadingError("WORKSPACE_UNAVAILABLE")
        return self.workspace

    def _file(self, scope_id, args):
        validate_path(args.path)
        current = self._workspace().read(scope_id, args.path)
        if (
            args.expected_version is not None
            and args.expected_version != current["version"]
        ):
            raise ReadingError("SOURCE_VERSION_CHANGED")
        return SourceSnapshot(
            "workspace_file", args.path, str(current["version"]), current["content"]
        )

    def _tree(self, scope_id):
        tree = self._workspace().tree(scope_id)["files"]
        if {item["path"] for item in tree} != ALLOWED_FILES:
            raise ReadingError("WORKSPACE_MANIFEST_INVALID")
        return tree

    def _query(self, name, source_id, items, args, metadata=None):
        # Inspect one extra match only, and advertise truncation/pagination.
        page = list(islice(items, args.offset, args.offset + args.limit + 1))
        body = {
            "items": page[: args.limit],
            "offset": args.offset,
            "next_offset": args.offset + len(page[: args.limit]),
            "has_more": len(page) > args.limit,
        }
        text = encoded(body)
        return SourceSnapshot(
            "query",
            source_id,
            digest(text),
            text,
            {"query_kind": name, **(metadata or {})},
        )

    def directory(self, scope_id, args):
        path = safe_relative(args.path)
        root = "" if path == "." else path.rstrip("/") + "/"
        entries = {}
        for item in self._tree(scope_id):
            if not item["path"].startswith(root):
                continue
            suffix = item["path"][len(root) :]
            first = suffix.split("/")[0]
            entries[first] = (
                {"path": root + first, "is_dir": True} if "/" in suffix else item
            )
        if not entries and path != ".":
            raise ReadingError("SOURCE_UNAVAILABLE")
        return self._query(
            "list_directory",
            path,
            iter(sorted(entries.values(), key=lambda i: i["path"])),
            args,
        )

    def glob(self, scope_id, args):
        safe_relative(args.pattern, pattern=True)
        return self._query(
            "glob_files",
            args.pattern,
            (i for i in self._tree(scope_id) if matches(i["path"], args.pattern)),
            args,
        )

    def search(self, scope_id, args):
        safe_relative(args.pattern, pattern=True)
        tree = self._tree(scope_id)
        query = args.query if args.case_sensitive else args.query.casefold()
        versions = {}

        def hits():
            for item in tree:
                if not matches(item["path"], args.pattern):
                    continue
                current = self._workspace().read(scope_id, item["path"])
                versions[item["path"]] = current["version"]
                for number, line in enumerate(current["content"].splitlines(), 1):
                    if query in (line if args.case_sensitive else line.casefold()):
                        yield {
                            "path": item["path"],
                            "version": current["version"],
                            "line": number,
                            "excerpt": line[:300],
                            "excerpt_truncated": len(line) > 300,
                        }

        # Materialize the bounded page before freezing metadata, which records
        # each actually searched version (never claims an atomic repo snapshot).
        snapshot = self._query(
            "search_content",
            "workspace-search",
            hits(),
            args,
            {
                "query": args.query,
                "pattern": args.pattern,
                "case_sensitive": args.case_sensitive,
            },
        )
        return SourceSnapshot(
            snapshot.kind,
            snapshot.source_id,
            snapshot.version,
            snapshot.text,
            {**snapshot.metadata, "observed_versions": versions},
        )

    def conversation(self, scope_id, args):
        state = self.store.conversations.load(scope_id, args.conversation_id)
        if (
            args.expected_revision is not None
            and args.expected_revision != state.revision
        ):
            raise ReadingError("SOURCE_VERSION_CHANGED")
        metadata = {
            "conversation_id": state.id,
            "revision": state.revision,
            "view": args.view,
        }
        if args.view == "summary":
            latest = state.compactions[-1] if state.compactions else None
            body = {
                "summary": latest.summary if latest else None,
                "context_generation": state.context_generation,
                "covered_turns": latest.through_turn_count if latest else 0,
                "covered_steps": latest.through_step_count if latest else 0,
                "through_step_id": latest.through_step_id if latest else None,
                "retained_turn_count": len(state.turns),
            }
        elif args.view == "tool_result":
            try:
                _, step, _ = self.store._call(state, args.step_id, args.tool_call_id)
            except StaleStep:
                raise ReadingError("SOURCE_UNAVAILABLE") from None
            result = next(
                (r for r in step.tool_results if r.tool_call_id == args.tool_call_id),
                None,
            )
            if result is None:
                raise ReadingError("SOURCE_UNAVAILABLE")
            body = result.model_dump(mode="json")
            metadata.update(step_id=args.step_id, tool_call_id=args.tool_call_id)
        else:
            messages = list(
                islice(
                    public_messages(state),
                    args.first_message,
                    args.first_message + args.message_limit + 1,
                )
            )
            body = {"messages": messages[: args.message_limit]}
            metadata.update(
                first_message=args.first_message,
                next_message=args.first_message + len(body["messages"]),
                has_more=len(messages) > args.message_limit,
            )
        return SourceSnapshot(
            "conversation", state.id, str(state.revision), encoded(body), metadata
        )

    def source(self, scope_id, args):
        if self.text_sources is None:
            raise ReadingError("TEXT_SOURCE_UNAVAILABLE")
        snapshot = self.text_sources.resolve(scope_id, args.source_id)
        if snapshot.source_id != args.source_id:
            raise ReadingError("SOURCE_IDENTITY_INVALID")
        if (
            args.expected_version is not None
            and snapshot.version != args.expected_version
        ):
            raise ReadingError("SOURCE_VERSION_CHANGED")
        return snapshot

    def create_reference(self, scope_id, conversation_id, reference):
        reference = REFERENCE_ADAPTER.validate_python(reference, strict=True)
        self.store.conversations.describe(scope_id, conversation_id)
        value = reference.model_dump(mode="json", exclude_none=True)
        snapshot = None
        if reference.kind == "directory":
            path = safe_relative(reference.path)
            if path != "." and not any(p.startswith(path + "/") for p in ALLOWED_FILES):
                raise ReadingError("SOURCE_UNAVAILABLE")
        elif reference.kind == "conversation":
            source = self.store.conversations.load(scope_id, reference.conversation_id)
            if (
                reference.expected_revision is not None
                and source.revision != reference.expected_revision
            ):
                raise ReadingError("SOURCE_VERSION_CHANGED")
        elif reference.kind == "source":
            self.source(
                scope_id,
                ReadSource(
                    source_id=reference.source_id,
                    expected_version=reference.expected_version,
                ),
            )
        elif reference.kind == "material":
            self.store.material(scope_id, reference.material_id)
        elif reference.kind == "selection":
            start, end = selection_bounds(reference.content, reference.selection)
            selected = reference.content[start:end]
            snapshot = SourceSnapshot(
                "user_selection",
                reference.path,
                digest(selected),
                selected,
                {
                    "unsaved": True,
                    "base_version": reference.base_version,
                    "selection": value.get("selection"),
                    "buffer_start_offset": start,
                    "buffer_end_offset": end,
                },
                digest(reference.content),
            )
            value.pop("content")
        # File/directory references do not read a workspace or mark it as read.
        return self.store.create_reference(scope_id, conversation_id, value, snapshot)

    def resolve(self, scope_id, conversation_id, name, args):
        if name == "list_directory":
            snapshot = self.directory(scope_id, args)
        elif name == "glob_files":
            snapshot = self.glob(scope_id, args)
        elif name == "search_content":
            snapshot = self.search(scope_id, args)
        elif name == "read_file":
            snapshot = self._file(scope_id, args)
        elif name == "read_material":
            snapshot = self.store.material(scope_id, args.material_id)
        elif name == "read_conversation":
            snapshot = self.conversation(scope_id, args)
        elif name == "list_conversations":
            body = self.store.conversations.list_conversations(
                scope_id, offset=args.offset, limit=args.limit
            )
            text = encoded(body)
            snapshot = SourceSnapshot(
                "query", "conversations", digest(text), text, {"query_kind": name}
            )
        elif name == "read_source":
            snapshot = self.source(scope_id, args)
        elif name == "read_reference":
            stored = self.store.reference(scope_id, conversation_id, args.reference_id)
            ref = stored["reference"]
            if stored["material_id"]:
                snapshot = self.store.material(scope_id, stored["material_id"])
            elif ref["kind"] == "file":
                target = ReadFile(**{k: v for k, v in ref.items() if k != "kind"})
                snapshot = self._file(scope_id, target)
                # A reference's selection is binding; caller selection cannot
                # turn an attached excerpt into a full-file read implicitly.
                start, end = selection_bounds(
                    snapshot.text,
                    target.selection,
                )
                if ref.get("selection"):
                    snapshot = SourceSnapshot(
                        "file_selection",
                        snapshot.source_id,
                        snapshot.version,
                        snapshot.text[start:end],
                        {
                            "selection": ref["selection"],
                            "file_start_offset": start,
                            "file_end_offset": end,
                        },
                        snapshot.content_digest,
                    )
            elif ref["kind"] == "conversation":
                snapshot = self.conversation(
                    scope_id,
                    ReadConversation(**{k: v for k, v in ref.items() if k != "kind"}),
                )
            elif ref["kind"] == "source":
                snapshot = self.source(
                    scope_id,
                    ReadSource(**{k: v for k, v in ref.items() if k != "kind"}),
                )
            else:
                snapshot = self.directory(scope_id, ListDirectory(path=ref["path"]))
        else:
            raise ReadingError("TOOL_UNAVAILABLE")
        options = (
            {
                "offset": args.offset,
                "max_chars": args.max_chars,
                "selection": args.selection,
            }
            if hasattr(args, "max_chars")
            else {}
        )
        return snapshot, fragment(snapshot, **options)

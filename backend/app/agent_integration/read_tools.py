"""Real read handlers behind the existing Agent core's native tool protocol."""

from app.agent import ToolDefinition, ToolRegistry
from app.db.lifecycle import LifecycleConflict, LifecycleNotFound, SessionBusy
from app.sandbox.runtime import SandboxOperationFailed
from app.workspace.files import InvalidFilePath
from app.workspace.service import RevisionStoreUnavailable

from .reading_types import (
    GlobFiles,
    ListDirectory,
    Page,
    ReadConversation,
    ReadDecision,
    ReadFile,
    ReadingApprovalRequired,
    ReadingError,
    ReadMaterial,
    ReadReference,
    ReadSource,
    SearchContent,
)
from .store import ConversationNotFound

DEFINITIONS = (
    (
        "list_conversations",
        "List authorized conversations in this exam. An index is not their history.",
        Page,
    ),
    (
        "read_conversation",
        "Read retained public messages, latest summary, or one tool result in an authorized conversation. No private reasoning/system configuration. Pin expected_revision for stable paging; use returned material_id to revisit a snapshot.",
        ReadConversation,
    ),
    (
        "read_material",
        "Read an immutable previously captured material in this exam, even after the live source changes. Does not imply that the whole source was read. Offsets count Unicode codepoints; max_chars bounds the returned fragment.",
        ReadMaterial,
    ),
    (
        "read_reference",
        "Resolve one user attachment in the current conversation. Attaching alone does not read it. Selections remain selections; directory references return an index, not file bodies.",
        ReadReference,
    ),
)
WORKSPACE_DEFINITIONS = (
    (
        "list_directory",
        "List immediate entries in the authorized sandbox workspace. An index is not file content. Paths are relative POSIX paths; use '.' for the root.",
        ListDirectory,
    ),
    (
        "glob_files",
        "Find published workspace paths using a glob. Results include versions but do not constitute reading their bodies. No host paths.",
        GlobFiles,
    ),
    (
        "search_content",
        "Search literal text in published workspace files, returning bounded line excerpts, observed versions, and pagination. Search hits are not full-file reads; use read_file to inspect surrounding code.",
        SearchContent,
    ),
    (
        "read_file",
        "Read a bounded fragment of an authorized sandbox file. Optionally pin expected_version; changes return SOURCE_VERSION_CHANGED. Lines are 1-based inclusive; UTF16 editor selections have an exclusive end. Returned offsets count Unicode codepoints; use material_id for stable follow-up reads.",
        ReadFile,
    ),
)


class ReadToolService:
    def __init__(self, reader, *, policy=None):
        self.reader = reader
        self.store = reader.store
        self.policy = policy
        definitions = [*DEFINITIONS]
        if reader.workspace is not None:
            definitions.extend(WORKSPACE_DEFINITIONS)
        if reader.text_sources is not None:
            definitions.append(
                (
                    "read_source",
                    "Read a server-registered question, dependency, or output text by logical source ID. No arbitrary host paths or URLs; optionally pin expected_version.",
                    ReadSource,
                )
            )
        self.registry = ToolRegistry([ToolDefinition(*item) for item in definitions])

    def execute(self, scope_id, conversation_id, step_id, tool_call_id):
        """Dispatch only an actual pending call from persisted core state.

        Returns the core ToolResult; the orchestrator must still use the core's
        supply_tool_results/save before preparing another model request. Ask is
        a pre-I/O port for the later durable approval coordinator.
        """
        call, previous = self.store.pending(
            scope_id, conversation_id, step_id, tool_call_id
        )
        if previous is not None:
            return previous
        args = self.registry.parse_arguments(call)
        decision = (
            ReadDecision(decision="allow")
            if self.policy is None
            else self.policy(scope_id, conversation_id, call)
        )
        decision = ReadDecision.model_validate(decision)
        action = (
            "query"
            if call.name
            in ("list_directory", "glob_files", "search_content", "list_conversations")
            else "read"
        )
        if decision.decision == "ask":
            raise ReadingApprovalRequired(call, decision.reason_code)
        if decision.decision == "deny":
            return self.store.record_read(
                scope_id,
                conversation_id,
                step_id,
                call,
                action_kind=action,
                error_code=decision.reason_code,
                denied=True,
            )
        self.store.check_open(scope_id)
        try:
            snapshot, projection = self.reader.resolve(
                scope_id, conversation_id, call.name, args
            )
        except (
            ReadingError,
            InvalidFilePath,
            ConversationNotFound,
            LifecycleNotFound,
            LifecycleConflict,
            SessionBusy,
            RevisionStoreUnavailable,
            SandboxOperationFailed,
            TimeoutError,
        ) as error:
            if isinstance(error, ReadingError):
                code = error.code
            elif isinstance(error, InvalidFilePath):
                code = "PATH_INVALID"
            elif isinstance(error, (ConversationNotFound, LifecycleNotFound)):
                code = "SOURCE_UNAVAILABLE"
            elif isinstance(error, (LifecycleConflict, SessionBusy)):
                code = "WORKSPACE_NOT_READY"
            else:
                code = "WORKSPACE_UNAVAILABLE"
            return self.store.record_read(
                scope_id,
                conversation_id,
                step_id,
                call,
                action_kind=action,
                error_code=code,
            )
        return self.store.record_read(
            scope_id,
            conversation_id,
            step_id,
            call,
            action_kind="query" if snapshot.kind == "query" else action,
            snapshot=snapshot,
            projection=projection,
        )

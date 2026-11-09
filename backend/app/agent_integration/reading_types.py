"""Application-owned reading schemas; model calls still use app.agent.ToolCall."""

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, model_validator

from app.agent.session_types import FrozenModel, Identifier
from app.workspace.files import validate_path


class LineSelection(FrozenModel):
    kind: Literal["lines"] = "lines"
    start_line: int = Field(strict=True, ge=1)
    end_line: int = Field(strict=True, ge=1)

    @model_validator(mode="after")
    def ordered(self):
        if self.end_line < self.start_line:
            raise ValueError("line range is reversed")
        return self


class Utf16Selection(FrozenModel):
    kind: Literal["utf16"] = "utf16"
    start_line: int = Field(strict=True, ge=1)
    start_column: int = Field(strict=True, ge=1)
    end_line: int = Field(strict=True, ge=1)
    end_column: int = Field(strict=True, ge=1)

    @model_validator(mode="after")
    def ordered(self):
        if (self.end_line, self.end_column) < (self.start_line, self.start_column):
            raise ValueError("selection is reversed")
        return self


Selection = Annotated[LineSelection | Utf16Selection, Field(discriminator="kind")]


class Page(FrozenModel):
    offset: int = Field(default=0, strict=True, ge=0)
    limit: int = Field(default=50, strict=True, ge=1, le=100)


class ListDirectory(Page):
    path: str = Field(default=".", max_length=500)


class GlobFiles(Page):
    pattern: str = Field(min_length=1, max_length=500)


class SearchContent(Page):
    query: str = Field(min_length=1, max_length=200)
    pattern: str = Field(default="**/*", min_length=1, max_length=500)
    case_sensitive: bool = False


class ReadText(FrozenModel):
    offset: int = Field(default=0, strict=True, ge=0)
    max_chars: int = Field(default=12000, strict=True, ge=1, le=16000)
    selection: Selection | None = None


class ReadFile(ReadText):
    path: str = Field(min_length=1, max_length=500)
    expected_version: int | None = Field(default=None, strict=True, ge=1)


class ReadMaterial(ReadText):
    material_id: Identifier


class ReadConversation(ReadText):
    conversation_id: Identifier
    expected_revision: int | None = Field(default=None, strict=True, ge=0)
    view: Literal["messages", "summary", "tool_result"] = "messages"
    first_message: int = Field(default=0, strict=True, ge=0)
    message_limit: int = Field(default=5, strict=True, ge=1, le=5)
    step_id: Identifier | None = None
    tool_call_id: Identifier | None = None

    @model_validator(mode="after")
    def result_target(self):
        if self.view == "tool_result" and not (self.step_id and self.tool_call_id):
            raise ValueError("tool result requires step and call IDs")
        if self.view != "tool_result" and (self.step_id or self.tool_call_id):
            raise ValueError("step/call IDs require tool_result view")
        return self


class ReadSource(ReadText):
    source_id: Identifier
    expected_version: str | None = Field(default=None, min_length=1, max_length=200)


class ReadReference(ReadText):
    reference_id: Identifier


class FileReference(FrozenModel):
    kind: Literal["file"]
    path: str = Field(min_length=1, max_length=500)
    expected_version: int | None = Field(default=None, strict=True, ge=1)
    selection: Selection | None = None

    @model_validator(mode="after")
    def published_path(self):
        validate_path(self.path)
        return self


class DirectoryReference(FrozenModel):
    kind: Literal["directory"]
    path: str = Field(default=".", max_length=500)


class BufferReference(FrozenModel):
    kind: Literal["selection"]
    path: str = Field(min_length=1, max_length=500)
    base_version: int | None = Field(default=None, strict=True, ge=1)
    content: str = Field(max_length=64000)
    selection: Selection | None = None

    @model_validator(mode="after")
    def published_path(self):
        validate_path(self.path)
        return self


class ConversationReference(FrozenModel):
    kind: Literal["conversation"]
    conversation_id: Identifier
    expected_revision: int | None = Field(default=None, strict=True, ge=0)


class MaterialReference(FrozenModel):
    kind: Literal["material"]
    material_id: Identifier


class SourceReference(FrozenModel):
    kind: Literal["source"]
    source_id: Identifier
    expected_version: str | None = Field(default=None, min_length=1, max_length=200)


InputReference = Annotated[
    FileReference
    | DirectoryReference
    | BufferReference
    | ConversationReference
    | MaterialReference
    | SourceReference,
    Field(discriminator="kind"),
]
REFERENCE_ADAPTER = TypeAdapter(InputReference)


class ReadingError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ReadingApprovalRequired(RuntimeError):
    """The external scheduler must obtain approval; no reader was invoked."""

    def __init__(self, call, reason):
        super().__init__(reason)
        self.call = call
        self.reason = reason


class ReadDecision(FrozenModel):
    decision: Literal["allow", "ask", "deny"]
    reason_code: str = Field(default="READ_POLICY", pattern=r"^[A-Z0-9_]{1,80}$")

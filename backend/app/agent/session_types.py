"""Python session contracts adapted from Codex/Claude message and state structures.

See UPSTREAM.md for source versions, adaptation boundaries and license notices.
No upstream CLI or SDK is invoked by these types.
"""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(pattern=r"^\S{1,128}$")]
Status = Literal[
    "ready", "awaiting_model", "awaiting_tools", "completed", "failed", "interrupted"
]
TERMINAL = frozenset(("completed", "failed", "interrupted"))


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SessionError(ValueError):
    pass


class SessionBusy(SessionError):
    pass


class SessionConflict(SessionError):
    pass


class StaleStep(SessionError):
    pass


class TokenUsage(FrozenModel):
    prompt_tokens: int | None = Field(default=None, strict=True, ge=0)
    completion_tokens: int | None = Field(default=None, strict=True, ge=0)
    total_tokens: int | None = Field(default=None, strict=True, ge=0)


def _reject_constant(value):
    raise ValueError("nonstandard JSON constant")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON argument key")
        result[key] = value
    return result


class ToolCall(FrozenModel):
    id: Identifier
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    # Keep the exact argument string so a later API adapter can replay it.
    arguments: str = Field(max_length=16000)

    @field_validator("arguments")
    @classmethod
    def object_arguments(cls, value):
        try:
            parsed = json.loads(
                value, parse_constant=_reject_constant, object_pairs_hook=_unique_object
            )
        except (ValueError, RecursionError):
            raise ValueError("tool arguments must be a JSON object") from None
        if not isinstance(parsed, dict):
            raise ValueError("tool arguments must be a JSON object")
        return value


class ToolResult(FrozenModel):
    tool_call_id: Identifier
    content: str = Field(max_length=64000)
    is_error: bool = False


class AssistantOutput(FrozenModel):
    content: str | None = Field(default=None, max_length=64000)
    tool_calls: tuple[ToolCall, ...] = Field(default=(), max_length=32)
    # Adapter-owned JSON; never interpreted as public reasoning or UI text.
    continuation_json: str | None = None

    @model_validator(mode="after")
    def unique_calls(self):
        ids = [call.id for call in self.tool_calls]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate tool call IDs in an assistant output")
        if self.continuation_json is not None:
            try:
                if not isinstance(
                    json.loads(self.continuation_json, parse_constant=_reject_constant),
                    dict,
                ):
                    raise ValueError
            except (ValueError, RecursionError):
                raise ValueError(
                    "continuation metadata must be a JSON object"
                ) from None
        return self


class ModelResponse(FrozenModel):
    model: str = Field(min_length=1, max_length=200)
    message: AssistantOutput
    finish_reason: Literal["stop", "tool_calls", "length", "content_filter"]
    usage: TokenUsage | None = None
    response_id: str | None = None

    @model_validator(mode="after")
    def usable_output(self):
        if self.finish_reason == "tool_calls" and not self.message.tool_calls:
            raise ValueError("tool_calls finish reason requires tool calls")
        if self.finish_reason == "stop" and not (
            self.message.tool_calls or (self.message.content or "").strip()
        ):
            raise ValueError("a final response requires text or tool calls")
        return self


class ModelStep(FrozenModel):
    id: Identifier
    status: Literal[
        "awaiting_model", "awaiting_tools", "completed", "failed", "interrupted"
    ]
    response: ModelResponse | None = None
    tool_results: tuple[ToolResult, ...] = ()
    error_code: str | None = None

    @model_validator(mode="after")
    def paired_results(self):
        calls = self.response.message.tool_calls if self.response else ()
        ids = {call.id for call in calls}
        results = [result.tool_call_id for result in self.tool_results]
        if len(results) != len(set(results)) or not set(results) <= ids:
            raise ValueError("tool results must match unique calls in this step")
        if self.status == "awaiting_model" and (
            self.response or self.tool_results or self.error_code
        ):
            raise ValueError("an awaiting model step cannot already contain a result")
        if self.status == "awaiting_tools" and (
            not calls
            or set(results) == ids
            or self.error_code
            or self.response.finish_reason in ("length", "content_filter")
        ):
            raise ValueError("an awaiting tools step needs unresolved tool calls")
        if self.status == "completed" and (
            self.response is None
            or set(results) != ids
            or self.error_code
            or self.response.finish_reason in ("length", "content_filter")
        ):
            raise ValueError(
                "a completed step must have a complete response and tool results"
            )
        if self.status == "failed" and not self.error_code:
            raise ValueError("a failed step requires an error code")
        return self


class TurnState(FrozenModel):
    id: Identifier
    request_key: Identifier
    user_message: str = Field(min_length=1, max_length=16000)
    status: Status = "ready"
    steps: tuple[ModelStep, ...] = ()

    @field_validator("user_message")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("user message cannot be blank")
        return value

    @model_validator(mode="after")
    def coherent_steps(self):
        if any(step.status != "completed" for step in self.steps[:-1]):
            raise ValueError("only the last model step can be unfinished")
        last = self.steps[-1] if self.steps else None
        if (
            self.status == "ready"
            and last is not None
            and (last.status != "completed" or not last.response.message.tool_calls)
        ):
            raise ValueError("a continued turn requires a completed tool exchange")
        if self.status not in ("ready", "interrupted") and (
            last is None or last.status != self.status
        ):
            raise ValueError("turn status does not match its current model step")
        if self.status == "completed" and last.response.message.tool_calls:
            raise ValueError("a tool request does not complete a user turn")
        if (
            self.status == "interrupted"
            and last
            and last.status not in ("completed", "interrupted")
        ):
            raise ValueError("interrupted turn has an active model step")
        return self


class SessionConfig(FrozenModel):
    model: str | None = None
    system_prompt: str = ""
    prompt_version: str = "1"
    tools_version: str = "1"


def turns_digest(turns: tuple[TurnState, ...]) -> str:
    encoded = json.dumps(
        [turn.model_dump(mode="json") for turn in turns],
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def sum_usage(items: list[TokenUsage | None]) -> TokenUsage:
    counts = {}
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        values = [getattr(item, field) if item else None for item in items]
        counts[field] = (
            sum(values)
            if values and all(value is not None for value in values)
            else None
        )
    return TokenUsage(**counts)


class ContextCheckpoint(FrozenModel):
    id: Identifier
    generation: int = Field(strict=True, ge=1)
    parent_id: Identifier | None = None
    source_revision: int = Field(strict=True, ge=0)
    source_state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    through_turn_count: int = Field(strict=True, ge=1)
    through_turn_id: Identifier
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary: str = Field(min_length=1, max_length=16000)
    model: str = Field(min_length=1, max_length=200)
    prompt_version: Literal["1"] = "1"
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_id: str | None = None
    usage: TokenUsage | None = None

    @field_validator("summary")
    @classmethod
    def nonblank_summary(cls, value):
        if not value.strip():
            raise ValueError("summary cannot be blank")
        return value


class ConversationState(FrozenModel):
    schema_version: Literal[1] = 1
    id: Identifier
    scope_id: Identifier
    revision: int = Field(default=0, strict=True, ge=0)
    config: SessionConfig = Field(default_factory=SessionConfig)
    turns: tuple[TurnState, ...] = ()
    compactions: tuple[ContextCheckpoint, ...] = ()

    @model_validator(mode="after")
    def coherent_turns(self):
        ids = [turn.id for turn in self.turns]
        keys = [turn.request_key for turn in self.turns]
        steps = [step.id for turn in self.turns for step in turn.steps]
        if (
            len(ids) != len(set(ids))
            or len(keys) != len(set(keys))
            or len(steps) != len(set(steps))
        ):
            raise ValueError("turn IDs, request keys and step IDs must be unique")
        if any(turn.status not in TERMINAL for turn in self.turns[:-1]):
            raise ValueError("only the last turn may be active")
        covered, parent, seen, last_source_revision = 0, None, set(), -1
        for generation, checkpoint in enumerate(self.compactions, 1):
            count = checkpoint.through_turn_count
            if (
                checkpoint.id in seen
                or checkpoint.generation != generation
                or checkpoint.parent_id != parent
            ):
                raise ValueError("invalid compaction checkpoint chain")
            if (
                not covered <= count <= len(self.turns)
                or not last_source_revision < checkpoint.source_revision < self.revision
            ):
                raise ValueError("invalid compaction boundary or source revision")
            prefix = self.turns[:count]
            if any(turn.status not in TERMINAL for turn in prefix):
                raise ValueError("compaction cannot cover an unfinished turn")
            if (
                prefix[-1].id != checkpoint.through_turn_id
                or turns_digest(prefix) != checkpoint.source_digest
            ):
                raise ValueError(
                    "compaction source history does not match its checkpoint"
                )
            covered, parent = count, checkpoint.id
            last_source_revision = checkpoint.source_revision
            seen.add(checkpoint.id)
        return self

    @property
    def active_turn(self) -> TurnState | None:
        if self.turns and self.turns[-1].status not in TERMINAL:
            return self.turns[-1]
        return None

    @property
    def usage(self) -> TokenUsage:
        # Derive totals from accepted responses, so replaying a response cannot
        # double count it. A missing counter stays unknown rather than becoming 0.
        items = [
            step.response.usage
            for turn in self.turns
            for step in turn.steps
            if step.response
        ]
        return sum_usage(items + [checkpoint.usage for checkpoint in self.compactions])

    @property
    def compaction_usage(self) -> TokenUsage:
        return sum_usage([checkpoint.usage for checkpoint in self.compactions])

    @property
    def context_generation(self) -> int:
        return len(self.compactions)

"""Inspectable native Chat Completions requests and typed response parsing.

Named-call parsing follows the upstream patterns documented in UPSTREAM.md.
The API is called once; callers supply tool results and advance session state.
"""

import hashlib
import json
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.llm.client import LLMClient
from app.llm.config import LLMError, LLMSettings, load_settings

from .context import render_context
from .prompts import GLOBAL_AGENT_PROMPT, PROMPT_VERSION
from .session_types import (
    AssistantOutput,
    ConversationState,
    FrozenModel,
    ModelResponse,
    SessionBusy,
    SessionError,
    TokenUsage,
    ToolCall,
)
from .tools import ToolRegistry


class FunctionCall(FrozenModel):
    name: str
    arguments: str


class NativeCall(FrozenModel):
    id: str
    type: Literal["function"]
    function: FunctionCall

    def as_call(self) -> ToolCall:
        return ToolCall(
            id=self.id, name=self.function.name, arguments=self.function.arguments
        )


class NativeMessage(FrozenModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: tuple[NativeCall, ...] | None = None
    tool_call_id: str | None = None
    reasoning_details: list[dict[str, Any]] | None = None

    @model_validator(mode="after")
    def valid_role(self):
        calls = self.tool_calls or ()
        if self.role == "tool":
            if (
                not self.tool_call_id
                or self.content is None
                or calls
                or self.reasoning_details
            ):
                raise ValueError("invalid tool result message")
        elif self.role == "assistant":
            if self.tool_call_id or not (calls or (self.content or "").strip()):
                raise ValueError("invalid assistant message")
        elif (
            not (self.content or "").strip()
            or calls
            or self.tool_call_id
            or self.reasoning_details
        ):
            raise ValueError("invalid input message")
        for call in calls:
            call.as_call()
        return self


class NativeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=200)
    messages: tuple[NativeMessage, ...] = Field(min_length=1, max_length=256)
    max_tokens: int = Field(strict=True, ge=1, le=4096)
    stream: Literal[False] = False
    temperature: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)
    tools: list[dict[str, Any]] | None = None
    tool_choice: Literal["auto"] | None = None

    @model_validator(mode="after")
    def paired_history(self):
        pending = set()
        for message in self.messages:
            if pending and message.role != "tool":
                raise ValueError(
                    "all pending tools must have results before continuing"
                )
            if message.role == "tool":
                if message.tool_call_id not in pending:
                    raise ValueError("orphan or duplicate tool result")
                pending.remove(message.tool_call_id)
            elif message.tool_calls:
                ids = [call.id for call in message.tool_calls]
                if len(ids) != len(set(ids)):
                    raise ValueError("duplicate tool call ID")
                pending.update(ids)
        if pending:
            raise ValueError("unresolved tool calls in request")
        if self.tool_choice and not self.tools:
            raise ValueError("tool choice requires tool definitions")
        return self


class PreparedModelRequest(FrozenModel):
    conversation_id: str
    scope_id: str
    turn_id: str
    step_id: str
    revision: int = Field(strict=True, ge=0)
    prompt_version: str
    tools_version: str
    tools_fingerprint: str
    context_generation: int = Field(default=0, strict=True, ge=0)
    context_through_turns: int = Field(default=0, strict=True, ge=0)
    payload_json: str = Field(max_length=64000)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def valid_payload(self):
        if (
            hashlib.sha256(self.payload_json.encode("utf-8")).hexdigest()
            != self.payload_sha256
        ):
            raise ValueError("prepared request digest mismatch")
        NativeRequest.model_validate_json(self.payload_json)
        return self

    @property
    def payload(self) -> dict:
        # A fresh dict lets callers inspect it without changing the saved request.
        return json.loads(self.payload_json)


class CompletionClient(Protocol):
    async def complete(self, payload: dict) -> dict: ...


def prepare_model_request(
    state: ConversationState,
    *,
    tools: ToolRegistry | None = None,
    settings: LLMSettings | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> PreparedModelRequest:
    """Prepare one already-started step; this function never calls the model."""
    turn = state.active_turn
    if turn is None or turn.status != "awaiting_model":
        raise SessionBusy("start a model step before preparing its request")
    registry = tools if tools is not None else ToolRegistry()
    if (
        state.config.prompt_version != PROMPT_VERSION
        or state.config.tools_version != registry.version
    ):
        raise SessionError(
            "prompt or tool schema version does not match this conversation"
        )
    config = settings if settings is not None else load_settings()
    model = state.config.model or config.model
    if not model:
        raise LLMError(503, "尚未配置模型，请设置 LLM_MODEL 或会话模型")
    limit = config.max_output_tokens if max_tokens is None else max_tokens
    if type(limit) is not int or not 1 <= limit <= config.max_output_tokens:
        raise LLMError(422, "max_tokens 超过后端配置的输出上限或无效")
    messages = [{"role": "system", "content": GLOBAL_AGENT_PROMPT}]
    if state.config.system_prompt:
        messages.append({"role": "system", "content": state.config.system_prompt})
    for item in render_context(state, include_continuation=True):
        metadata = item.pop("provider_metadata", {})
        if set(metadata) - {"reasoning_details"}:
            raise SessionError("unsupported provider continuation fields")
        item.update(metadata)
        messages.append(item)
    specifications = registry.specifications()
    native = NativeRequest(
        model=model,
        messages=tuple(messages),
        max_tokens=limit,
        temperature=temperature,
        tools=specifications or None,
        tool_choice="auto" if specifications else None,
    )
    payload = native.model_dump(mode="json", exclude_none=True)
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return PreparedModelRequest(
        conversation_id=state.id,
        scope_id=state.scope_id,
        turn_id=turn.id,
        step_id=turn.steps[-1].id,
        revision=state.revision,
        prompt_version=PROMPT_VERSION,
        tools_version=registry.version,
        tools_fingerprint=registry.fingerprint,
        context_generation=state.context_generation,
        context_through_turns=(
            state.compactions[-1].through_turn_count if state.compactions else 0
        ),
        payload_json=encoded,
        payload_sha256=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    )


def parse_model_response(
    payload: dict, *, tools: ToolRegistry, model: str
) -> ModelResponse:
    """Filter vendor fields, parse calls, and validate their declared arguments."""
    try:
        choice = payload["choices"][0]
        message = choice["message"]
        if message.get("role", "assistant") != "assistant":
            raise ValueError
        finish_reason = choice["finish_reason"]
        calls = []
        # Truncated/filtered output may contain unfinished argument JSON or even
        # an unfinished call header. Preserve its reason/usage, never advertise
        # it as a valid external request or replay its partial tool chain.
        raw_calls = (
            []
            if finish_reason in ("length", "content_filter")
            else message.get("tool_calls")
        )
        if raw_calls is None:
            raw_calls = []
        if not isinstance(raw_calls, list):
            raise ValueError
        for raw in raw_calls:
            call = NativeCall(
                id=raw["id"],
                type=raw["type"],
                function=FunctionCall(
                    name=raw["function"]["name"],
                    arguments=raw["function"]["arguments"],
                ),
            ).as_call()
            tools.parse_arguments(call)
            calls.append(call)
        metadata = None
        if message.get("reasoning_details") is not None:
            details = message["reasoning_details"]
            if not isinstance(details, list) or not all(
                isinstance(item, dict) for item in details
            ):
                raise ValueError
            metadata = json.dumps(
                {"reasoning_details": details}, ensure_ascii=False, allow_nan=False
            )
        usage = payload.get("usage")
        if usage is not None:
            usage = TokenUsage(
                **{
                    key: usage.get(key)
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                }
            )
        return ModelResponse(
            model=payload.get("model") or model,
            response_id=payload.get("id"),
            message=AssistantOutput(
                content=message.get("content"),
                tool_calls=tuple(calls),
                continuation_json=metadata,
            ),
            finish_reason=finish_reason,
            usage=usage,
        )
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise LLMError(502, "模型返回的消息或工具调用格式无效") from None


async def call_model(
    prepared: PreparedModelRequest,
    *,
    tools: ToolRegistry | None = None,
    client: CompletionClient | None = None,
) -> ModelResponse:
    registry = tools if tools is not None else ToolRegistry()
    # Revalidate persisted input and ensure it is parsed with the same tool plan.
    checked = PreparedModelRequest.model_validate_json(prepared.model_dump_json())
    payload = checked.payload
    if checked.prompt_version != PROMPT_VERSION or payload["messages"][0] != {
        "role": "system",
        "content": GLOBAL_AGENT_PROMPT,
    }:
        raise SessionError("prepared request does not use the expected global prompt")
    if (
        checked.tools_version != registry.version
        or checked.tools_fingerprint != registry.fingerprint
        or payload.get("tools", []) != registry.specifications()
    ):
        raise SessionError("tool plan changed after preparing the request")
    provider = client if client is not None else LLMClient(load_settings())
    response = await provider.complete(payload)
    return parse_model_response(response, tools=registry, model=payload["model"])

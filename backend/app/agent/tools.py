"""Declared tool surfaces and argument validation, without handlers or execution.

Adapted from Codex's advertised tool plan / named-call dispatch split. See UPSTREAM.md.
"""

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from app.llm.config import LLMError

from .session_types import ToolCall


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_model: type[BaseModel]
    _schema_json: str = field(init=False, repr=False)

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.name):
            raise ValueError("invalid tool name")
        if not self.description.strip() or len(self.description) > 4000:
            raise ValueError("tool description must contain 1 to 4000 characters")
        if not isinstance(self.input_model, type) or not issubclass(
            self.input_model, BaseModel
        ):
            raise ValueError("tool arguments require a Pydantic object model")
        if self.input_model.model_config.get("extra") != "forbid":
            raise ValueError("tool input models must forbid unknown fields")
        schema = self.input_model.model_json_schema()
        if schema.get("type") != "object":
            raise ValueError("tool argument schema must describe an object")
        object.__setattr__(
            self, "_schema_json", json.dumps(schema, sort_keys=True, allow_nan=False)
        )

    def specification(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": json.loads(self._schema_json),
            },
        }

    def parse_arguments(self, call: ToolCall) -> BaseModel:
        if call.name != self.name:
            raise LLMError(502, "模型返回了未声明的工具调用")
        try:
            return self.input_model.model_validate_json(call.arguments, strict=True)
        except ValidationError:
            raise LLMError(502, "模型返回的工具参数不符合声明的 schema") from None


class ToolRegistry:
    def __init__(self, tools: Sequence[ToolDefinition] = (), *, version: str = "1"):
        self._tools = tuple(tools)
        self._version = version
        if not version.strip() or len(version) > 128:
            raise ValueError("invalid tool schema version")
        if len(self._tools) > 32 or len({tool.name for tool in self._tools}) != len(
            self._tools
        ):
            raise ValueError("at most 32 tools with unique names are supported")

    @property
    def version(self) -> str:
        return self._version

    def specifications(self) -> list[dict]:
        return [tool.specification() for tool in self._tools]

    @property
    def fingerprint(self) -> str:
        value = json.dumps(self.specifications(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def parse_arguments(self, call: ToolCall) -> BaseModel:
        for tool in self._tools:
            if tool.name == call.name:
                return tool.parse_arguments(call)
        raise LLMError(502, "模型返回了未声明的工具调用")

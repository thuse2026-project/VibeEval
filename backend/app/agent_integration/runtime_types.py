"""Trusted run policy and browser intents; no browser model/tool receipts."""

from typing import Annotated, Literal

from pydantic import Field

from app.agent.session_types import FrozenModel, Identifier


class RunInput(FrozenModel):
    request_key: Identifier
    text: str = Field(min_length=1, max_length=12000, pattern=r"\S")
    reference_ids: tuple[Identifier, ...] = Field(default=(), max_length=20)
    mode: Literal["chat_only", "read_only", "confirm_actions", "sandbox_auto"] = (
        "read_only"
    )


class CompactInput(FrozenModel):
    request_key: Identifier


class RunPolicy(FrozenModel):
    version: str = "1"
    mode: Literal["chat_only", "read_only", "confirm_actions", "sandbox_auto"] = (
        "read_only"
    )
    read_decision: Literal["allow", "ask", "deny"] = "allow"
    action_decision: Literal["allow", "ask", "deny"] = "ask"
    max_operations: int = Field(default=16, strict=True, ge=1, le=32)
    max_command_seconds: int = Field(default=120, strict=True, ge=1, le=120)
    max_model_calls: int = Field(default=8, strict=True, ge=1, le=32)
    max_compactions: int = Field(default=2, strict=True, ge=1, le=8)
    max_tool_calls: int = Field(default=32, strict=True, ge=1, le=128)
    max_total_tokens: int = Field(default=64000, strict=True, ge=1)
    max_run_seconds: int = Field(default=900, strict=True, ge=30, le=3600)
    lease_seconds: int = Field(default=180, strict=True, ge=150, le=600)
    interaction_seconds: int = Field(default=300, strict=True, ge=1, le=900)


class AskUser(FrozenModel):
    question: str = Field(min_length=1, max_length=1000, pattern=r"^[^\x00]*$")
    options: tuple[
        Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[^\x00]*$")], ...
    ] = Field(default=(), max_length=5)


class CompactContext(FrozenModel):
    reason: str = Field(default="", max_length=200)


class AnswerInput(FrozenModel):
    answer: str = Field(min_length=1, max_length=4000)


class ApprovalInput(FrozenModel):
    decision: Literal["approve", "reject"]


class RunBusy(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


class RunBudgetExceeded(RuntimeError):
    pass

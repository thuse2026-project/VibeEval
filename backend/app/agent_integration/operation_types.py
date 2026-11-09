"""Native action schemas and bounded, platform-owned execution receipts."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.agent.session_types import FrozenModel, Identifier
from app.workspace.files import validate_path


class RunCommand(FrozenModel):
    argv: tuple[Annotated[str, Field(max_length=12000)], ...] = Field(
        min_length=1, max_length=64
    )
    cwd: Literal[".", "src", "tests"] = "."
    timeout_seconds: int = Field(default=30, strict=True, ge=1, le=120)
    max_output_bytes: int = Field(default=32768, strict=True, ge=1024, le=32768)
    expected_versions: dict[str, Annotated[int, Field(strict=True, ge=1)]] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def published_versions(self):
        if not self.argv[0]:
            raise ValueError("command executable is empty")
        if any("\x00" in value for value in self.argv):
            raise ValueError("NUL is not a command argument")
        for path in self.expected_versions:
            validate_path(path)
        return self


class ReadExecution(FrozenModel):
    execution_id: Identifier
    stream: Literal["stdout", "stderr"] = "stdout"
    offset: int = Field(default=0, strict=True, ge=0)
    max_chars: int = Field(default=8000, strict=True, ge=1, le=16000)
    expected_revision: int | None = Field(default=None, strict=True, ge=0)


class ExecutionSnapshot(FrozenModel):
    version: Literal[1] = 1
    operation_id: str | None = None
    request_sha256: str | None = None
    status: Literal[
        "running",
        "succeeded",
        "failed",
        "conflict",
        "timed_out",
        "cancelled",
        "unknown",
    ]
    processes_stopped: bool
    reason_code: str | None = Field(default=None, max_length=80)
    started_at: float | None = None
    command_started_at: float | None = None
    finished_at: float | None = None
    exit_code: int | None = None
    stdout_b64: str = Field(default="", max_length=44000)
    stderr_b64: str = Field(default="", max_length=44000)
    stdout_bytes: int = Field(default=0, strict=True, ge=0)
    stderr_bytes: int = Field(default=0, strict=True, ge=0)
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    output_complete: bool = False
    pause_owned: bool = False
    file_change: dict | None = None


class OperationWaiting(RuntimeError):
    pass

"""Trusted published task manifests and stage-local sandbox bindings."""

from dataclasses import dataclass
from typing import Annotated

from pydantic import Field, TypeAdapter, model_validator

from app.agent_integration.operation_types import RunCommand
from app.agent_integration.reading_types import (
    BufferReference,
    ConversationReference,
    DirectoryReference,
    FileReference,
    MaterialReference,
    SourceReference,
)
from app.agent_integration.runtime_types import RunPolicy
from app.db.lifecycle import SessionView
from app.exam_flow.types import Contract, Identifier


def safe_path(path):
    return (
        isinstance(path, str)
        and 1 <= len(path) <= 500
        and not any(c in path for c in ("\\", ":"))
        and not any(ord(c) < 32 or ord(c) == 127 for c in path)
        and all(p not in ("", ".", "..") for p in path.split("/"))
    )


class StageRunCommand(RunCommand):
    cwd: str = Field(default=".", min_length=1, max_length=500)

    @model_validator(mode="after")
    def published_versions(self):
        if not self.argv[0] or any("\x00" in value for value in self.argv):
            raise ValueError("invalid command arguments")
        if self.cwd != "." and not safe_path(self.cwd):
            raise ValueError("unsafe working directory")
        if not all(safe_path(path) for path in self.expected_versions):
            raise ValueError("unsafe version path")
        return self


class StageFileReference(FileReference):
    @model_validator(mode="after")
    def published_path(self):
        if not safe_path(self.path):
            raise ValueError("unsafe reference path")
        return self


class StageBufferReference(BufferReference):
    @model_validator(mode="after")
    def published_path(self):
        if not safe_path(self.path):
            raise ValueError("unsafe reference path")
        return self


StageInputReference = Annotated[
    StageFileReference
    | DirectoryReference
    | StageBufferReference
    | ConversationReference
    | MaterialReference
    | SourceReference,
    Field(discriminator="kind"),
]
STAGE_REFERENCE_ADAPTER = TypeAdapter(StageInputReference)


class ResourceSpec(Contract):
    task_version: Identifier
    image_ref: str = Field(pattern=r"^(?:[^\s]+@)?sha256:[0-9a-f]{64}$")
    files: tuple[str, ...] = Field(min_length=1, max_length=200)
    writable: tuple[str, ...] = ()
    agent_policy: RunPolicy = Field(
        default_factory=lambda: RunPolicy(mode="confirm_actions")
    )

    @model_validator(mode="after")
    def manifest(self):
        if (
            len(set(self.files)) != len(self.files)
            or len(set(self.writable)) != len(self.writable)
            or not set(self.writable) <= set(self.files)
            or not all(safe_path(p) for p in self.files)
        ):
            raise ValueError("invalid published manifest")
        if any(p.endswith(".vibeeval-save") for p in self.files):
            raise ValueError("staging names are reserved")
        return self


@dataclass(frozen=True)
class WorkspaceView(SessionView):
    allowed_files: tuple[str, ...] = ()
    writable_files: tuple[str, ...] = ()
    read_only: bool = False


def binding_view(value):
    return WorkspaceView(**value) if "allowed_files" in value else SessionView(**value)


def validate_file(view, path, *, write=False):
    from app.workspace.files import InvalidFilePath, ReadOnlyFile

    if not safe_path(path) or path not in view.allowed_files:
        raise InvalidFilePath("file is not in the published task")
    if write and (view.read_only or path not in view.writable_files):
        raise ReadOnlyFile("published file is read-only")
    return path

"""Versioned single-file proposals; exact replacements never touch the filesystem."""

import difflib
from typing import Literal

from pydantic import Field, model_validator

from app.agent.session_types import FrozenModel, ToolResult
from app.workspace.files import encode_content

from .materials import SourceSnapshot, digest, encoded, validate_json_text
from .reading_types import ReadingError

WritablePath = Literal["src/booking.py", "tests/test_candidate.py"]


class WriteFile(FrozenModel):
    path: WritablePath
    expected_version: int = Field(strict=True, ge=1)
    content: str = Field(max_length=12000)

    @model_validator(mode="after")
    def valid_text(self):
        validate_json_text(self.content)
        encode_content(self.content)
        return self


class Replacement(FrozenModel):
    old_text: str = Field(min_length=1, max_length=12000)
    new_text: str = Field(max_length=12000)

    @model_validator(mode="after")
    def valid_text(self):
        validate_json_text((self.old_text, self.new_text))
        return self


class ApplyPatch(FrozenModel):
    path: WritablePath
    expected_version: int = Field(strict=True, ge=1)
    edits: tuple[Replacement, ...] = Field(min_length=1, max_length=20)


class StageWriteFile(WriteFile):
    path: str = Field(min_length=1, max_length=500)


class StageApplyPatch(ApplyPatch):
    path: str = Field(min_length=1, max_length=500)


def proposed_text(before, args):
    validate_json_text(before)
    if isinstance(args, WriteFile):
        after = args.content
    else:
        after = before
        for edit in args.edits:
            index = after.find(edit.old_text)
            if index < 0:
                raise ReadingError("PATCH_CONTEXT_NOT_FOUND")
            if after.find(edit.old_text, index + 1) >= 0:
                raise ReadingError("PATCH_CONTEXT_AMBIGUOUS")
            after = after[:index] + edit.new_text + after[index + len(edit.old_text) :]
    try:
        encode_content(after)
    except ValueError:
        raise ReadingError("FILE_TOO_LARGE") from None
    return after


def unified_diff(before, after, path):
    parts = []
    for line in difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile="a/" + path,
        tofile="b/" + path,
    ):
        parts.append(line)
        if not line.endswith(("\n", "\r")):
            parts.append("\n\\ No newline at end of file\n")
    return "".join(parts)


def prepare_change(materials, db, scope, operation_id, before, args):
    after = proposed_text(before, args)
    diff = unified_diff(before, after, args.path)
    snapshots = {
        "before": SourceSnapshot(
            "workspace_file", args.path, str(args.expected_version), before
        ),
        "after": SourceSnapshot(
            "proposed_file",
            operation_id + ":after",
            "1",
            after,
            {"path": args.path, "proposal": True},
        ),
        "diff": SourceSnapshot(
            "file_diff",
            operation_id + ":diff",
            "1",
            diff,
            {"path": args.path, "proposal": True},
        ),
    }
    return {
        "path": args.path,
        "base_version": args.expected_version,
        "before_sha256": digest(before),
        "after_sha256": digest(after),
        "changed": before != after,
        "before_bytes": len(before.encode("utf-8")),
        "after_bytes": len(after.encode("utf-8")),
        **{
            part + "_material_id": materials._put(db, scope, snapshot)
            for part, snapshot in snapshots.items()
        },
    }


def finish_change(service, db, conv, run, row, snapshot):
    """Adopt a trusted stop receipt; current observation is separate from intent."""
    plan = row.binding_json["change"]
    expected = {
        key: plan[key] for key in ("path", "before_sha256", "after_sha256", "changed")
    }
    applied = None
    if snapshot.status == "succeeded":
        if snapshot.file_change != expected:
            raise ReadingError("EXECUTION_RECEIPT_INVALID")
        applied = True
    elif snapshot.reason_code in (
        "SOURCE_VERSION_CHANGED",
        "CANCELLED_BEFORE_REPLACE",
        "CANCELLED_BEFORE_START",
        "EXAM_DEADLINE",
    ):
        applied = False
    current_version, observed_id, current_matches, resumed = None, None, None, False
    view = service.target(db, row.exam_session_id)
    if (
        view.status == "in_progress"
        and view.deadline_at is not None
        and view.deadline_at > int(service.runs.clock())
        and view.sandbox_id == row.binding_json["sandbox"]["sandbox_id"]
    ):
        # These fixed lifecycle calls run under the exam lock so submission
        # cannot begin between the status check and unpausing the original.
        text = service.backend.read_file(view, plan["path"])
        validate_json_text(text)
        actual_digest = digest(text)
        from .scopes import revision_row

        revision = revision_row(db, row.exam_session_id, plan["path"])
        if revision is None:
            raise ReadingError("FILE_REVISIONS_UNAVAILABLE")
        if revision.content_sha256 != actual_digest:
            revision.version += 1
            revision.content_sha256 = actual_digest
        current_version = revision.version
        current_matches = actual_digest == plan["after_sha256"]
        observed_id = service.materials._put(
            db,
            row.exam_session_id,
            SourceSnapshot("workspace_file", plan["path"], str(current_version), text),
        )
        if snapshot.pause_owned:
            service.backend.resume_workspace(view)
            resumed = True
    diff = service.materials._material(
        db, row.exam_session_id, plan["diff_material_id"]
    )
    limit = 2000
    while True:
        content = {
            "operation_id": row.operation_id,
            "execution_id": row.operation_id,
            "status": row.status,
            "code": row.reason_code,
            "processes_stopped": True,
            "workspace_resumed": resumed,
            "change": {
                **plan,
                "applied": applied,
                "current_version": current_version,
                "current_matches_proposal": current_matches,
                "observed_material_id": observed_id,
                "diff_preview": diff.text[:limit],
                "diff_preview_has_more": len(diff.text) > limit,
            },
        }
        if len(encoded(content)) <= 16000 or limit == 0:
            break
        limit //= 2
    result = ToolResult(
        tool_call_id=row.tool_call_id,
        content=encoded(content),
        is_error=row.status != "succeeded",
    )
    row.result_json, row.holds_workspace = result.model_dump(mode="json"), False
    service.event(
        db,
        conv,
        run,
        row,
        "operation." + row.status,
        applied=applied,
        current_version=current_version,
        current_matches_proposal=current_matches,
        workspace_resumed=resumed,
    )
    return result

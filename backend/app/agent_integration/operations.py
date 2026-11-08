"""Durable command intent, approval binding and non-replayable sandbox dispatch."""

import base64
import hashlib
import json
import uuid
from contextlib import contextmanager
from dataclasses import asdict

from sqlalchemy import select

from app.agent.session_types import SessionConflict, ToolResult
from app.db.lifecycle import (
    LifecycleConflict,
    LifecycleNotFound,
    SessionBusy,
    SessionView,
)
from app.db.models import (
    AgentInteraction,
    AgentOperation,
    ExamSession,
    WorkspaceFileRevision,
)
from app.db.session import session_scope
from app.sandbox.runtime import SandboxOperationFailed
from app.workspace.files import ALLOWED_FILES
from app.workspace.service import RevisionStoreUnavailable

from .file_changes import ApplyPatch, WriteFile, finish_change, prepare_change
from .material_store import call_digest
from .materials import SourceSnapshot, digest, encoded, fragment
from .operation_types import ExecutionSnapshot, OperationWaiting
from .reading_types import ReadingError
from .runtime_types import RunBudgetExceeded
from .store import ConversationNotFound

BACKEND_ERRORS = (TimeoutError, SandboxOperationFailed)


def output_bytes(receipt, stream):
    return base64.b64decode((receipt or {}).get(stream + "_b64", ""), validate=True)


def output_text(data):
    return data.decode("utf-8", errors="replace").replace("\x00", "\\0")


class OperationService:
    def __init__(self, reader, runs, backend=None):
        self.reader, self.runs, self.materials = reader, runs, reader.store
        self.backend = backend

    @contextmanager
    def locked(self, scope, cid, operation_id, *, token=None, open_required=False):
        with session_scope(self.runs.factory) as db:
            run_id = db.scalar(
                select(AgentOperation.run_id).where(
                    AgentOperation.operation_id == operation_id,
                    AgentOperation.exam_session_id == scope,
                    AgentOperation.conversation_id == cid,
                )
            )
        if run_id is None:
            raise ConversationNotFound("operation unavailable")
        with self.runs.locked(scope, cid, run_id, open_required=open_required) as (
            db,
            conv,
            run,
        ):
            if token is not None:
                self.runs._lease(run, token)
                run.lease_expires_at = (
                    int(self.runs.clock()) + run.policy_json["runtime"]["lease_seconds"]
                )
            row = db.scalar(
                select(AgentOperation)
                .where(AgentOperation.operation_id == operation_id)
                .with_for_update()
            )
            yield db, conv, run, row

    @staticmethod
    def _view(row):
        receipt = row.receipt_json or {}
        file_result = (
            json.loads(row.result_json["content"])
            if row.kind == "file_edit" and row.result_json
            else {}
        )
        return {
            "operation_id": row.operation_id,
            "kind": row.kind,
            "execution_id": row.operation_id,
            "run_id": row.run_id,
            "turn_id": row.turn_id,
            "step_id": row.step_id,
            "tool_call_id": row.tool_call_id,
            "status": row.status,
            "policy_decision": row.policy_decision,
            "reason_code": row.reason_code,
            "arguments": row.args_json,
            "expected_versions": {
                p: v["version"] for p, v in row.binding_json.get("files", {}).items()
            },
            "task_version_id": row.binding_json["sandbox"].get("task_version_id"),
            "revision": row.revision,
            "cancel_requested": row.cancel_requested,
            "processes_stopped": receipt.get(
                "processes_stopped", not row.holds_workspace
            ),
            "started_at": receipt.get("command_started_at"),
            "finished_at": receipt.get("finished_at"),
            "exit_code": receipt.get("exit_code"),
            "output_complete": receipt.get("output_complete", False),
            "stdout_bytes": receipt.get("stdout_bytes", 0),
            "stderr_bytes": receipt.get("stderr_bytes", 0),
            "stdout_truncated": receipt.get("stdout_truncated", False),
            "stderr_truncated": receipt.get("stderr_truncated", False),
            "change": row.binding_json.get("change"),
            "file_result": file_result or None,
        }

    def event(self, db, conv, run, row, kind, **data):
        row.updated_at = max(row.updated_at, int(self.runs.clock()))
        self.runs.event(
            db,
            conv,
            kind,
            run,
            operation_id=row.operation_id,
            step_id=row.step_id,
            tool_call_id=row.tool_call_id,
            **data,
        )

    @staticmethod
    def target(db, scope):
        exam = db.get(ExamSession, scope)
        return SessionView(
            **{
                field: getattr(exam, field)
                for field in SessionView.__dataclass_fields__
            }
        )

    def assert_target(self, db, row):
        view = self.target(db, row.exam_session_id)
        bound = row.binding_json["sandbox"]
        keys = (
            "sandbox_id",
            "sandbox_resource_key",
            "sandbox_backend_ref",
            "sandbox_image_ref",
            "task_version_id",
            "deadline_at",
        )
        if (
            view.status != "in_progress"
            or view.sandbox_deleted_at is not None
            or view.deadline_at is None
            or view.deadline_at <= int(self.runs.clock())
        ):
            raise ReadingError("WORKSPACE_NOT_READY")
        if any(getattr(view, key) != bound.get(key) for key in keys):
            raise ReadingError("SANDBOX_BINDING_CHANGED")
        return view

    def create(self, scope, cid, run_id, token, step_id, call, args):
        with self.runs.locked(scope, cid, run_id) as (db, conv, run):
            self.runs._lease(run, token)
            state = self.runs.sessions._state(conv)
            turn, step, actual = self.materials._call(state, step_id, call.id)
            if (
                turn is not state.active_turn
                or step.status != "awaiting_tools"
                or actual != call
            ):
                raise SessionConflict("command is not a pending native call")
            old = db.scalar(
                select(AgentOperation).where(
                    AgentOperation.run_id == run_id,
                    AgentOperation.step_id == step_id,
                    AgentOperation.tool_call_id == call.id,
                )
            )
            if old:
                if old.call_sha256 != call_digest(call):
                    raise SessionConflict("command call changed")
                return old.operation_id
            count = len(
                db.scalars(
                    select(AgentOperation.operation_id).where(
                        AgentOperation.run_id == run_id
                    )
                ).all()
            )
            if count >= run.policy_json["runtime"]["max_operations"]:
                raise RunBudgetExceeded()
            view = self.target(db, scope)
            binding = {
                "sandbox": asdict(view),
                "policy_sha256": digest(encoded(run.policy_json)),
            }
            identity = uuid.uuid4().hex
            is_edit = call.name in ("write_file", "apply_patch")
            config = {
                "operation_id": identity,
                "kind": "file_edit" if is_edit else "command",
                "tool_name": call.name,
                "timeout_seconds": min(
                    30 if is_edit else args.timeout_seconds,
                    run.policy_json["runtime"]["max_command_seconds"],
                ),
                "max_output_bytes": 1024 if is_edit else args.max_output_bytes,
                "deadline_at": min(
                    run.deadline_at, view.deadline_at or run.deadline_at
                ),
                "expected_digests": {},
            }
            if not is_edit:
                config.update(argv=list(args.argv), cwd=args.cwd)
            row = AgentOperation(
                operation_id=identity,
                kind=config["kind"],
                run_id=run_id,
                conversation_id=cid,
                exam_session_id=scope,
                turn_id=turn.id,
                step_id=step_id,
                tool_call_id=call.id,
                call_sha256=call_digest(call),
                args_json=args.model_dump(mode="json"),
                binding_json=binding,
                binding_sha256=digest(encoded(binding)),
                config_json=config,
                status="requested",
                revision=0,
                holds_workspace=False,
                cancel_requested=False,
                created_at=int(self.runs.clock()),
                updated_at=int(self.runs.clock()),
            )
            db.add(row)
            self.event(db, conv, run, row, "operation.requested")
            db.flush()
            return identity

    def error(self, scope, cid, operation_id, code, *, status="failed"):
        with self.locked(scope, cid, operation_id) as (db, conv, run, row):
            if row.result_json:
                return ToolResult.model_validate(row.result_json)
            if row.holds_workspace:
                raise SessionConflict(
                    "cannot declare an active execution failed without a stop receipt"
                )
            body = {"status": status, "code": code, "execution_id": operation_id}
            if row.kind == "file_edit" and row.binding_json.get("change"):
                body["change"] = {**row.binding_json["change"], "applied": False}
            result = ToolResult(
                tool_call_id=row.tool_call_id,
                is_error=True,
                content=encoded(body),
            )
            row.result_json, row.status, row.reason_code = (
                result.model_dump(mode="json"),
                status,
                code,
            )
            self.event(db, conv, run, row, "operation." + status, reason_code=code)
            return result

    @staticmethod
    def file_versions(db, scope):
        return {
            row.path: {"version": row.version, "sha256": row.content_sha256}
            for row in db.scalars(
                select(WorkspaceFileRevision).where(
                    WorkspaceFileRevision.session_id == scope
                )
            )
            if row.path in ALLOWED_FILES
        }

    def preflight(self, scope, cid, operation_id, token):
        if self.reader.workspace is None:
            raise ReadingError("WORKSPACE_UNAVAILABLE")
        try:
            self.reader.workspace.tree(scope)
            with self.locked(scope, cid, operation_id) as (_, _, _, row):
                edit_path = row.args_json["path"] if row.kind == "file_edit" else None
            current = (
                self.reader.workspace.read(scope, edit_path) if edit_path else None
            )
        except (LifecycleConflict, LifecycleNotFound):
            raise ReadingError("WORKSPACE_NOT_READY") from None
        except SessionBusy:
            raise ReadingError("WORKSPACE_BUSY") from None
        except (
            TimeoutError,
            SandboxOperationFailed,
            RevisionStoreUnavailable,
            ValueError,
        ):
            raise ReadingError("WORKSPACE_UNAVAILABLE") from None
        with self.locked(scope, cid, operation_id, token=token, open_required=True) as (
            db,
            conv,
            run,
            row,
        ):
            self.assert_target(db, row)
            versions = self.file_versions(db, scope)
            if set(versions) != ALLOWED_FILES:
                raise ReadingError("FILE_REVISIONS_UNAVAILABLE")
            expected_versions = (
                {row.args_json["path"]: row.args_json["expected_version"]}
                if row.kind == "file_edit"
                else row.args_json["expected_versions"]
            )
            if any(
                versions[path]["version"] != expected
                for path, expected in expected_versions.items()
            ):
                raise ReadingError("SOURCE_VERSION_CHANGED")
            if "files" in row.binding_json:
                if row.binding_json["files"] != versions:
                    raise ReadingError("SOURCE_VERSION_CHANGED")
            else:
                row.binding_json = {**row.binding_json, "files": versions}
                if row.kind == "file_edit":
                    if current["version"] != versions[edit_path]["version"]:
                        raise ReadingError("SOURCE_VERSION_CHANGED")
                    args_type = (
                        WriteFile
                        if row.config_json["tool_name"] == "write_file"
                        else ApplyPatch
                    )
                    change = prepare_change(
                        self.materials,
                        db,
                        scope,
                        operation_id,
                        current["content"],
                        args_type.model_validate(row.args_json),
                    )
                    row.binding_json = {**row.binding_json, "change": change}
                    after = self.materials._material(
                        db, scope, change["after_material_id"]
                    )
                    row.config_json = {
                        **row.config_json,
                        "edit": {
                            "path": edit_path,
                            "before_sha256": change["before_sha256"],
                            "after_sha256": change["after_sha256"],
                            "content_b64": base64.b64encode(
                                after.text.encode("utf-8")
                            ).decode("ascii"),
                        },
                    }
                row.binding_sha256 = digest(
                    encoded({"binding": row.binding_json, "args": row.args_json})
                )
                row.config_json = {
                    **row.config_json,
                    "request_sha256": row.binding_sha256,
                    "expected_digests": {
                        path: value["sha256"] for path, value in versions.items()
                    },
                }
                self.event(
                    db,
                    conv,
                    run,
                    row,
                    "operation.preflight",
                    versions={p: v["version"] for p, v in versions.items()},
                )

    def data(self, scope, cid, operation_id):
        with self.locked(scope, cid, operation_id) as (_, _, _, row):
            return (
                self._view(row),
                SessionView(**row.binding_json["sandbox"]),
                dict(row.config_json),
                row.handle,
            )

    def hold(self, scope, cid, operation_id, token):
        with self.locked(scope, cid, operation_id, token=token, open_required=True) as (
            db,
            conv,
            run,
            row,
        ):
            self.assert_target(db, row)
            if self.file_versions(db, scope) != row.binding_json["files"]:
                raise ReadingError("SOURCE_VERSION_CHANGED")
            other = db.scalar(
                select(AgentOperation.operation_id).where(
                    AgentOperation.exam_session_id == scope,
                    AgentOperation.holds_workspace.is_(True),
                    AgentOperation.operation_id != operation_id,
                )
            )
            if other:
                raise ReadingError("SANDBOX_OPERATION_BUSY")
            row.status, row.holds_workspace = "preparing", True
            self.event(db, conv, run, row, "operation.preparing")

    def wait(self, scope, cid, operation_id, token):
        with self.locked(scope, cid, operation_id, token=token) as (db, conv, run, row):
            run.status, run.lease_token, run.lease_expires_at = (
                "waiting_operation",
                None,
                None,
            )
            self.event(db, conv, run, row, "run.waiting_operation")
        raise OperationWaiting()

    def _snapshot(self, row, stream):
        receipt = row.receipt_json or {}
        data = output_bytes(receipt, stream)
        return SourceSnapshot(
            "execution_" + stream,
            row.operation_id + ":" + stream,
            str(row.revision),
            output_text(data),
            {
                "execution_id": row.operation_id,
                "stream": stream,
                "status": row.status,
                "exit_code": receipt.get("exit_code"),
                "capture_truncated": receipt.get(stream + "_truncated", False),
                "output_complete": receipt.get("output_complete", False),
                "output_can_grow": row.holds_workspace,
                "encoding": "utf8_replace_nul_escape",
            },
            hashlib.sha256(data).hexdigest(),
        )

    def record(self, scope, cid, operation_id, value):
        with self.locked(scope, cid, operation_id) as (db, conv, run, row):
            if row.result_json:
                return ToolResult.model_validate(row.result_json)
            previous = row.receipt_json or {}
            if row.kind == "file_edit":
                value["pause_owned"] = value.get("pause_owned", False) or previous.get(
                    "pause_owned", False
                )
            for key, old in previous.items():
                if key not in value and key not in (
                    "status",
                    "reason_code",
                    "processes_stopped",
                    "output_complete",
                ):
                    value[key] = old
            snapshot = ExecutionSnapshot.model_validate(value)
            if (
                snapshot.operation_id is not None
                and snapshot.operation_id != operation_id
            ):
                raise ReadingError("EXECUTION_RECEIPT_INVALID")
            if (
                snapshot.request_sha256 is not None
                and snapshot.request_sha256 != row.binding_sha256
            ):
                raise ReadingError("EXECUTION_RECEIPT_INVALID")
            payload = snapshot.model_dump(mode="json")
            if (
                snapshot.status not in ("running", "unknown")
                and not snapshot.processes_stopped
            ):
                raise ReadingError("EXECUTION_RECEIPT_INVALID")
            for stream in ("stdout", "stderr"):
                old, new = output_bytes(previous, stream), output_bytes(payload, stream)
                if (
                    not new.startswith(old)
                    or len(new) > row.config_json["max_output_bytes"]
                ):
                    raise ReadingError("EXECUTION_RECEIPT_INVALID")
                if len(new) > len(old):
                    self.event(
                        db,
                        conv,
                        run,
                        row,
                        "operation.output",
                        stream=stream,
                        byte_offset=len(old),
                        data_b64=base64.b64encode(new[len(old) :]).decode("ascii"),
                    )
            if payload != previous:
                row.revision += 1
                row.receipt_json = payload
            row.status, row.reason_code = snapshot.status, snapshot.reason_code
            if row.kind == "file_edit" and snapshot.processes_stopped:
                return finish_change(self, db, conv, run, row, snapshot)
            row.holds_workspace = not snapshot.processes_stopped
            if not snapshot.processes_stopped:
                self.event(db, conv, run, row, "operation." + snapshot.status)
                return None
            if row.cancel_requested and snapshot.status == "unknown":
                row.status, row.reason_code = (
                    "cancelled",
                    "CANCELLED_WITHOUT_COMPLETE_RECEIPT",
                )
            materials = {
                stream: self.materials._put(db, scope, self._snapshot(row, stream))
                for stream in ("stdout", "stderr")
            }
            limit = 2000
            while True:
                content = {
                    "execution_id": operation_id,
                    "status": row.status,
                    "code": row.reason_code,
                    "exit_code": snapshot.exit_code,
                    "processes_stopped": True,
                    "output_complete": snapshot.output_complete,
                    "output_revision": row.revision,
                    "started_at": snapshot.command_started_at,
                    "finished_at": snapshot.finished_at,
                    "starting_versions": {
                        p: v["version"]
                        for p, v in row.binding_json.get("files", {}).items()
                    },
                }
                for stream in ("stdout", "stderr"):
                    text = self._snapshot(row, stream).text
                    content.update(
                        {
                            stream: text[:limit],
                            stream + "_material_id": materials[stream],
                            stream + "_preview_has_more": len(text) > limit,
                            stream
                            + "_capture_truncated": getattr(
                                snapshot, stream + "_truncated"
                            ),
                        }
                    )
                if len(encoded(content)) <= 16000 or limit == 0:
                    break
                limit //= 2
            result = ToolResult(
                tool_call_id=row.tool_call_id,
                content=encoded(content),
                is_error=row.status in ("unknown", "conflict", "denied")
                or row.reason_code == "SUPERVISOR_FAILED",
            )
            row.result_json = result.model_dump(mode="json")
            self.event(
                db,
                conv,
                run,
                row,
                "operation." + row.status,
                reason_code=row.reason_code,
                exit_code=snapshot.exit_code,
                materials=materials,
            )
            return result

    def observe(self, scope, cid, operation_id):
        meta, view, config, handle = self.data(scope, cid, operation_id)
        if self.backend is not None and meta["status"] in (
            "dispatching",
            "running",
            "unknown",
        ):
            try:
                self.record(
                    scope,
                    cid,
                    operation_id,
                    self.backend.inspect_execution(view, config, handle),
                )
            except BACKEND_ERRORS:
                pass  # Keep the last persisted prefix and its unresolved state.
        with self.locked(scope, cid, operation_id) as (_, _, _, row):
            return self._view(row)

    def execute(
        self, scope, cid, run_id, token, step_id, call, args, policy, interaction
    ):
        operation_id = self.create(scope, cid, run_id, token, step_id, call, args)
        with self.locked(scope, cid, operation_id) as (_, _, _, row):
            if row.result_json:
                return ToolResult.model_validate(row.result_json)
            status = row.status
        if self.backend is None:
            return self.error(scope, cid, operation_id, "EXECUTOR_UNAVAILABLE")
        if status in ("requested", "waiting_approval"):
            choice = policy.action_decision
            if policy.mode == "confirm_actions" and choice != "deny":
                choice = "ask"
            if policy.mode not in ("confirm_actions", "sandbox_auto"):
                choice = "deny"
            if choice == "ask" and interaction:
                choice = "allow" if interaction["status"] == "approved" else "deny"
            with self.locked(scope, cid, operation_id) as (_, _, _, row):
                row.policy_decision = choice
            if choice == "deny":
                return self.error(
                    scope, cid, operation_id, "ACTION_DENIED", status="denied"
                )
            try:
                self.preflight(scope, cid, operation_id, token)
            except ReadingError as error:
                return self.error(
                    scope, cid, operation_id, error.code, status="conflict"
                )
            if choice == "ask":
                with self.locked(scope, cid, operation_id) as (db, conv, run, row):
                    row.status = "waiting_approval"
                    request = {
                        "tool_name": call.name,
                        "arguments": row.args_json,
                        "operation_id": operation_id,
                        "expected_versions": self._view(row)["expected_versions"],
                        "policy_decision": "ask",
                        "reason_code": "ACTION_APPROVAL_REQUIRED",
                    }
                    if row.kind == "file_edit":
                        change = row.binding_json["change"]
                        diff = self.materials._material(
                            db, scope, change["diff_material_id"]
                        )
                        request["change"] = {
                            **change,
                            "diff_preview": diff.text[:2000],
                            "diff_preview_has_more": len(diff.text) > 2000,
                        }
                    binding = {
                        "operation_id": operation_id,
                        "operation_binding_sha256": row.binding_sha256,
                    }
                self.runs.wait(
                    scope,
                    cid,
                    run_id,
                    token,
                    step_id,
                    call,
                    kind="approval",
                    request=request,
                    binding=binding,
                )
                raise OperationWaiting()
            try:
                self.hold(scope, cid, operation_id, token)
            except ReadingError as error:
                return self.error(
                    scope, cid, operation_id, error.code, status="conflict"
                )
            status = "preparing"
        meta, view, config, handle = self.data(scope, cid, operation_id)
        if status == "preparing":
            try:
                prepared = self.backend.prepare_execution(view, config)
            except SandboxOperationFailed:
                # Preparation can only create a stopped helper; it never starts
                # candidate code. A definite failure need not occupy the workspace.
                with self.locked(scope, cid, operation_id, token=token) as (
                    _,
                    _,
                    _,
                    row,
                ):
                    if row.status == "preparing":
                        row.holds_workspace = False
                return self.error(scope, cid, operation_id, "EXECUTION_PREPARE_FAILED")
            except TimeoutError:
                return self.wait(scope, cid, operation_id, token)
            with self.locked(
                scope, cid, operation_id, token=token, open_required=True
            ) as (db, conv, run, row):
                if row.status != "preparing" or row.cancel_requested:
                    if row.result_json:
                        return ToolResult.model_validate(row.result_json)
                    raise SessionConflict("preparation was cancelled")
                self.assert_target(db, row)
                row.handle, row.status = prepared["handle"], "prepared"
                handle = row.handle
                self.event(db, conv, run, row, "operation.prepared")
            status = "prepared"
        if status == "prepared":
            with self.locked(
                scope, cid, operation_id, token=token, open_required=True
            ) as (db, conv, run, row):
                self.assert_target(db, row)
                if row.status != "prepared" or row.cancel_requested:
                    if row.result_json:
                        return ToolResult.model_validate(row.result_json)
                    raise SessionConflict("command cannot be dispatched twice")
                row.status = "dispatching"
                self.event(db, conv, run, row, "operation.dispatching")
            try:
                if config.get("kind") == "file_edit":
                    # The original terminal pause is a lifecycle transition:
                    # serialize it with submission and cancellation. Dispatch
                    # intent was committed above before this bounded Docker I/O.
                    with self.locked(
                        scope, cid, operation_id, token=token, open_required=True
                    ) as (db, _, _, row):
                        self.assert_target(db, row)
                        if row.cancel_requested or row.status != "dispatching":
                            raise SessionConflict(
                                "file edit was cancelled before start"
                            )
                        self.backend.start_execution(view, config, handle)
                else:
                    self.backend.start_execution(view, config, handle)
            except BACKEND_ERRORS:
                pass  # No restart. Query this exact accepted-or-unknown start.
        try:
            snapshot = self.backend.inspect_execution(view, config, handle)
            result = self.record(scope, cid, operation_id, snapshot)
        except BACKEND_ERRORS:
            self.record(
                scope,
                cid,
                operation_id,
                {
                    "status": "unknown",
                    "processes_stopped": False,
                    "reason_code": "EXECUTION_UNREACHABLE",
                },
            )
            self.runs.stop(
                scope, cid, run_id, "unknown", "EXECUTION_OUTCOME_UNKNOWN", token=token
            )
            raise OperationWaiting()
        if result is not None:
            return result
        return self.wait(scope, cid, operation_id, token)

    def cancel(self, scope, cid, operation_id):
        with self.locked(scope, cid, operation_id) as (db, conv, run, row):
            if row.result_json:
                return self._view(row)
            row.cancel_requested = True
            self.event(db, conv, run, row, "operation.cancel_requested")
            early = row.status in ("requested", "waiting_approval", "preparing")
            if early:
                row.holds_workspace = False
                pending = db.scalar(
                    select(AgentInteraction).where(
                        AgentInteraction.run_id == row.run_id,
                        AgentInteraction.step_id == row.step_id,
                        AgentInteraction.tool_call_id == row.tool_call_id,
                    )
                )
                if pending and pending.status == "pending":
                    pending.status = "cancelled"
                    if run.status == "waiting_approval":
                        run.status = "queued"
        if early:
            self.error(
                scope,
                cid,
                operation_id,
                "CANCELLED_BEFORE_DISPATCH",
                status="cancelled",
            )
        else:
            _, view, config, handle = self.data(scope, cid, operation_id)
            if self.backend is not None:
                try:
                    stopped = self.backend.cancel_execution(view, config, handle)
                    try:
                        self.record(scope, cid, operation_id, stopped)
                    except ReadingError:
                        if not stopped.get("processes_stopped"):
                            raise
                        # Discard the invalid outcome but retain daemon stop and
                        # private pause ownership; neither is candidate output.
                        self.record(
                            scope,
                            cid,
                            operation_id,
                            {
                                "status": "unknown",
                                "processes_stopped": True,
                                "pause_owned": stopped.get("pause_owned", False),
                                "reason_code": "EXECUTION_RECEIPT_INVALID",
                            },
                        )
                except BACKEND_ERRORS:
                    pass
        with self.locked(scope, cid, operation_id) as (_, _, _, row):
            return self._view(row)

    def cancel_run(self, scope, cid, run_id):
        with session_scope(self.runs.factory) as db:
            ids = db.scalars(
                select(AgentOperation.operation_id).where(
                    AgentOperation.exam_session_id == scope,
                    AgentOperation.conversation_id == cid,
                    AgentOperation.run_id == run_id,
                    AgentOperation.result_json.is_(None),
                )
            ).all()
        for identity in ids:
            self.cancel(scope, cid, identity)

    def list(self, scope, cid, *, offset=0, limit=20):
        with session_scope(self.runs.factory) as db:
            self.runs.sessions._row(db, scope, cid)
            rows = db.scalars(
                select(AgentOperation)
                .where(
                    AgentOperation.conversation_id == cid,
                    AgentOperation.exam_session_id == scope,
                )
                .order_by(AgentOperation.created_at, AgentOperation.operation_id)
                .offset(offset)
                .limit(limit + 1)
            ).all()
            return {
                "items": [self._view(row) for row in rows[:limit]],
                "offset": offset,
                "has_more": len(rows) > limit,
            }

    def read_output(
        self,
        scope,
        cid,
        operation_id,
        stream,
        *,
        offset=0,
        max_chars=8000,
        expected_revision=None,
    ):
        with self.locked(scope, cid, operation_id) as (_, _, _, row):
            if expected_revision is not None and row.revision != expected_revision:
                raise ReadingError("SOURCE_VERSION_CHANGED")
            return fragment(
                self._snapshot(row, stream), offset=offset, max_chars=max_chars
            )

    def read_change(self, scope, cid, operation_id, part, *, offset=0, max_chars=8000):
        if part not in ("before", "after", "diff"):
            raise ReadingError("CHANGE_PART_INVALID")
        with self.locked(scope, cid, operation_id) as (db, _, _, row):
            plan = row.binding_json.get("change")
            if row.kind != "file_edit" or not plan:
                raise ReadingError("SOURCE_UNAVAILABLE")
            identity = plan[part + "_material_id"]
            snapshot = self.materials._material(db, scope, identity)
            return {
                "operation_id": operation_id,
                "part": part,
                "proposal": True,
                "material_id": identity,
                **fragment(snapshot, offset=offset, max_chars=max_chars),
            }

    def read_tool(self, scope, cid, step_id, call, args):
        with session_scope(self.runs.factory) as db:
            source = db.scalar(
                select(AgentOperation).where(
                    AgentOperation.operation_id == args.execution_id,
                    AgentOperation.exam_session_id == scope,
                )
            )
            if source is None:
                raise ReadingError("SOURCE_UNAVAILABLE")
            source_cid = source.conversation_id
        with self.locked(scope, source_cid, args.execution_id) as (_, _, _, row):
            if (
                args.expected_revision is not None
                and row.revision != args.expected_revision
            ):
                raise ReadingError("SOURCE_VERSION_CHANGED")
            snapshot = self._snapshot(row, args.stream)
            projection = fragment(
                snapshot, offset=args.offset, max_chars=args.max_chars
            )
        return self.materials.record_read(
            scope,
            cid,
            step_id,
            call,
            action_kind="read",
            snapshot=snapshot,
            projection=projection,
        )

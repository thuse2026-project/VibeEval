"""Concrete phase resource gateway over PostgreSQL, runsc and the Agent runtime."""

import hashlib
import time
from contextlib import contextmanager
from dataclasses import replace

from sqlalchemy import select

from app.agent_integration.material_store import MaterialStore
from app.agent_integration.run_store import ACTIVE, RunStore
from app.agent_integration.runtime import RuntimeCoordinator
from app.agent_integration.sources import SourceReader
from app.agent_integration.window import STAGE_AGENT_WINDOW
from app.db.models import (
    AgentOperation,
    AgentRun,
    AgentScope,
    ExamFlowAttempt,
    ExamSession,
    StageFileRevision,
    StageWorkspace,
)
from app.exam_flow.types import Artifact, EvidenceCutoff, FlowError, FlowState
from app.llm.config import load_settings
from app.submissions.storage import SnapshotStore
from app.workspace.files import encode_content
from app.workspace.service import SaveConflict

from . import evidence
from .types import STAGE_REFERENCE_ADAPTER, ResourceSpec, WorkspaceView, validate_file


class StageResources:
    supports_stage_resources = True

    def __init__(
        self,
        factory,
        specs,
        sandbox,
        *,
        snapshots=None,
        settings=None,
        client=None,
        clock=time.time,
    ):
        self.factory, self.sandbox, self.clock = factory, sandbox, clock
        self.specs = {
            key: ResourceSpec.model_validate(value) for key, value in specs.items()
        }
        self.snapshots = snapshots or SnapshotStore()
        self.settings, self.client, self._runtimes = settings, client, {}
        self.agent_budget_overrides: dict[str, int] = {}
        self.agent_window_policy = STAGE_AGENT_WINDOW
        if not all(
            callable(getattr(sandbox, name, None))
            for name in (
                "ensure_bound",
                "check_ready",
                "close_workspace",
                "export_volume",
                "read_file",
                "read_files",
                "save_file",
            )
        ):
            raise TypeError("complete stage sandbox runtime required")

    @contextmanager
    def _locked(self, session_id):
        with self.factory.begin() as db:
            exam = db.scalar(
                select(ExamSession)
                .where(ExamSession.session_id == session_id)
                .with_for_update()
            )
            row = db.scalar(
                select(ExamFlowAttempt)
                .where(ExamFlowAttempt.session_id == session_id)
                .with_for_update()
            )
            if exam is None or row is None or exam.exam_mode != "multi_stage":
                raise FlowError("ATTEMPT_NOT_FOUND", 404)
            try:
                state = FlowState.model_validate(row.state_json)
            except ValueError:
                raise FlowError("FLOW_STATE_UNAVAILABLE", 503) from None
            workspace = db.scalar(
                select(StageWorkspace)
                .where(StageWorkspace.workspace_id == state.stage.workspace_id)
                .with_for_update()
            )
            yield db, state, workspace

    @staticmethod
    def _operation(state, context, operation_id, kind):
        if (
            state.session_id != context.session_id
            or state.id != context.attempt_id
            or state.stage.id != context.stage_id
            or state.phase.id != context.phase_id
            or state.stage.workspace_id != context.workspace_id
            or state.phase.permission_epoch != context.permission_epoch
            or state.operation is None
            or state.operation.id != operation_id
            or state.operation.kind != kind
        ):
            raise FlowError("RESOURCE_OPERATION_REVOKED")

    @staticmethod
    def _view(row, state, *, status=None):
        spec = ResourceSpec.model_validate(row.spec_json)
        return WorkspaceView(
            session_id=row.workspace_id,
            status=status or "in_progress",
            task_version_id=spec.task_version,
            sandbox_id=row.sandbox_id,
            sandbox_resource_key=row.sandbox_resource_key,
            sandbox_image_ref=row.sandbox_image_ref,
            sandbox_backend_ref=row.sandbox_backend_ref,
            admitted_at=0,
            started_at=state.phase.started_at,
            deadline_at=state.phase.deadline_at,
            submitted_at=None,
            sandbox_deleted_at=None,
            lifecycle_error_code=None,
            lifecycle_error_at=None,
            allowed_files=spec.files,
            writable_files=spec.writable,
            read_only=row.read_only,
            file_sha256=spec.file_sha256,
        )

    def prepare(self, context, definition, operation_id):
        if any(
            "use_terminal" in p.capabilities
            and "write_repository" not in p.capabilities
            for p in definition.phases
        ):
            raise FlowError("RESOURCE_POLICY_INVALID", 503)
        spec = self.specs.get(definition.resource_ref)
        if spec is None or spec.task_version != definition.task_version:
            raise FlowError("RESOURCE_SPEC_UNAVAILABLE", 503)
        with self._locked(context.session_id) as (db, state, row):
            self._operation(state, context, operation_id, "prepare")
            if row is None:
                row = StageWorkspace(
                    workspace_id=context.workspace_id,
                    session_id=context.session_id,
                    attempt_id=context.attempt_id,
                    stage_id=context.stage_id,
                    spec_json=spec.model_dump(mode="json"),
                    sandbox_resource_key="vbe-stage-"
                    + hashlib.sha256(context.workspace_id.encode()).hexdigest()[:40],
                    sandbox_image_ref=spec.image_ref,
                    sandbox_backend_ref=self.sandbox.settings[1],
                    read_only=not any(
                        "write_repository" in p.capabilities
                        or "use_terminal" in p.capabilities
                        for p in definition.phases
                    ),
                    status="preparing",
                    permission_epoch=0,
                )
                db.add(row)
            elif (
                row.stage_id != context.stage_id
                or row.attempt_id != context.attempt_id
                or ResourceSpec.model_validate(row.spec_json) != spec
            ):
                raise FlowError("RESOURCE_BINDING_CHANGED")
        # Persist the plan before allocating. Retries use the same owned name.
        with self._locked(context.session_id) as (_, state, row):
            self._operation(state, context, operation_id, "prepare")
            view = self._view(row, state, status="provisioning")
            identity = self.sandbox.ensure_bound(view)
            self.sandbox.check_ready(replace(view, sandbox_id=identity))
            row.sandbox_id, row.status = identity, "ready"

    def _runtime(self, phase_id, capabilities, spec):
        cached = self._runtimes.get(phase_id)
        if cached is not None:
            return cached
        settings = self.settings or load_settings()
        mode = spec.agent_policy.mode
        if (
            "write_repository" not in capabilities
            and "use_terminal" not in capabilities
        ):
            mode = "read_only" if mode != "chat_only" else mode
        policy = spec.agent_policy.model_copy(
            update={**self.agent_budget_overrides, "mode": mode}
        )
        reader = SourceReader(
            MaterialStore(self.factory, clock=self.clock),
            workspace=PhaseWorkspace(self, phase_id, capabilities),
            manifest=spec.files,
            reference_adapter=STAGE_REFERENCE_ADAPTER,
        )
        runtime = RuntimeCoordinator(
            reader,
            settings=settings,
            client=self.client,
            policy=policy,
            window_policy=self.agent_window_policy,
            clock=self.clock,
            execution_backend=self.sandbox,
            access_monitor=reader.store.check_open,
        )
        self._runtimes[phase_id] = runtime
        return runtime

    def activate_phase(self, context, definition, operation_id):
        with self._locked(context.session_id) as (db, state, row):
            self._operation(state, context, operation_id, "open")
            if row is None or row.status not in ("ready", "frozen", "active"):
                raise FlowError("WORKSPACE_NOT_READY", 503)
            if row.read_only and (
                "write_repository" in definition.capabilities
                or "use_terminal" in definition.capabilities
            ):
                raise FlowError("CAPABILITY_DENIED", 403)
            spec = ResourceSpec.model_validate(row.spec_json)
            if "use_agent" in definition.capabilities:
                self._runtime(context.phase_id, definition.capabilities, spec)
            if "read_frozen_code" in definition.capabilities:
                if row.artifact_json is None or row.sandbox_id is not None:
                    raise FlowError("WORKSPACE_NOT_FROZEN", 503)
                EvidenceCutoff.model_validate(row.artifact_json["evidence"])
                self._read_snapshot(row)
            elif row.sandbox_id is None:
                view = self._view(row, state, status="provisioning")
                identity = self.sandbox.ensure_bound(view)
                self.sandbox.check_ready(replace(view, sandbox_id=identity))
                row.sandbox_id = identity
            scope = db.get(AgentScope, context.phase_id)
            if scope is None:
                scope = AgentScope(
                    scope_id=context.phase_id,
                    session_id=context.session_id,
                    kind="phase",
                    phase_id=context.phase_id,
                    workspace_id=context.workspace_id,
                    status="active",
                    permission_epoch=context.permission_epoch,
                )
                db.add(scope)
            elif scope.status == "closed" or scope.workspace_id != context.workspace_id:
                raise FlowError("AUTHORIZATION_REVOKED", 403)
            scope.status, scope.permission_epoch = "active", context.permission_epoch
            row.current_phase_id, row.permission_epoch, row.status = (
                context.phase_id,
                context.permission_epoch,
                "active",
            )

    def close_phase(self, context, operation_id):
        # The flow coordinator has already committed its phase-level write fence.
        with self._locked(context.session_id) as (db, state, row):
            self._operation(state, context, operation_id, "close")
            if row is None:
                raise FlowError("WORKSPACE_NOT_FOUND", 503)
            scope = db.get(AgentScope, context.phase_id)
            if scope is not None:
                scope.status, scope.permission_epoch = (
                    "closed",
                    context.permission_epoch,
                )
            if row.sandbox_id:
                self.sandbox.close_workspace(
                    self._view(row, state, status="submitting")
                )
            row.sandbox_id, row.status = None, "frozen"
            row.permission_epoch = context.permission_epoch
            # Physical stopping is complete. A late operation must never resume
            # this workspace or retain a holder that can block the next phase.
            for op in db.scalars(
                select(AgentOperation).where(
                    AgentOperation.exam_session_id == context.phase_id
                )
            ):
                if op.result_json is None:
                    op.cancel_requested, op.holds_workspace = True, False
                    op.status, op.reason_code = "cancelled", "PHASE_CLOSED"
        runs = RunStore(self.factory, clock=self.clock)
        with self.factory.begin() as db:
            active = [
                (r.conversation_id, r.run_id)
                for r in db.scalars(
                    select(AgentRun).where(
                        AgentRun.exam_session_id == context.phase_id,
                        AgentRun.status.in_(ACTIVE),
                    )
                )
            ]
        for cid, rid in active:
            runs.stop(context.phase_id, cid, rid, "interrupted", "PHASE_CLOSED")
        self._runtimes.pop(context.phase_id, None)
        with self._locked(context.session_id) as (db, state, row):
            self._operation(state, context, operation_id, "close")
            next_index = state.stage.phase_index + 1
            phases = state.stage.definition.phases
            if (
                next_index < len(phases)
                and "read_frozen_code" in phases[next_index].capabilities
            ):
                if row.artifact_json is None:
                    self._capture_snapshot(row, state)
                    cutoff = EvidenceCutoff(
                        coding_phase_id=context.phase_id,
                        sequence=evidence.head(db, context.phase_id),
                        completeness=(
                            "complete"
                            if row.evidence_status == "complete"
                            else "partial"
                        ),
                        reason=(
                            None
                            if row.evidence_status == "complete"
                            else (
                                "CAPTURE_GAP"
                                if row.evidence_status == "partial"
                                else "COLLECTION_NOT_CONNECTED"
                            )
                        ),
                    )
                    row.artifact_json = {
                        **row.artifact_json,
                        "evidence": cutoff.model_dump(mode="json"),
                    }
                self._read_snapshot(row)

    def _capture_snapshot(self, row, state):
        files = self.sandbox.export_volume(self._view(row, state, status="submitting"))
        stored = self.snapshots.write(files)
        row.artifact_json = {
            "sha256": stored.content_sha256,
            "size_bytes": stored.size_bytes,
            "file_count": stored.file_count,
        }

    def _read_snapshot(self, row):
        stored = row.artifact_json
        return self.snapshots.read(
            stored["sha256"],
            size_bytes=stored["size_bytes"],
            file_count=stored["file_count"],
        )

    def append_evidence(self, session_id, phase_id, item):
        """Server-only. Pass candidate-visible facts from the actual recorder."""
        item = evidence.EvidenceItem.model_validate(item)
        with self.guard(session_id, phase_id, "write_repository") as (db, _, _, _):
            return evidence.append(db, phase_id, item)

    def mark_evidence_collection(self, session_id, phase_id, *, complete: bool):
        """Called by a trusted recorder. Once a gap is reported it stays visible."""
        with self.guard(session_id, phase_id, "write_repository") as (_, _, row, _):
            if row.evidence_status != "partial":
                row.evidence_status = "complete" if complete else "partial"

    def frozen_code(self, session_id, phase_id):
        with self.guard(session_id, phase_id, "read_frozen_code") as (_, state, row, _):
            coding = state.stage.phases[state.stage.phase_index - 1]
            return {
                "workspace_id": row.workspace_id,
                "sha256": row.artifact_json["sha256"],
                "coding_phase_id": coding.id,
                "closed_at": coding.closed_at,
                "evidence": EvidenceCutoff.model_validate(
                    row.artifact_json["evidence"]
                ).model_dump(mode="json"),
            }

    def evidence_page(self, session_id, phase_id, cursor, limit):
        with self.guard(session_id, phase_id, "read_frozen_code") as (db, _, row, _):
            stored = row.artifact_json
            cutoff = EvidenceCutoff.model_validate(stored["evidence"])
            after = 0
            if cursor is not None:
                scope, separator, position = cursor.partition(":")
                if (
                    scope != cutoff.coding_phase_id
                    or not separator
                    or not position.isascii()
                    or not position.isdecimal()
                ):
                    raise FlowError("EVIDENCE_CURSOR_INVALID", 422)
                after = int(position)
            items, following = evidence.page(db, cutoff, after=after, limit=limit)
            return {
                "sha256": stored["sha256"],
                "coding_phase_id": cutoff.coding_phase_id,
                "completeness": cutoff.completeness,
                "reason": cutoff.reason,
                "items": items,
                "next_cursor": (
                    f"{cutoff.coding_phase_id}:{following}"
                    if following is not None
                    else None
                ),
            }

    def seal(self, context, definition, operation_id):
        with self._locked(context.session_id) as (_, state, row):
            self._operation(state, context, operation_id, "seal")
            if row is None or row.status not in ("frozen", "sealed") or row.sandbox_id:
                raise FlowError("WORKSPACE_NOT_FROZEN", 503)
            if definition.submission_kind == "text":
                row.status = "sealed"
                return None
            if row.artifact_json is None:
                if "read_frozen_code" in state.phase.definition.capabilities:
                    raise FlowError("FROZEN_SNAPSHOT_MISSING", 503)
                self._capture_snapshot(row, state)
            self._read_snapshot(row)
            stored = row.artifact_json
            row.status = "sealed"
            return Artifact(
                workspace_id=row.workspace_id,
                evidence=stored.get("evidence"),
                reference="stage-snapshot:" + row.workspace_id + ":" + stored["sha256"],
                sha256=stored["sha256"],
            )

    @contextmanager
    def guard(self, session_id, phase_id, capability, *, epoch=None):
        with self._locked(session_id) as (db, state, row):
            phase = state.phase
            if (
                phase.id != phase_id
                or phase.status != "active"
                or phase.deadline_at is None
                or phase.deadline_at <= int(self.clock())
            ):
                raise FlowError("PHASE_CLOSED")
            if capability not in phase.definition.capabilities:
                raise FlowError("CAPABILITY_DENIED", 403)
            if (
                row is None
                or row.status != "active"
                or row.current_phase_id != phase_id
                or row.permission_epoch != phase.permission_epoch
                or epoch is not None
                and epoch != phase.permission_epoch
            ):
                raise FlowError("AUTHORIZATION_REVOKED", 403)
            yield db, state, row, self._view(row, state)

    def owner(self, scope_id):
        with self.factory.begin() as db:
            row = db.get(AgentScope, scope_id)
            if row is None or row.kind != "phase":
                raise FlowError("PHASE_NOT_FOUND", 404)
            return row.session_id

    def agent_access(self, session_id, phase_id):
        with self.guard(session_id, phase_id, "use_agent"):
            return {"session_id": phase_id}

    def runtime_for(self, phase_id):
        with self.guard(self.owner(phase_id), phase_id, "use_agent") as (
            _,
            state,
            row,
            _,
        ):
            return self._runtime(
                phase_id,
                state.phase.definition.capabilities,
                ResourceSpec.model_validate(row.spec_json),
            )

    @staticmethod
    def _observe(db, workspace_id, path, text):
        content_sha = hashlib.sha256(encode_content(text)).hexdigest()
        row = db.get(StageFileRevision, (workspace_id, path))
        if row is None:
            row = StageFileRevision(
                workspace_id=workspace_id,
                path=path,
                version=1,
                content_sha256=content_sha,
            )
            db.add(row)
            db.flush()
        elif row.content_sha256 != content_sha:
            row.content_sha256, row.version = content_sha, row.version + 1
        return row.version

    @staticmethod
    def _versions(db, workspace_id, paths):
        return {
            row.path: (row.version, row.content_sha256)
            for row in db.scalars(
                select(StageFileRevision).where(
                    StageFileRevision.workspace_id == workspace_id,
                    StageFileRevision.path.in_(paths),
                )
            )
        }

    def _read_contents(self, session_id, phase_id, paths=None):
        # The sandbox operation can be slow. Do not hold the phase locks while
        # waiting, and adopt the result only against the same live binding.
        for _ in range(2):
            with self.guard(session_id, phase_id, "read_repository") as (
                db,
                state,
                row,
                view,
            ):
                selected = view.allowed_files if paths is None else paths
                for path in selected:
                    validate_file(view, path)
                if "read_frozen_code" in state.phase.definition.capabilities:
                    contents = {
                        f.path: f.content for f in self._read_snapshot(row).files
                    }
                    return replace(view, read_only=True), {
                        path: {
                            "content": contents[path],
                            "version": self._observe(
                                db, row.workspace_id, path, contents[path]
                            ),
                        }
                        for path in selected
                    }
                workspace, epoch = row.workspace_id, state.phase.permission_epoch
                before = self._versions(db, workspace, selected)
            if paths is None:
                contents = self.sandbox.read_files(view)
            elif len(selected) > 1 and hasattr(self.sandbox, "read_paths"):
                contents = self.sandbox.read_paths(view, selected)
            else:
                contents = {
                    path: self.sandbox.read_file(view, path) for path in selected
                }
            if set(contents) != set(selected):
                raise ValueError("manifest mismatch")
            digests = {
                path: hashlib.sha256(encode_content(content)).hexdigest()
                for path, content in contents.items()
            }
            with self.guard(session_id, phase_id, "read_repository", epoch=epoch) as (
                db,
                _,
                row,
                current,
            ):
                if (
                    row.workspace_id != workspace
                    or current.sandbox_id != view.sandbox_id
                ):
                    raise FlowError("AUTHORIZATION_REVOKED", 403)
                observed = self._versions(db, workspace, selected)
                if any(
                    observed.get(path) != before.get(path)
                    and observed.get(path, (None, None))[1] != digests[path]
                    for path in selected
                ):
                    continue
                return current, {
                    path: {
                        "content": contents[path],
                        "version": self._observe(db, workspace, path, contents[path]),
                    }
                    for path in selected
                }
        raise FlowError("SOURCE_VERSION_CHANGED", 409)

    def tree(self, session_id, phase_id):
        view, contents = self._read_contents(session_id, phase_id)
        return {
            "files": [
                {
                    "path": path,
                    "is_dir": False,
                    "readonly": view.read_only or path not in view.writable_files,
                    "version": contents[path]["version"],
                }
                for path in sorted(contents)
            ]
        }

    def list_paths(self, session_id, phase_id):
        with self.guard(session_id, phase_id, "read_repository") as (_, _, _, view):
            return [
                {
                    "path": path,
                    "is_dir": False,
                    "readonly": view.read_only or path not in view.writable_files,
                }
                for path in sorted(view.allowed_files)
            ]

    def read(self, session_id, phase_id, path):
        _, contents = self._read_contents(session_id, phase_id, (path,))
        return {"path": path, **contents[path]}

    def read_many(self, session_id, phase_id, paths):
        _, contents = self._read_contents(session_id, phase_id, paths)
        return contents

    def save(self, session_id, phase_id, path, expected_version, content):
        encode_content(content)
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("positive version required")
        conflict = None
        with self.guard(session_id, phase_id, "write_repository") as (db, _, row, view):
            validate_file(view, path, write=True)
            held = db.scalar(
                select(AgentOperation.operation_id)
                .join(AgentScope, AgentScope.scope_id == AgentOperation.exam_session_id)
                .where(
                    AgentScope.workspace_id == row.workspace_id,
                    AgentOperation.holds_workspace.is_(True),
                )
                .limit(1)
            )
            if held:
                raise FlowError("WORKSPACE_BUSY")
            current = self.sandbox.read_file(view, path)
            version = self._observe(db, row.workspace_id, path, current)
            if version != expected_version:
                conflict = version
            else:
                result = self.sandbox.save_file(
                    view,
                    path,
                    hashlib.sha256(encode_content(current)).hexdigest(),
                    content,
                )
                if not result["saved"]:
                    conflict = self._observe(
                        db, row.workspace_id, path, result["content"]
                    )
                else:
                    version = self._observe(db, row.workspace_id, path, content)
        if conflict is not None:
            raise SaveConflict(conflict)
        return {"path": path, "new_version": version, "saved_at": int(self.clock())}


class PhaseWorkspace:
    supports_stage_resources = True

    def __init__(self, resources, scope_id, capabilities):
        self.resources, self.sandbox, self.phase_id = (
            resources,
            resources.sandbox,
            scope_id,
        )
        self.capabilities = capabilities

    def tree(self, scope_id):
        return self._invoke(
            lambda: self.resources.tree(self.resources.owner(scope_id), scope_id)
        )

    def list_paths(self, scope_id):
        return self._invoke(
            lambda: self.resources.list_paths(self.resources.owner(scope_id), scope_id)
        )

    def read(self, scope_id, path):
        return self._invoke(
            lambda: self.resources.read(self.resources.owner(scope_id), scope_id, path)
        )

    def read_many(self, scope_id, paths):
        return self._invoke(
            lambda: self.resources.read_many(
                self.resources.owner(scope_id), scope_id, paths
            )
        )

    @staticmethod
    def _invoke(action):
        from app.agent_integration.reading_types import ReadingError
        from app.agent_integration.store import ConversationClosed

        try:
            return action()
        except FlowError as error:
            if error.code in (
                "PHASE_CLOSED",
                "AUTHORIZATION_REVOKED",
                "PHASE_NOT_FOUND",
            ):
                raise ConversationClosed("phase access revoked") from None
            raise ReadingError(error.code) from None
        except (ValueError, UnicodeError):
            raise ReadingError("SOURCE_UNAVAILABLE") from None

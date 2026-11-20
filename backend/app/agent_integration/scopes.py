"""Resolve legacy exam scopes and phase scopes without weakening ownership."""

from sqlalchemy import select

from app.db.models import AgentScope, ExamFlowAttempt, ExamSession, StageWorkspace
from app.exam_flow.types import FlowState


def resolve_scope(db, scope_id, *, lock=False, create_legacy=False):
    from .store import ConversationNotFound

    row = db.get(AgentScope, scope_id)
    owner = row.session_id if row else scope_id
    query = select(ExamSession).where(ExamSession.session_id == owner)
    exam = db.scalar(query.with_for_update() if lock else query)
    if exam is None:
        raise ConversationNotFound("scope unavailable")
    if row is None:
        if exam.exam_mode != "single" or not create_legacy:
            raise ConversationNotFound("scope unavailable")
        row = AgentScope(
            scope_id=scope_id,
            session_id=owner,
            kind="legacy",
            status="active",
            permission_epoch=0,
        )
        db.add(row)
        db.flush()
    if row.kind == "legacy":
        return row, exam, None, None
    query = select(ExamFlowAttempt).where(ExamFlowAttempt.session_id == owner)
    flow_row = db.scalar(query.with_for_update() if lock else query)
    if flow_row is None:
        raise ConversationNotFound("scope unavailable")
    try:
        state = FlowState.model_validate(flow_row.state_json)
    except ValueError:
        from .store import StoredStateInvalid

        raise StoredStateInvalid("flow state unavailable") from None
    query = select(StageWorkspace).where(
        StageWorkspace.workspace_id == row.workspace_id
    )
    workspace = db.scalar(query.with_for_update() if lock else query)
    if (
        workspace is None
        or workspace.session_id != owner
        or workspace.attempt_id != state.id
    ):
        raise ConversationNotFound("scope unavailable")
    if lock:
        db.refresh(row, with_for_update=True)
    return row, exam, state, workspace


def assert_open(db, scope_id, now, *, lock=False):
    from .store import ConversationClosed

    row, exam, state, workspace = resolve_scope(
        db, scope_id, lock=lock, create_legacy=True
    )
    if row.kind == "legacy":
        closed = (
            exam.exam_mode != "single"
            or exam.status in ("submitting", "submission_failed", "submitted")
            or exam.deadline_at is not None
            and exam.deadline_at <= now
        )
    else:
        closed = (
            row.status != "active"
            or state.phase.id != row.phase_id
            or state.stage.workspace_id != row.workspace_id
            or state.phase.status != "active"
            or state.phase.permission_epoch != row.permission_epoch
            or state.phase.deadline_at is None
            or state.phase.deadline_at <= now
            or "use_agent" not in state.phase.definition.capabilities
            or workspace.status != "active"
            or workspace.current_phase_id != row.phase_id
            or workspace.permission_epoch != row.permission_epoch
        )
    if closed:
        raise ConversationClosed("scope no longer accepts Agent operations")
    return row, exam, state, workspace


def target_view(db, scope_id):
    from app.db.lifecycle import SessionView
    from app.stage_resources.types import ResourceSpec, WorkspaceView

    row, exam, state, workspace = resolve_scope(db, scope_id)
    if row.kind == "legacy":
        return SessionView(
            **{key: getattr(exam, key) for key in SessionView.__dataclass_fields__}
        )
    phase = next(
        (p for s in state.stages for p in s.phases if p.id == row.phase_id), None
    )
    if phase is None:
        from .store import ConversationNotFound

        raise ConversationNotFound("phase unavailable")
    spec = ResourceSpec.model_validate(workspace.spec_json)
    active = (
        state.phase.id == phase.id
        and phase.status == "active"
        and row.status == "active"
    )
    return WorkspaceView(
        session_id=workspace.workspace_id,
        status="in_progress" if active else "submitting",
        task_version_id=spec.task_version,
        sandbox_id=workspace.sandbox_id,
        sandbox_resource_key=workspace.sandbox_resource_key,
        sandbox_image_ref=workspace.sandbox_image_ref,
        sandbox_backend_ref=workspace.sandbox_backend_ref,
        admitted_at=exam.admitted_at,
        started_at=phase.started_at,
        deadline_at=phase.deadline_at,
        submitted_at=None,
        sandbox_deleted_at=None,
        lifecycle_error_code=None,
        lifecycle_error_at=None,
        allowed_files=spec.files,
        writable_files=spec.writable,
        read_only=workspace.read_only,
    )


def revision_row(db, scope_id, path):
    from app.db.models import StageFileRevision, WorkspaceFileRevision

    scope = db.get(AgentScope, scope_id)
    if scope and scope.kind == "phase":
        return db.get(StageFileRevision, (scope.workspace_id, path))
    return db.get(WorkspaceFileRevision, (scope_id, path))

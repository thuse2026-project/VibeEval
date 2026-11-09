import os
from collections import defaultdict
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from .admin.accounts import load_accounts
from .admin.evaluation import router as evaluation_router
from .admin.overview import router as overview_router
from .admin.router import AuthState, current_session, router
from .admin.submissions import router as submission_router
from .agent_integration.router import router as agent_router
from .exam_flow.router import bridge as exam_flow_bridge
from .exam_flow.router import router as exam_flow_router
from .exam_flow.types import FlowError
from .exams.router import router as entry_router
from .exams.store import configured_store
from .llm.router import router as llm_router
from .stage_resources.router import (
    agent_router as stage_agent_router,
)
from .stage_resources.router import (
    router as stage_files_router,
)
from .stage_resources.router import (
    terminal_router as stage_terminal_router,
)
from .submissions.coordinator import SubmissionSandbox
from .submissions.router import router as candidate_submission_router
from .submissions.storage import SnapshotStore


def create_app(
    accounts: dict | None = None,
    entry_db: str | Path | None = None,
    entry_store=None,
    *,
    submission_sandbox: SubmissionSandbox | None = None,
    snapshot_store: SnapshotStore | None = None,
    agent_enabled: bool | None = None,
    agent_store=None,
    agent_reading=None,
    agent_runtime=None,
    agent_event_policy=None,
    exam_flow=None,
    stage_resources=None,
) -> FastAPI:
    app = FastAPI(title="VibeEval API", version="0.1.0")
    app.state.auth = AuthState(load_accounts() if accounts is None else accounts)
    app.state.entry = (
        entry_store if entry_store is not None else configured_store(entry_db)
    )
    app.state.entry_failures = defaultdict(list)
    app.state.submission_sandbox = submission_sandbox
    app.state.snapshot_store = snapshot_store
    app.state.agent_enabled = (
        (os.environ.get("VIBEEVAL_AGENT_ENABLED") == "1")
        if agent_enabled is None
        else agent_enabled
    )
    app.state.agent_store = agent_store
    app.state.agent_reading = agent_reading
    app.state.agent_runtime = agent_runtime
    app.state.agent_event_policy = agent_event_policy
    app.state.exam_flow = exam_flow
    app.state.stage_resources = stage_resources
    if (
        stage_resources is None
        and exam_flow is not None
        and getattr(exam_flow.resources, "supports_stage_resources", False)
    ):
        app.state.stage_resources = exam_flow.resources
    if entry_db is None:
        from .admin.submissions import get_submission_reader
        from .submissions.service import SubmissionService

        def submission_reader(request: Request, authenticated=Depends(current_session)):
            if request.query_params.get("source") == "demo":
                return None
            return SubmissionService(
                store=app.state.snapshot_store, factory=app.state.entry.factory
            )

        app.dependency_overrides[get_submission_reader] = submission_reader
    app.include_router(router)
    app.include_router(overview_router)
    app.include_router(submission_router)
    app.include_router(evaluation_router)
    app.include_router(llm_router)
    app.include_router(entry_router)
    app.include_router(candidate_submission_router)
    app.include_router(agent_router)
    app.include_router(exam_flow_bridge)
    app.include_router(exam_flow_router)
    app.include_router(stage_files_router)
    app.include_router(stage_terminal_router)
    app.include_router(stage_agent_router())

    @app.exception_handler(FlowError)
    async def examination_flow_error(request: Request, error: FlowError):
        return JSONResponse(
            {"detail": {"code": error.code}},
            status_code=error.status,
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_unavailable(request: Request, error: SQLAlchemyError):
        return JSONResponse(
            {"detail": "共享数据库暂时不可用，请稍后重试"},
            status_code=503,
            headers={"Cache-Control": "no-store"},
        )

    @app.middleware("http")
    async def private_responses(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(
            ("/api/v1/admin", "/api/v1/sessions", "/api/v2")
        ):
            response.headers["Cache-Control"] = "no-store"
        return response

    return app


app = create_app()

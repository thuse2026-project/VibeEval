from collections import defaultdict
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from .admin.accounts import load_accounts
from .admin.overview import router as overview_router
from .admin.router import AuthState, current_session, router
from .admin.submissions import router as submission_router
from .exams.router import router as entry_router
from .exams.store import configured_store


def create_app(
    accounts: dict | None = None, entry_db: str | Path | None = None, entry_store=None
) -> FastAPI:
    app = FastAPI(title="VibeEval API", version="0.1.0")
    app.state.auth = AuthState(load_accounts() if accounts is None else accounts)
    app.state.entry = (
        entry_store if entry_store is not None else configured_store(entry_db)
    )
    app.state.entry_failures = defaultdict(list)
    if entry_db is None:
        from .admin.submissions import get_submission_reader
        from .submissions.service import SubmissionService

        def submission_reader(request: Request, authenticated=Depends(current_session)):
            if request.query_params.get("source") == "demo":
                return None
            return SubmissionService(factory=app.state.entry.factory)

        app.dependency_overrides[get_submission_reader] = submission_reader
    app.include_router(router)
    app.include_router(overview_router)
    app.include_router(submission_router)
    app.include_router(entry_router)

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
        if request.url.path.startswith(("/api/v1/admin", "/api/v1/sessions")):
            response.headers["Cache-Control"] = "no-store"
        return response

    return app


app = create_app()

from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, Request

from .admin.accounts import load_accounts
from .admin.router import AuthState, router
from .exams.router import router as entry_router
from .exams.store import EntryStore


def create_app(accounts: dict | None = None, entry_db: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="VibeEval API", version="0.1.0")
    app.state.auth = AuthState(load_accounts() if accounts is None else accounts)
    app.state.entry = EntryStore(entry_db)
    app.state.entry_failures = defaultdict(list)
    app.include_router(router)
    app.include_router(entry_router)

    @app.middleware("http")
    async def private_responses(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith(("/api/v1/admin", "/api/v1/sessions")):
            response.headers["Cache-Control"] = "no-store"
        return response

    return app


app = create_app()

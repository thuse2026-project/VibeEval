from fastapi import FastAPI, Request

from .admin.accounts import load_accounts
from .admin.router import AuthState, router


def create_app(accounts: dict | None = None) -> FastAPI:
    app = FastAPI(title="VibeEval API", version="0.1.0")
    app.state.auth = AuthState(load_accounts() if accounts is None else accounts)
    app.include_router(router)

    @app.middleware("http")
    async def private_responses(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/v1/admin"):
            response.headers["Cache-Control"] = "no-store"
        return response

    return app


app = create_app()

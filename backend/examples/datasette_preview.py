"""Serve the existing R2 API and built preview on a single private origin."""

from pathlib import Path

from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.exams.postgres import PostgresEntryStore
from app.llm.config import load_settings
from examples.datasette_exam import create_app


def create_preview():
    app = create_app()
    app.state.stage_resources.agent_budget_overrides = {
        "max_model_calls": 64,
        "max_compactions": 8,
        "max_tool_calls": 128,
        "max_total_tokens": 1_500_000,
    }
    flow = app.state.exam_flow
    definition = app.state.datasette_definition

    class PreviewEntry(PostgresEntryStore):
        def admit(self, token, browser):
            admitted = super().admit(token, browser)
            flow.attach(admitted["session_id"], definition)
            return admitted

    app.state.entry = PreviewEntry(app.state.entry.factory)
    frontend = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")

    def index():
        return FileResponse(
            frontend / "index.html", headers={"Cache-Control": "no-store"}
        )

    def model_status():
        settings = load_settings()
        return {"configured": bool(settings.model and settings.api_key)}

    def entry(mode: str = ""):
        if mode == "r2":
            return index()
        return RedirectResponse("/entry?mode=r2", headers={"Cache-Control": "no-store"})

    def workspace():
        return RedirectResponse("/r2-workspace", headers={"Cache-Control": "no-store"})

    for path in ("/", "/entry"):
        app.add_api_route(path, entry, methods=["GET"], include_in_schema=False)
    app.add_api_route("/workspace", workspace, methods=["GET"], include_in_schema=False)
    app.add_api_route("/r2-workspace", index, methods=["GET"], include_in_schema=False)
    app.add_api_route(
        "/r2-preview/model", model_status, methods=["GET"], include_in_schema=False
    )
    return app

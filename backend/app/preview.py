"""Single-origin joint acceptance server; production Compose continues to use Nginx."""

from pathlib import Path

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .sandbox.http_app import create_app

app = create_app()
frontend = Path(__file__).resolve().parents[2] / "frontend" / "dist"
app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")


def index(submission_id: str = ""):
    return FileResponse(frontend / "index.html", headers={"Cache-Control": "no-store"})


for route in (
    "/",
    "/entry",
    "/workspace",
    "/admin",
    "/admin/login",
    "/admin/submissions/{submission_id}",
):
    app.add_api_route(route, index, methods=["GET"], include_in_schema=False)

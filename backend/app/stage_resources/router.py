"""Phase-bound files, PTY and the existing native Agent transport."""

import os

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from app.agent_integration.router import (
    create_reference as native_create_reference,
)
from app.agent_integration.router import reading
from app.agent_integration.router import router as native_agent_router
from app.core.security import require_browser_header
from app.exam_flow.router import identity
from app.exam_flow.types import FlowError
from app.sandbox.terminal import DockerTerminalFactory
from app.workspace.files import InvalidFilePath, ReadOnlyFile
from app.workspace.service import SaveConflict
from app.workspace.terminal import relay

from .types import StageInputReference

router = APIRouter(
    prefix="/api/v2/phases",
    dependencies=[Depends(require_browser_header)],
    tags=["阶段工作区"],
)
terminal_router = APIRouter(prefix="/api/v2/phases")


class FileEntry(BaseModel):
    path: str
    is_dir: bool
    readonly: bool
    version: int


class FileTree(BaseModel):
    files: list[FileEntry]


class FileContent(BaseModel):
    path: str
    content: str
    version: int


class FileSaved(BaseModel):
    path: str
    new_version: int
    saved_at: int


def resources(request: Request):
    selected = request.app.state.stage_resources
    if selected is None:
        raise FlowError("STAGE_RESOURCES_NOT_CONFIGURED", 503)
    return selected


def perform(action):
    try:
        return action()
    except SaveConflict as error:
        raise HTTPException(
            409, {"code": "FILE_VERSION_CONFLICT", "current_version": error.version}
        ) from None
    except ReadOnlyFile:
        raise FlowError("CAPABILITY_DENIED", 403) from None
    except (InvalidFilePath, ValueError, UnicodeError):
        raise FlowError("FILE_INPUT_INVALID", 422) from None
    except FlowError:
        raise
    except Exception:
        raise FlowError("WORKSPACE_OPERATION_UNCERTAIN", 503) from None


@router.get("/{phase_id}/files", response_model=FileTree)
def files(phase_id: str, sid=Depends(identity), gateway=Depends(resources)):
    return perform(lambda: gateway.tree(sid, phase_id))


@router.get("/{phase_id}/file", response_model=FileContent)
def read_file(
    phase_id: str, path: str, sid=Depends(identity), gateway=Depends(resources)
):
    return perform(lambda: gateway.read(sid, phase_id, path))


class SaveFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=500)
    expected_version: int = Field(strict=True, ge=1)
    content: str = Field(max_length=1048576)


@router.put("/{phase_id}/file", response_model=FileSaved)
def save_file(
    phase_id: str, body: SaveFile, sid=Depends(identity), gateway=Depends(resources)
):
    return perform(
        lambda: gateway.save(
            sid, phase_id, body.path, body.expected_version, body.content
        )
    )


@terminal_router.websocket("/{phase_id}/terminal")
async def terminal(websocket: WebSocket, phase_id: str):
    handle, code = None, 1000
    try:
        allowed = {
            s.strip()
            for s in os.environ.get("SANDBOX_TERMINAL_ORIGINS", "").split(",")
            if s.strip()
        }
        if websocket.headers.get("origin") not in allowed:
            raise FlowError("TERMINAL_ORIGIN_REJECTED", 403)
        sid = identity(websocket)
        gateway = websocket.app.state.stage_resources
        if gateway is None:
            raise FlowError("STAGE_RESOURCES_NOT_CONFIGURED", 503)
        factory = getattr(
            websocket.app.state, "stage_terminal_factory", None
        ) or DockerTerminalFactory(gateway.sandbox.settings)

        def check(operation):
            with gateway.guard(sid, phase_id, "use_terminal") as (_, _, _, view):
                return operation(view)

        def opening(view):
            nonlocal handle
            handle = factory.open(view)

        await run_in_threadpool(check, opening)
        await websocket.accept()
        await relay(websocket, handle, check, guard_output=True)
    except WebSocketDisconnect:
        return
    except (HTTPException, FlowError, ValueError, UnicodeError, KeyError):
        code = 1008
    except Exception:
        code = 1011
    finally:
        if handle is not None:
            try:
                await run_in_threadpool(handle.close)
            except Exception:
                code = 1011
        try:
            await websocket.close(code=code)
        except (RuntimeError, OSError):
            pass


def create_phase_reference(
    body: StageInputReference,
    session_id: str,
    conversation_id: str,
    reader=Depends(reading),
):
    return native_create_reference(body, session_id, conversation_id, reader)


def agent_router():
    """Preserve the native transport and schemas; session_id here is a phase scope."""
    selected = APIRouter(tags=["阶段 Agent"])
    for route in native_agent_router.routes:
        selected.add_api_route(
            route.path.replace("/api/v1/sessions/", "/api/v2/phases/", 1),
            (
                create_phase_reference
                if route.endpoint is native_create_reference
                else route.endpoint
            ),
            methods=route.methods,
            response_model=route.response_model,
            status_code=route.status_code,
            response_class=route.response_class,
            dependencies=route.dependencies,
            name="phase_" + route.name,
        )
    return selected

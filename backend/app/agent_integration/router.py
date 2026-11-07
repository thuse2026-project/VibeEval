"""Owned conversations, user references, and material inspection APIs."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.agent import create_conversation
from app.agent.session_types import SessionConflict, SessionError
from app.core.security import require_browser_header
from app.db.lifecycle import SessionLifecycle
from app.exams.router import require_candidate_session
from app.llm.config import LLMError, load_settings
from app.workspace.service import WorkspaceReader

from .material_store import MaterialStore
from .read_tools import ReadToolService
from .reading_types import InputReference, ReadingError
from .sources import SourceReader
from .store import (
    ConversationClosed,
    ConversationNotFound,
    PostgresSessionStore,
    StoredStateInvalid,
)
from .window import inspect_window

router = APIRouter(
    prefix="/api/v1/sessions/{session_id}/agent",
    tags=["Agent 会话"],
    dependencies=[Depends(require_browser_header)],
)


class CreateConversationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(default="新对话", min_length=1, max_length=200)


def repository(request: Request, owned: dict = Depends(require_candidate_session)):
    if not request.app.state.agent_enabled:
        raise HTTPException(
            403, {"code": "AGENT_DISABLED", "message": "Agent 接口尚未启用"}
        )
    configured = request.app.state.agent_store
    if configured is not None:
        return configured
    factory = getattr(request.app.state.entry, "factory", None)
    if factory is None:
        raise HTTPException(
            503,
            {"code": "AGENT_STORAGE_UNAVAILABLE", "message": "Agent 需要共享持久存储"},
        )
    return PostgresSessionStore(factory)


def result(action):
    try:
        return action()
    except ConversationNotFound:
        raise HTTPException(
            404,
            {"code": "AGENT_CONVERSATION_NOT_FOUND", "message": "对话不存在或无权访问"},
        ) from None
    except ConversationClosed:
        raise HTTPException(
            409, {"code": "AGENT_SESSION_CLOSED", "message": "考试已结束或正在交卷"}
        ) from None
    except SessionConflict:
        raise HTTPException(
            409, {"code": "AGENT_CONFLICT", "message": "对话已发生变化，请重新读取"}
        ) from None
    except SessionError:
        raise HTTPException(
            422, {"code": "AGENT_INPUT_INVALID", "message": "Agent 输入不合法"}
        ) from None
    except StoredStateInvalid:
        raise HTTPException(
            503, {"code": "AGENT_STATE_UNAVAILABLE", "message": "Agent 记录暂时不可用"}
        ) from None
    except ReadingError as error:
        status = (
            404
            if error.code == "SOURCE_UNAVAILABLE"
            else (
                409
                if error.code == "SOURCE_VERSION_CHANGED"
                else 503 if error.code.endswith("_UNAVAILABLE") else 422
            )
        )
        raise HTTPException(
            status,
            {"code": error.code, "message": "引用或材料暂不可用，请核对范围及版本"},
        ) from None
    except LLMError as error:
        raise HTTPException(
            error.status, {"code": "AGENT_MODEL_CONFIG_INVALID", "message": str(error)}
        ) from None


def reading(request: Request, store=Depends(repository)):
    configured = request.app.state.agent_reading
    if configured is not None:
        return configured
    factory = getattr(store, "factory", None)
    if factory is None:
        raise HTTPException(
            503,
            {"code": "AGENT_STORAGE_UNAVAILABLE", "message": "Agent 需要共享持久存储"},
        )
    sandbox = request.app.state.submission_sandbox
    workspace = (
        WorkspaceReader(SessionLifecycle(factory), sandbox)
        if sandbox is not None
        else None
    )
    return SourceReader(MaterialStore(factory), workspace=workspace)


@router.post("/conversations", status_code=201)
def create(body: CreateConversationInput, session_id: str, store=Depends(repository)):
    state = create_conversation(session_id)

    def action():
        store.create(state, title=body.title)
        return store.describe(session_id, state.id)

    return result(action)


@router.get("/conversations")
def conversations(
    session_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    store=Depends(repository),
):
    return result(
        lambda: store.list_conversations(session_id, offset=offset, limit=limit)
    )


@router.get("/conversations/{conversation_id}")
def conversation(session_id: str, conversation_id: str, store=Depends(repository)):
    return result(lambda: store.describe(session_id, conversation_id))


@router.get("/conversations/{conversation_id}/history")
def history(
    session_id: str,
    conversation_id: str,
    after_turn: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    store=Depends(repository),
):
    return result(
        lambda: store.history(
            session_id, conversation_id, after_turn=after_turn, limit=limit
        )
    )


@router.get("/conversations/{conversation_id}/events")
def events(
    session_id: str,
    conversation_id: str,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    store=Depends(repository),
):
    return result(
        lambda: store.events(
            session_id, conversation_id, after_sequence=after_sequence, limit=limit
        )
    )


@router.post("/conversations/{conversation_id}/references", status_code=201)
def create_reference(
    body: InputReference, session_id: str, conversation_id: str, reader=Depends(reading)
):
    return result(
        lambda: reader.create_reference(
            session_id, conversation_id, body.model_dump(mode="json")
        )
    )


@router.get("/conversations/{conversation_id}/references")
def references(
    session_id: str,
    conversation_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    reader=Depends(reading),
):
    return result(
        lambda: reader.store.references(
            session_id, conversation_id, offset=offset, limit=limit
        )
    )


@router.get("/conversations/{conversation_id}/references/{reference_id}")
def reference(
    session_id: str, conversation_id: str, reference_id: str, reader=Depends(reading)
):
    return result(
        lambda: reader.store.reference(session_id, conversation_id, reference_id)
    )


@router.get("/conversations/{conversation_id}/materials/{material_id}")
def material(
    session_id: str,
    conversation_id: str,
    material_id: str,
    offset: int = Query(default=0, ge=0),
    max_chars: int = Query(default=12000, ge=1, le=16000),
    reader=Depends(reading),
):
    return result(
        lambda: reader.store.material_view(
            session_id, conversation_id, material_id, offset=offset, max_chars=max_chars
        )
    )


@router.get("/conversations/{conversation_id}/reads")
def reads(
    session_id: str,
    conversation_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    reader=Depends(reading),
):
    return result(
        lambda: reader.store.observations(
            session_id, conversation_id, offset=offset, limit=limit
        )
    )


@router.get("/conversations/{conversation_id}/context")
def context_window(
    session_id: str,
    conversation_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    reader=Depends(reading),
):
    def action():
        state = reader.store.conversations.load(session_id, conversation_id)
        report = inspect_window(
            state, tools=ReadToolService(reader).registry, settings=load_settings()
        )
        materials = report.pop("materials")
        return {
            **report,
            "materials": materials[offset : offset + limit],
            "material_count": len(materials),
            "offset": offset,
            "has_more": offset + limit < len(materials),
        }

    return result(action)

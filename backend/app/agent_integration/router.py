"""Owned conversation metadata APIs. Model/tool dispatch is a later batch."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.agent import create_conversation
from app.agent.session_types import SessionConflict, SessionError
from app.core.security import require_browser_header
from app.exams.router import require_candidate_session

from .store import (
    ConversationClosed,
    ConversationNotFound,
    PostgresSessionStore,
    StoredStateInvalid,
)

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

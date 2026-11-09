"""Owned conversations, user references, and material inspection APIs."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.agent import create_conversation
from app.agent.session_types import SessionConflict, SessionError
from app.core.security import require_browser_header
from app.db.lifecycle import SessionLifecycle
from app.exams.router import require_candidate_session
from app.llm.config import LLMError, load_settings
from app.workspace.service import WorkspaceReader

from .event_stream import (
    MAX_SEQUENCE,
    EventStream,
    EventStreamError,
    resolve_cursor,
    threaded,
)
from .material_store import MaterialStore
from .reading_types import InputReference, ReadingError
from .runtime import RuntimeCoordinator
from .runtime_types import AnswerInput, ApprovalInput, CompactInput, RunBusy, RunInput
from .sources import SourceReader
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
    except EventStreamError as error:
        raise HTTPException(
            error.status,
            {
                "code": error.code,
                "message": "事件游标或事件记录暂不可用，请核对连接位置",
            },
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
    except RunBusy:
        raise HTTPException(
            409, {"code": "AGENT_RUN_BUSY", "message": "对话已有活动运行或工作进程"}
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


def runtime(request: Request, reader=Depends(reading)):
    configured = request.app.state.agent_runtime
    return (
        configured
        if configured is not None
        else RuntimeCoordinator(reader, settings=result(load_settings))
    )


def event_access(
    request: Request, session_id: str, owned=Depends(require_candidate_session)
):
    def check():
        if not request.app.state.agent_enabled:
            raise EventStreamError("AGENT_DISABLED", 403)
        try:
            require_candidate_session(session_id, request)
        except HTTPException:
            raise EventStreamError("AGENT_ACCESS_REVOKED", 404) from None

    return check


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


@router.get("/conversations/{conversation_id}/events/stream")
async def event_subscription(
    request: Request,
    session_id: str,
    conversation_id: str,
    after_sequence: int | None = Query(default=None, ge=0, le=MAX_SEQUENCE),
    follow: bool = True,
    store=Depends(repository),
    access=Depends(event_access),
):
    cursor = result(
        lambda: resolve_cursor(
            conversation_id, after_sequence, request.headers.get("Last-Event-ID")
        )
    )

    async def page(after, limit):
        return await threaded(
            store.event_page,
            session_id,
            conversation_id,
            after_sequence=after,
            limit=limit,
        )

    async def check():
        return await threaded(access)

    stream = EventStream(
        page,
        check,
        request.is_disconnected,
        policy=request.app.state.agent_event_policy,
    )
    try:
        initial = await stream.prepare(conversation_id, cursor)
    except Exception as error:
        # Reuse ordinary HTTP errors before headers; failures during streaming
        # become sanitized control frames and never turn into tool results.
        def failed(error=error):
            raise error

        return result(failed)
    return StreamingResponse(
        stream.body(conversation_id, cursor, initial, follow=follow),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
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
    request: Request,
    session_id: str,
    conversation_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    reader=Depends(reading),
):
    def action():
        service = request.app.state.agent_runtime or RuntimeCoordinator(
            reader, settings=load_settings()
        )
        report = service.preview(session_id, conversation_id)
        materials = report.pop("materials")
        references = report.pop("input_references")
        return {
            **report,
            "materials": materials[offset : offset + limit],
            "material_count": len(materials),
            "input_references": references[offset : offset + limit],
            "input_reference_count": len(references),
            "offset": offset,
            "has_more": offset + limit < max(len(materials), len(references)),
        }

    return result(action)


@router.post("/conversations/{conversation_id}/turns", status_code=202)
def submit_turn(
    body: RunInput, session_id: str, conversation_id: str, service=Depends(runtime)
):
    return result(lambda: service.submit(session_id, conversation_id, body))


@router.post("/conversations/{conversation_id}/compactions", status_code=202)
def submit_compaction(
    body: CompactInput, session_id: str, conversation_id: str, service=Depends(runtime)
):
    return result(
        lambda: service.submit(session_id, conversation_id, body, kind="compact")
    )


@router.get("/conversations/{conversation_id}/runs")
def runs(
    session_id: str,
    conversation_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    service=Depends(runtime),
):
    return result(
        lambda: service.store.list(
            session_id, conversation_id, offset=offset, limit=limit
        )
    )


@router.get("/conversations/{conversation_id}/runs/{run_id}")
def run(session_id: str, conversation_id: str, run_id: str, service=Depends(runtime)):
    return result(lambda: service.store.get(session_id, conversation_id, run_id))


@router.post("/conversations/{conversation_id}/runs/{run_id}/advance")
async def advance_run(
    session_id: str, conversation_id: str, run_id: str, service=Depends(runtime)
):
    try:
        return await service.advance(session_id, conversation_id, run_id)
    except Exception as error:
        return result(lambda raised=error: (_ for _ in ()).throw(raised))


@router.post("/conversations/{conversation_id}/runs/{run_id}/interrupt")
def interrupt_run(
    session_id: str, conversation_id: str, run_id: str, service=Depends(runtime)
):
    return result(lambda: service.interrupt(session_id, conversation_id, run_id))


@router.get("/conversations/{conversation_id}/operations")
def operations(
    session_id: str,
    conversation_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    service=Depends(runtime),
):
    return result(
        lambda: service.operations.list(
            session_id, conversation_id, offset=offset, limit=limit
        )
    )


@router.get("/conversations/{conversation_id}/operations/{operation_id}")
def operation(
    session_id: str, conversation_id: str, operation_id: str, service=Depends(runtime)
):
    return result(
        lambda: service.operations.observe(session_id, conversation_id, operation_id)
    )


@router.post("/conversations/{conversation_id}/operations/{operation_id}/cancel")
def cancel_operation(
    session_id: str, conversation_id: str, operation_id: str, service=Depends(runtime)
):
    return result(
        lambda: service.operations.cancel(session_id, conversation_id, operation_id)
    )


@router.get("/conversations/{conversation_id}/operations/{operation_id}/output")
def operation_output(
    session_id: str,
    conversation_id: str,
    operation_id: str,
    stream: str = Query(default="stdout", pattern="^(stdout|stderr)$"),
    offset: int = Query(default=0, ge=0),
    max_chars: int = Query(default=8000, ge=1, le=16000),
    expected_revision: int | None = Query(default=None, ge=0),
    service=Depends(runtime),
):
    return result(
        lambda: service.operations.read_output(
            session_id,
            conversation_id,
            operation_id,
            stream,
            offset=offset,
            max_chars=max_chars,
            expected_revision=expected_revision,
        )
    )


@router.get("/conversations/{conversation_id}/operations/{operation_id}/change")
def operation_change(
    session_id: str,
    conversation_id: str,
    operation_id: str,
    part: str = Query(default="diff", pattern="^(before|after|diff)$"),
    offset: int = Query(default=0, ge=0),
    max_chars: int = Query(default=8000, ge=1, le=16000),
    service=Depends(runtime),
):
    return result(
        lambda: service.operations.read_change(
            session_id,
            conversation_id,
            operation_id,
            part,
            offset=offset,
            max_chars=max_chars,
        )
    )


@router.post("/conversations/{conversation_id}/runs/{run_id}/compact")
def request_compaction(
    session_id: str, conversation_id: str, run_id: str, service=Depends(runtime)
):
    return result(
        lambda: service.store.request_compaction(session_id, conversation_id, run_id)
    )


@router.get("/conversations/{conversation_id}/runs/{run_id}/attempts")
def attempts(
    session_id: str,
    conversation_id: str,
    run_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    service=Depends(runtime),
):
    return result(
        lambda: service.store.attempts(
            session_id, conversation_id, run_id, offset=offset, limit=limit
        )
    )


@router.get("/conversations/{conversation_id}/runs/{run_id}/interactions")
def interactions(
    session_id: str, conversation_id: str, run_id: str, service=Depends(runtime)
):
    return result(
        lambda: service.store.interactions(session_id, conversation_id, run_id)
    )


@router.post(
    "/conversations/{conversation_id}/runs/{run_id}/interactions/{interaction_id}/approval"
)
def approve_interaction(
    body: ApprovalInput,
    session_id: str,
    conversation_id: str,
    run_id: str,
    interaction_id: str,
    service=Depends(runtime),
):
    return result(
        lambda: service.store.resolve(
            session_id,
            conversation_id,
            run_id,
            interaction_id,
            kind="approval",
            answer=body.model_dump(mode="json"),
        )
    )


@router.post(
    "/conversations/{conversation_id}/runs/{run_id}/interactions/{interaction_id}/answer"
)
def answer_interaction(
    body: AnswerInput,
    session_id: str,
    conversation_id: str,
    run_id: str,
    interaction_id: str,
    service=Depends(runtime),
):
    return result(
        lambda: service.store.resolve(
            session_id,
            conversation_id,
            run_id,
            interaction_id,
            kind="user_input",
            answer=body.model_dump(mode="json"),
        )
    )

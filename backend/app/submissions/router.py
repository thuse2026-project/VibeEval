"""Candidate submission entry; authorize before accessing persistence or sandboxes."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.core.security import require_browser_header
from app.db.lifecycle import LifecycleConflict, LifecycleNotFound, SessionBusy
from app.exams.router import require_candidate_session

from .coordinator import SubmissionAttemptFailed, SubmissionCoordinator
from .service import SubmissionService

router = APIRouter(
    prefix="/api/v1/sessions",
    tags=["答卷提交"],
    dependencies=[Depends(require_browser_header)],
)


class SubmissionReceipt(BaseModel):
    submission_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    snapshot_id: str = Field(min_length=1, max_length=128)
    submitted_at: int = Field(ge=0)
    status: Literal["frozen"]


def unavailable() -> HTTPException:
    return HTTPException(
        503,
        {
            "code": "SUBMISSION_UNAVAILABLE",
            "message": "交卷服务暂不可用，当前环境已保留，请稍后重试",
        },
    )


def get_submission_service(
    request: Request,
    owned_session: Annotated[dict, Depends(require_candidate_session)],
) -> SubmissionService:
    # Authentication must run before resolving DB configuration or reading receipts.
    factory = getattr(request.app.state.entry, "factory", None)
    if factory is None:
        raise unavailable()
    return SubmissionService(
        store=request.app.state.snapshot_store,
        factory=factory,
    )


@router.post("/{session_id}/submit", response_model=SubmissionReceipt, status_code=202)
def submit(
    request: Request,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9_-]+$",
        ),
    ],
    owned_session: Annotated[dict, Depends(require_candidate_session)],
    service: Annotated[SubmissionService, Depends(get_submission_service)],
):
    session_id = owned_session["session_id"]
    receipt = service.get_session_receipt(session_id)
    if receipt is not None:
        # A lost response can be recovered even while the adapter is unavailable.
        return receipt
    sandbox = request.app.state.submission_sandbox
    if sandbox is None:
        raise unavailable()
    try:
        coordinator = SubmissionCoordinator(sandbox, submissions=service)
    except TypeError:
        raise unavailable() from None
    try:
        return coordinator.submit(session_id, idempotency_key)
    except SessionBusy:
        raise HTTPException(
            409,
            {"code": "SESSION_BUSY", "message": "正在保存或提交，请稍后重试"},
            headers={"Retry-After": "1"},
        ) from None
    except LifecycleNotFound:
        raise HTTPException(
            404, {"code": "SESSION_NOT_FOUND", "message": "会话不存在或无权访问"}
        ) from None
    except LifecycleConflict:
        raise HTTPException(
            409,
            {"code": "SUBMISSION_NOT_READY", "message": "当前会话状态不能交卷"},
        ) from None
    except SubmissionAttemptFailed as error:
        raise HTTPException(
            503,
            {
                "code": error.code,
                "message": "交卷暂未完成，当前环境已保留，请使用原会话重试",
            },
        ) from None

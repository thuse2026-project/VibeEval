import os
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.security import require_browser_header
from app.db.lifecycle import (
    LifecycleConflict,
    LifecycleNotFound,
    SessionBusy,
    SessionLifecycle,
)
from app.exams.router import require_candidate_session
from app.sandbox.preparation import PreparationService
from app.workspace.files import InvalidFilePath
from app.workspace.service import RevisionStoreUnavailable, WorkspaceReader

router = APIRouter(
    prefix="/api/v1/sessions", dependencies=[Depends(require_browser_header)]
)


def execute(request, owned, prepare):
    factory = getattr(request.app.state.entry, "factory", None)
    if factory is None:
        raise HTTPException(503, "共享会话服务未配置")
    lifecycle = SessionLifecycle(factory)
    try:
        if prepare:
            service = PreparationService(
                lifecycle,
                request.app.state.submission_sandbox,
                os.environ["SANDBOX_IMAGE_REF"],
                os.environ["SANDBOX_BACKEND_REF"],
                os.environ["SANDBOX_TASK_VERSION"],
            )
            view = service.prepare(owned["session_id"])
        else:
            view = lifecycle.start_exam(owned["session_id"])
        # Never expose container IDs or internal backend configuration.
        return {
            k: asdict(view)[k]
            for k in ("session_id", "status", "started_at", "deadline_at")
        }
    except SessionBusy:
        raise HTTPException(
            409, "会话正在处理，请稍后重试", headers={"Retry-After": "1"}
        ) from None
    except LifecycleNotFound:
        raise HTTPException(404, "会话不存在") from None
    except LifecycleConflict:
        raise HTTPException(409, "当前会话状态不允许此操作") from None
    except Exception:
        raise HTTPException(503, "沙箱准备暂不可用，已保存的环境将保留") from None


@router.post("/{session_id}/prepare")
def prepare(request: Request, owned: dict = Depends(require_candidate_session)):
    return execute(request, owned, True)


@router.post("/{session_id}/start")
def start(request: Request, owned: dict = Depends(require_candidate_session)):
    return execute(request, owned, False)


def read_workspace(request, owned, path=None):
    try:
        factory = getattr(request.app.state.entry, "factory", None)
        if factory is None:
            raise RevisionStoreUnavailable()
        reader = WorkspaceReader(
            SessionLifecycle(factory), request.app.state.submission_sandbox
        )
        return (
            reader.tree(owned["session_id"])
            if path is None
            else reader.read(owned["session_id"], path)
        )
    except InvalidFilePath:
        raise HTTPException(422, "文件路径不属于当前题目") from None
    except SessionBusy:
        raise HTTPException(
            409, "会话正在处理，请稍后重试", headers={"Retry-After": "1"}
        ) from None
    except LifecycleNotFound:
        raise HTTPException(404, "会话不存在") from None
    except LifecycleConflict:
        raise HTTPException(409, "请先开始作答；考试结束后不能访问工作区") from None
    except RevisionStoreUnavailable:
        raise HTTPException(
            503,
            {"code": "FILE_REVISIONS_UNAVAILABLE", "message": "文件版本服务尚未接入"},
        ) from None
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "暂时无法读取文件，请稍后重试") from None


@router.get("/{session_id}/files")
def files(request: Request, owned: dict = Depends(require_candidate_session)):
    return read_workspace(request, owned)


@router.get("/{session_id}/file")
def file(request: Request, path: str, owned: dict = Depends(require_candidate_session)):
    return read_workspace(request, owned, path)

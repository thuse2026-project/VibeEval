import os
import re
import secrets
import time
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..core.security import require_browser_header
from .store import EntryError, RECOVERY_SECONDS

COOKIE_NAME = "vibeeval_candidate"
COOKIE_SECONDS = RECOVERY_SECONDS
router = APIRouter(prefix="/api/v1/sessions", tags=["考生入场"],
                   dependencies=[Depends(require_browser_header)])


class VerifyInput(BaseModel):
    token: str = Field(min_length=1, max_length=256)


def browser_key(request: Request) -> str | None:
    value = request.cookies.get(COOKIE_NAME, "")
    return value if re.fullmatch(r"[A-Za-z0-9_-]{43}", value) else None


def recover(request: Request) -> dict | None:
    browser = browser_key(request)
    try:
        return request.app.state.entry.current(browser) if browser else None
    except EntryError as error:
        raise HTTPException(error.status, error.message) from error


@router.get("/current")
def current(request: Request):
    browser = browser_key(request)
    session = recover(request)
    if session:
        return session
    response = JSONResponse({"detail": "尚未入场，请输入分配给你的凭证"}, status_code=401)
    if browser is None:
        response.set_cookie(
            COOKIE_NAME, secrets.token_urlsafe(32), max_age=COOKIE_SECONDS,
            httponly=True, samesite="strict", path="/api/v1/sessions",
            secure=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
        )
    return response


@router.post("/verify-token")
def verify(body: VerifyInput, request: Request):
    browser = browser_key(request)
    if browser is None:
        raise HTTPException(428, "请先检查或恢复浏览器会话，再提交入场凭证；请确认浏览器允许本站 Cookie")
    now = time.time()
    failures = request.app.state.entry_failures
    request.app.state.entry_failures = defaultdict(list, {
        key: [stamp for stamp in attempts if now - stamp < 60]
        for key, attempts in failures.items() if attempts and now - attempts[-1] < 60
    })
    failures = request.app.state.entry_failures
    address = request.client.host if request.client else "unknown"
    if len(failures[address]) >= 10:
        raise HTTPException(429, "尝试过于频繁，请最多等待 60 秒后重试", headers={"Retry-After": "60"})
    try:
        return request.app.state.entry.admit(body.token.strip(), browser)
    except EntryError as error:
        if error.status == 401:
            failures[address].append(now)
        raise HTTPException(error.status, error.message) from error


def require_candidate_session(session_id: str, request: Request) -> dict:
    session = recover(request)
    if session is None or session["session_id"] != session_id:
        raise HTTPException(404, "会话不存在或无权访问，请使用原浏览器恢复")
    return session


@router.get("/{session_id}/status")
def status(session: dict = Depends(require_candidate_session)):
    return session

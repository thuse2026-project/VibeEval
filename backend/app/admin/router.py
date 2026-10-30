"""Single-process local login. Database-backed accounts/sessions can replace this later."""

import hashlib
import os
import secrets
import time
from collections import defaultdict
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..core.security import require_browser_header
from .accounts import hash_password, verify_password

COOKIE_NAME = "vibeeval_admin"
SESSION_SECONDS = 3 * 60 * 60
router = APIRouter(prefix="/api/v1/admin", tags=["考官登录"])


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class Identity(BaseModel):
    username: str
    name: str
    expires_at: int
    server_time: int


@dataclass
class Session:
    username: str
    expires_at: int


class AuthState:
    def __init__(self, accounts: dict):
        self.accounts = accounts
        self.sessions: dict[str, Session] = {}
        self.failures: dict[str, list[float]] = defaultdict(list)
        self.dummy_hash = hash_password(secrets.token_urlsafe(24))


def state(request: Request) -> AuthState:
    return request.app.state.auth


def token_key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def current_session(request: Request, auth: AuthState = Depends(state)) -> Session:
    token = request.cookies.get(COOKIE_NAME, "")
    key = token_key(token)
    session = auth.sessions.get(key)
    if session is None or session.expires_at <= time.time():
        auth.sessions.pop(key, None)
        raise HTTPException(401, "尚未登录或登录已过期，请重新登录")
    return session


def identity(session: Session, auth: AuthState) -> Identity:
    return Identity(
        username=session.username,
        name=auth.accounts[session.username]["name"],
        expires_at=session.expires_at,
        server_time=int(time.time()),
    )


@router.post("/login", response_model=Identity, dependencies=[Depends(require_browser_header)])
def login(body: LoginInput, request: Request, response: Response, auth: AuthState = Depends(state)):
    if not auth.accounts:
        raise HTTPException(503, "尚未配置考官账号，请联系项目维护者")
    now = time.time()
    # ponytail: per-process throttling; use shared storage when deploying multiple workers.
    auth.failures = defaultdict(list, {
        key: [stamp for stamp in attempts if now - stamp < 60]
        for key, attempts in auth.failures.items() if attempts and now - attempts[-1] < 60
    })
    address = request.client.host if request.client else "unknown"
    if len(auth.failures[address]) >= 5:
        raise HTTPException(429, "尝试过于频繁，请稍后重试（最多等待 60 秒）", headers={"Retry-After": "60"})
    username = body.username.strip()
    account = auth.accounts.get(username)
    valid = verify_password(body.password, account["password_hash"] if account else auth.dummy_hash)
    if not account or not valid:
        auth.failures[address].append(now)
        raise HTTPException(401, "账号或密码错误")
    auth.failures.pop(address, None)
    auth.sessions = {key: value for key, value in auth.sessions.items() if value.expires_at > now}
    old_token = request.cookies.get(COOKIE_NAME, "")
    auth.sessions.pop(token_key(old_token), None)
    token = secrets.token_urlsafe(32)
    session = Session(username=username, expires_at=int(now) + SESSION_SECONDS)
    auth.sessions[token_key(token)] = session
    response.set_cookie(
        COOKIE_NAME, token, max_age=SESSION_SECONDS, httponly=True,
        secure=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
        samesite="strict", path="/api/v1/admin",
    )
    return identity(session, auth)


@router.get("/me", response_model=Identity)
def me(session: Session = Depends(current_session), auth: AuthState = Depends(state)):
    return identity(session, auth)


@router.post("/logout", status_code=204, dependencies=[Depends(require_browser_header)])
def logout(request: Request, response: Response, auth: AuthState = Depends(state)):
    auth.sessions.pop(token_key(request.cookies.get(COOKIE_NAME, "")), None)
    response.delete_cookie(COOKIE_NAME, path="/api/v1/admin", samesite="strict")

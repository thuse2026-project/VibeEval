"""Thin candidate HTTP facade; trusted callers attach definitions through Python."""

import os
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.core.security import require_browser_header
from app.exams.router import browser_key, recover
from app.exams.store import RECOVERY_SECONDS, EntryError

from .api_types import (
    ERROR_RESPONSES,
    AttemptView,
    EventPage,
    QuestionList,
    StageProgress,
)
from .types import Draft, FlowError, Identifier, Receipt

COOKIE_NAME = "vibeeval_exam_flow"
bridge = APIRouter(
    dependencies=[Depends(require_browser_header)], responses=ERROR_RESPONSES
)
router = APIRouter(
    prefix="/api/v2",
    tags=["多阶段考试"],
    dependencies=[Depends(require_browser_header)],
    responses=ERROR_RESPONSES,
)


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_state_version: int = Field(strict=True, ge=0)
    request_key: Identifier


class Submit(Command):
    summary: str | None = Field(default=None, max_length=10000)


class Save(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(strict=True, ge=0)
    value: JsonValue


class SaveText(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(strict=True, ge=0)
    text: str = Field(max_length=20000)


def service(request: Request):
    value = request.app.state.exam_flow
    if value is None:
        raise FlowError("EXAM_FLOW_NOT_CONFIGURED", 503)
    return value


def identity(request: Request):
    token = request.cookies.get(COOKIE_NAME, "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise FlowError("CANDIDATE_NOT_AUTHENTICATED", 401)
    try:
        owned = request.app.state.entry.current(token)
    except EntryError as error:
        raise HTTPException(error.status, error.message) from None
    if owned is None:
        raise FlowError("CANDIDATE_NOT_AUTHENTICATED", 401)
    return owned["session_id"]


@bridge.post("/api/v1/sessions/{session_id}/exam-access", response_model=AttemptView)
def exchange(
    session_id: str, request: Request, response: Response, flow=Depends(service)
):
    owned = recover(request)
    if owned is None or owned["session_id"] != session_id:
        raise FlowError("SESSION_NOT_FOUND", 404)
    value = flow.current(session_id)
    response.set_cookie(
        COOKIE_NAME,
        browser_key(request),
        path="/api/v2",
        max_age=RECOVERY_SECONDS,
        httponly=True,
        samesite="strict",
        secure=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
    )
    return value


@router.get("/attempts/current", response_model=AttemptView)
def current(sid=Depends(identity), flow=Depends(service)):
    return flow.current(sid)


def check_attempt(flow, sid, attempt_id):
    if flow.current(sid)["attempt_id"] != attempt_id:
        raise FlowError("ATTEMPT_NOT_FOUND", 404)


@router.post("/attempts/{attempt_id}/prepare", response_model=AttemptView)
def prepare(
    attempt_id: str, body: Command, sid=Depends(identity), flow=Depends(service)
):
    check_attempt(flow, sid, attempt_id)
    return flow.prepare(sid, body.expected_state_version, body.request_key)


@router.post("/attempts/{attempt_id}/start", response_model=AttemptView)
def start_attempt(
    attempt_id: str, body: Command, sid=Depends(identity), flow=Depends(service)
):
    check_attempt(flow, sid, attempt_id)
    stage_id = flow.current(sid)["stages"][0]["id"]
    return flow.start(sid, stage_id, body.expected_state_version, body.request_key)


@router.get("/stages/{stage_id}", response_model=AttemptView | StageProgress)
def stage(stage_id: str, sid=Depends(identity), flow=Depends(service)):
    value = flow.current(sid)
    if not any(s["id"] == stage_id for s in value["stages"]):
        raise FlowError("STAGE_NOT_FOUND", 404)
    if value["current_stage"]["id"] != stage_id:
        return next(s for s in value["stages"] if s["id"] == stage_id)
    return value


@router.post("/stages/{stage_id}/start", response_model=AttemptView)
def start_stage(
    stage_id: str, body: Command, sid=Depends(identity), flow=Depends(service)
):
    return flow.start(sid, stage_id, body.expected_state_version, body.request_key)


@router.post("/phases/{phase_id}/finish", response_model=AttemptView)
def finish(phase_id: str, body: Command, sid=Depends(identity), flow=Depends(service)):
    return flow.finish_phase(
        sid, phase_id, body.expected_state_version, body.request_key
    )


@router.get("/phases/{phase_id}/questions", response_model=QuestionList)
def questions(phase_id: str, sid=Depends(identity), flow=Depends(service)):
    return {"items": flow.get_questions(sid, phase_id)}


@router.get("/phases/{phase_id}/answers", response_model=dict[str, Draft])
def answers(phase_id: str, sid=Depends(identity), flow=Depends(service)):
    return flow.answers(sid, phase_id)


@router.put("/phases/{phase_id}/answers/{item_id}", response_model=Draft)
def save_answer(
    phase_id: str,
    item_id: str,
    body: Save,
    sid=Depends(identity),
    flow=Depends(service),
):
    return flow.save_answer(sid, phase_id, item_id, body.expected_revision, body.value)


@router.get("/phases/{phase_id}/notes", response_model=Draft)
def notes(phase_id: str, sid=Depends(identity), flow=Depends(service)):
    return flow.notes(sid, phase_id)


@router.put("/phases/{phase_id}/notes", response_model=Draft)
def save_notes(
    phase_id: str, body: SaveText, sid=Depends(identity), flow=Depends(service)
):
    return flow.save_notes(sid, phase_id, body.expected_revision, body.text)


@router.get("/stages/{stage_id}/summary", response_model=Draft)
def summary(stage_id: str, sid=Depends(identity), flow=Depends(service)):
    return flow.summary(sid, stage_id)


@router.put("/stages/{stage_id}/summary", response_model=Draft)
def save_summary(
    stage_id: str, body: SaveText, sid=Depends(identity), flow=Depends(service)
):
    return flow.save_summary(sid, stage_id, body.expected_revision, body.text)


@router.post("/stages/{stage_id}/submit", response_model=Receipt)
def submit(stage_id: str, body: Submit, sid=Depends(identity), flow=Depends(service)):
    return flow.submit(
        sid, stage_id, body.expected_state_version, body.request_key, body.summary
    )


@router.get("/stages/{stage_id}/submission", response_model=Receipt)
def receipt(stage_id: str, sid=Depends(identity), flow=Depends(service)):
    return flow.receipt(sid, stage_id)


@router.get("/attempts/{attempt_id}/events", response_model=EventPage)
def events(
    attempt_id: str,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=100),
    sid=Depends(identity),
    flow=Depends(service),
):
    check_attempt(flow, sid, attempt_id)
    return flow.events(sid, after, limit)

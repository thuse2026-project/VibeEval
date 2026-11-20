"""Examiner rooms backed by the existing invitation, flow and grading records."""

import base64
import hashlib
import hmac
import os
import time
from contextlib import contextmanager
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text

from app.admin import stage_evaluation as grading
from app.admin.router import current_session
from app.core.security import require_browser_header
from app.db.models import (
    AgentAttempt,
    AgentConversation,
    AgentEvent,
    AgentInteraction,
    AgentMaterial,
    AgentOperation,
    AgentScope,
    ExamFlowAttempt,
    ExaminerRoom,
    ExaminerRoomReview,
    ExamSession,
    Invitation,
)
from app.exam_flow.types import ExamDefinition, digest
from app.exams.postgres import PostgresEntryStore
from app.exams.store import digest as token_digest

router = APIRouter(
    prefix="/api/v1/admin/r2/rooms",
    dependencies=[Depends(current_session), Depends(require_browser_header)],
)


def factory(request):
    entry = request.app.state.entry
    if not isinstance(entry, PostgresEntryStore) or request.app.state.exam_flow is None:
        raise HTTPException(503, "考试房间需要 PostgreSQL 和多阶段考试服务")
    return entry.factory


def definition(request):
    state = request.app.state
    return getattr(state, "examiner_exam_definition", None) or getattr(
        state, "datasette_definition", None
    )


def token_for(room_id):
    secret = os.environ.get("VIBEEVAL_ROOM_TOKEN_KEY", "")
    if len(secret.encode()) < 32:
        raise HTTPException(503, "尚未配置考官房间凭证密钥")
    value = hmac.digest(
        secret.encode(), ("room-token-v1:" + room_id).encode(), "sha256"
    )
    return "tok_" + base64.urlsafe_b64encode(value).decode().rstrip("=")


def template_version(config):
    return digest(config.model_dump(mode="json"))


def advisory_lock(db, key):
    number = int.from_bytes(
        hashlib.sha256(key.encode()).digest()[:8], "big", signed=True
    )
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": number})


@contextmanager
def grade_write(resources, exam_id, submission_id):
    """Serialize base score writes and lock-in for managed rooms."""
    if not exam_id.startswith("room_"):
        yield
        return
    with resources.factory.begin() as db:
        advisory_lock(db, submission_id)
        if db.get(ExaminerRoomReview, submission_id):
            raise HTTPException(409, "该题复核已锁定")
        yield


def attach_room(entry, flow, admitted):
    """Apply the room's frozen definition on initial admission and recovery."""
    if flow is None or not isinstance(entry, PostgresEntryStore):
        return False
    room_id = admitted["exam_info"]["id"]
    if not room_id.startswith("room_"):
        return False
    with entry.factory() as db:
        room = db.get(ExaminerRoom, room_id)
        if room is None:
            return False
        config = ExamDefinition.model_validate(room.definition)
    if not flow.manages(admitted["session_id"]):
        flow.attach(admitted["session_id"], config)
    return True


def room_query():
    return (
        select(ExaminerRoom, Invitation, ExamSession, ExamFlowAttempt)
        .join(Invitation, ExaminerRoom.invitation_hash == Invitation.token_hash)
        .outerjoin(ExamSession, ExamSession.invitation_hash == Invitation.token_hash)
        .outerjoin(
            ExamFlowAttempt, ExamFlowAttempt.session_id == ExamSession.session_id
        )
    )


def room_row(db, room_id):
    row = db.execute(room_query().where(ExaminerRoom.room_id == room_id)).one_or_none()
    if row is None:
        raise HTTPException(404, "未找到考试房间")
    return row


def exam_status(row, now):
    _, invite, session, attempt = row
    if session is None:
        return "expired" if invite.expires_at <= now else "invited"
    status = attempt.state_json["status"] if attempt else None
    if status == "completed" and all(
        s.get("receipt") for s in attempt.state_json["stages"]
    ):
        return "completed"
    return "in_progress" if status in ("running", "completed") else "admitted"


def summary(row, now):
    room, invite, _, attempt = row
    status = exam_status(row, now)
    return {
        "id": room.room_id,
        "candidate_name": invite.candidate_name,
        "exam_title": invite.exam_title,
        "exam_status": status,
        "admission_expires_at": invite.expires_at,
        "created_at": room.created_at,
        "token_available": status == "invited",
        "stage_count": len(room.definition["stages"]),
        "error_code": attempt.state_json.get("error_code") if attempt else None,
    }


class CreateRoom(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    candidate_name: str = Field(min_length=1, max_length=80)
    admission_expires_at: int
    template_version: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


@router.get("")
def list_rooms(
    request: Request,
    status: str = "",
    q: str = Query("", max_length=80),
    cursor: str = Query("", max_length=120),
    limit: int = Query(20, ge=1, le=100),
):
    from sqlalchemy import tuple_

    now = int(time.time())
    config = definition(request)
    with factory(request)() as db:
        query = room_query()
        if q:
            query = query.where(Invitation.candidate_name.icontains(q, autoescape=True))
        if cursor:
            previous = db.get(ExaminerRoom, cursor)
            if previous is None:
                raise HTTPException(422, "房间分页位置已失效")
            query = query.where(
                tuple_(ExaminerRoom.created_at, ExaminerRoom.room_id)
                < (previous.created_at, previous.room_id)
            )
        query = query.order_by(
            ExaminerRoom.created_at.desc(), ExaminerRoom.room_id.desc()
        )
        rows = []
        for row in db.execute(query.execution_options(yield_per=100)):
            item = summary(row, now)
            if not status or item["exam_status"] == status:
                rows.append(item)
            if len(rows) > limit:
                break
    return {
        "items": rows[:limit],
        "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
        "meta": {
            "current_template": (
                {
                    "title": config.title,
                    "version": template_version(config),
                    "stage_count": len(config.stages),
                    "can_create": len(
                        os.environ.get("VIBEEVAL_ROOM_TOKEN_KEY", "").encode()
                    )
                    >= 32,
                }
                if config
                else None
            )
        },
    }


@router.post("")
def create_room(
    body: CreateRoom,
    request: Request,
    response: Response,
    examiner=Depends(current_session),
):
    now = int(time.time())
    with factory(request).begin() as db:
        advisory_lock(db, "room-create:" + examiner.username + ":" + body.request_key)
        previous = db.scalar(
            select(ExaminerRoom).where(
                ExaminerRoom.creator == examiner.username,
                ExaminerRoom.request_key == body.request_key,
            )
        )
        if previous is None:
            config = definition(request)
            if config is None or template_version(config) != body.template_version:
                raise HTTPException(409, "试卷版本有变，请刷新并核对后创建")
            if not now < body.admission_expires_at <= now + 365 * 86400:
                raise HTTPException(422, "入场截止须在未来一年内")
            room_id = "room_" + uuid4().hex
            raw = token_for(room_id)
            fingerprint = token_digest(raw)
            duration = sum(p.duration_seconds for s in config.stages for p in s.phases)
            db.add(
                Invitation(
                    token_hash=fingerprint,
                    candidate_id=room_id,
                    candidate_name=body.candidate_name,
                    exam_id=room_id,
                    exam_title=config.title[:120],
                    duration_minutes=min(1440, (duration + 59) // 60),
                    expires_at=body.admission_expires_at,
                )
            )
            db.flush()
            db.add(
                ExaminerRoom(
                    room_id=room_id,
                    invitation_hash=fingerprint,
                    creator=examiner.username,
                    request_key=body.request_key,
                    created_at=now,
                    definition=config.model_dump(mode="json"),
                )
            )
            db.flush()
        else:
            room_id = previous.room_id
        row = room_row(db, room_id)
        room, invite, _, _ = row
        if (invite.candidate_name, invite.expires_at, digest(room.definition)) != (
            body.candidate_name,
            body.admission_expires_at,
            body.template_version,
        ):
            raise HTTPException(409, "创建请求已用于其他房间内容")
        item = summary(row, now)
        token = visible_token(row) if item["token_available"] else None
    response.headers["Cache-Control"] = "no-store"
    return {"room": item, "entry_url": "/entry?mode=r2", "token": token}


def visible_token(row):
    room, invite, _, _ = row
    status = exam_status(row, int(time.time()))
    if status != "invited":
        raise HTTPException(
            410 if status == "expired" else 409, "入场凭证已过期或已认领"
        )
    value = token_for(room.room_id)
    if not hmac.compare_digest(token_digest(value), invite.token_hash):
        raise HTTPException(503, "凭证密钥已变更，原凭证无法回显")
    return value


@router.post("/{room_id}/token")
def get_token(room_id: str, request: Request, response: Response):
    with factory(request)() as db:
        value = visible_token(room_row(db, room_id))
    response.headers["Cache-Control"] = "no-store"
    return {"token": value, "entry_url": "/entry?mode=r2"}


def find_stage(row, stage_id):
    attempt = row[3]
    if exam_status(row, int(time.time())) != "completed":
        raise HTTPException(409, "整场考试尚未完成")
    stage = next((s for s in attempt.state_json["stages"] if s["id"] == stage_id), None)
    if stage is None or not stage.get("receipt"):
        raise HTTPException(404, "本房间没有该答卷")
    return stage


@router.get("/{room_id}")
def detail(room_id: str, request: Request):
    with factory(request)() as db:
        row = room_row(db, room_id)
        item = summary(row, int(time.time()))
        attempt = row[3]
        stages = attempt.state_json["stages"] if attempt else []
        return {
            **item,
            "stages": [
                {
                    "id": s["id"],
                    "title": s["definition"]["title"],
                    "receipt": s.get("receipt"),
                    "decision": (
                        decision.decision
                        if (
                            s.get("receipt")
                            and (
                                decision := db.get(
                                    ExaminerRoomReview, s["receipt"]["id"]
                                )
                            )
                        )
                        else None
                    ),
                }
                for s in stages
            ],
        }


def scores(request, stage):
    if stage["receipt"]["kind"] != "code":
        return {"state": "pending"}
    try:
        report = grading.detail(request, stage["receipt"]["id"])
    except HTTPException as error:
        if error.status_code not in (409, 503):
            raise
        return {"state": "unavailable", "message": error.detail}
    objective, review = report["objective"], report["review"]
    basis = {
        "objective": objective,
        "review": review,
        "rule_version": report["rule_version"],
    }
    subjective = sum(review["scores"].values()) if review else None
    return {
        **basis,
        "state": "scored" if objective and review else "pending",
        "subjective": subjective,
        "revision": digest(basis),
        "changes": report["changes"],
        "manual_criteria": report["manual_criteria"],
        "maximum": report["manual_max"],
        "last_attempt": report["last_attempt"],
    }


@router.get("/{room_id}/stages/{stage_id}")
def stage_detail(room_id: str, stage_id: str, request: Request):
    with factory(request)() as db:
        stage = find_stage(room_row(db, room_id), stage_id)
        saved = db.get(ExaminerRoomReview, stage["receipt"]["id"])
        return {
            "title": stage["definition"]["title"],
            "receipt": stage["receipt"],
            "submission": stage["submission"],
            "grading": saved.decision["basis"] if saved else scores(request, stage),
            "decision": saved.decision if saved else None,
        }


class Preview(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)
    score_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    delta: int = Field(ge=-10, le=10)


def quote(report, body):
    if report["state"] != "scored" or body.score_revision != report["revision"]:
        raise HTTPException(409, "基础评分未完成或已更新，请刷新核对")
    original = report["subjective"]
    if not 0 <= original + body.delta <= report["maximum"]:
        raise HTTPException(422, "调分后主观得分超出本题范围")
    objective = report["objective"]["score"]
    cap = report["objective"].get("cap")
    raw = round(objective + original + body.delta, 2)
    return {
        "objective": objective,
        "original_subjective": original,
        "subjective": original + body.delta,
        "raw": raw,
        "cap": cap,
        "final": min(raw, cap) if cap is not None else raw,
    }


@router.post("/{room_id}/stages/{stage_id}/review-preview")
def preview_review(room_id: str, stage_id: str, body: Preview, request: Request):
    with factory(request)() as db:
        stage = find_stage(room_row(db, room_id), stage_id)
        if db.get(ExaminerRoomReview, stage["receipt"]["id"]):
            raise HTTPException(409, "该题复核已锁定")
    return quote(scores(request, stage), body)


class Decision(Preview):
    action: str = Field(pattern=r"^(confirm|adjust)$")
    reason: str = Field(default="", max_length=2000)
    request_key: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


@router.post("/{room_id}/stages/{stage_id}/review")
def lock_review(
    room_id: str,
    stage_id: str,
    body: Decision,
    request: Request,
    examiner=Depends(current_session),
):
    if (
        body.action == "confirm"
        and body.delta != 0
        or body.action == "adjust"
        and (not body.delta or not body.reason)
    ):
        raise HTTPException(422, "确认原分的差值须为零，调分须填写理由")
    with factory(request).begin() as db:
        stage = find_stage(room_row(db, room_id), stage_id)
        sid = stage["receipt"]["id"]
        advisory_lock(db, sid)
        saved = db.get(ExaminerRoomReview, sid)
        fingerprint = digest(body.model_dump())
        if saved:
            if (
                saved.decision["fingerprint"] == fingerprint
                and saved.decision["examiner"] == examiner.username
            ):
                return saved.decision
            raise HTTPException(409, "该题复核已锁定")
        basis = scores(request, stage)
        value = {
            "score": quote(basis, body),
            "basis": basis,
            "examiner": examiner.username,
            "at": int(time.time()),
            "fingerprint": fingerprint,
            **body.model_dump(),
        }
        db.add(ExaminerRoomReview(submission_id=sid, room_id=room_id, decision=value))
    return value


def scoped_conversations(stage):
    phase_ids = [p["id"] for p in stage["phases"]]
    return (
        select(AgentConversation)
        .join(AgentScope, AgentScope.scope_id == AgentConversation.exam_session_id)
        .where(
            AgentScope.workspace_id == stage["workspace_id"],
            AgentScope.phase_id.in_(phase_ids),
            AgentScope.kind == "phase",
        )
    )


@router.get("/{room_id}/stages/{stage_id}/timeline")
def timeline(
    room_id: str,
    stage_id: str,
    request: Request,
    cursor: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    conversation_id: str | None = None,
    around_event_id: str | None = Query(None, max_length=300),
):
    with factory(request)() as db:
        stage = find_stage(room_row(db, room_id), stage_id)
        conversations = db.scalars(scoped_conversations(stage)).all()
        entries = []
        for conv in conversations:
            if conversation_id and conv.conversation_id != conversation_id:
                continue
            cid = conv.conversation_id
            events = db.scalars(
                select(AgentEvent)
                .where(AgentEvent.conversation_id == cid)
                .order_by(AgentEvent.sequence)
            ).all()
            queued = {
                e.data_json.get("turn_id"): e for e in events if e.kind == "run.queued"
            }
            model_times = dict(
                db.execute(
                    select(AgentAttempt.step_id, AgentAttempt.finished_at)
                    .where(
                        AgentAttempt.conversation_id == cid,
                        AgentAttempt.kind == "model",
                        AgentAttempt.status == "adopted",
                        AgentAttempt.step_id.is_not(None),
                        AgentAttempt.finished_at.is_not(None),
                    )
                    .order_by(AgentAttempt.finished_at)
                ).all()
            )
            operations = db.scalars(
                select(AgentOperation).where(AgentOperation.conversation_id == cid)
            ).all()
            interactions = db.scalars(
                select(AgentInteraction).where(AgentInteraction.conversation_id == cid)
            ).all()
            approvals = {}
            tool_approvals = {}
            for interaction in interactions:
                if interaction.kind == "approval":
                    operation_id = interaction.request_json.get("operation_id")
                    if operation_id:
                        approvals[operation_id] = interaction
                    else:
                        tool_approvals[
                            interaction.step_id, interaction.tool_call_id
                        ] = interaction
            by_step = {}
            for op in operations:
                by_step.setdefault(op.step_id, []).append(op)
            for turn_number, turn in enumerate(conv.state_json.get("turns", [])):
                event = queued.get(turn["id"])
                stamp = event.created_at if event else conv.created_at
                order = event.sequence if event else turn_number
                messages = [
                    {
                        "id": turn["id"],
                        "actor": "candidate",
                        "kind": "message",
                        "text": turn["user_message"],
                    }
                ]
                for step in turn.get("steps", []):
                    response = (step.get("response") or {}).get("message") or {}
                    if response.get("content"):
                        messages.append(
                            {
                                "id": step["id"],
                                "actor": "agent",
                                "kind": "message",
                                "text": response["content"],
                                "occurred_at": model_times.get(step["id"], stamp),
                            }
                        )
                    operation_calls = {
                        op.tool_call_id for op in by_step.get(step["id"], [])
                    }
                    names = {
                        call["id"]: call["name"]
                        for call in response.get("tool_calls", [])
                    }
                    for result in step.get("tool_results", []):
                        call_id = result["tool_call_id"]
                        if call_id not in operation_calls:
                            approval = tool_approvals.get((step["id"], call_id))
                            if approval and approval.status in ("approved", "rejected"):
                                messages.append(
                                    {
                                        "id": approval.interaction_id,
                                        "actor": "candidate",
                                        "kind": "approval",
                                        "status": approval.status,
                                        "occurred_at": approval.finished_at or stamp,
                                    }
                                )
                            messages.append(
                                {
                                    "id": step["id"] + ":" + call_id,
                                    "actor": "agent",
                                    "kind": "tool",
                                    "status": (
                                        "failed"
                                        if result.get("is_error")
                                        else "succeeded"
                                    ),
                                    "tool": names.get(call_id),
                                    "occurred_at": model_times.get(step["id"], stamp),
                                }
                            )
                    for op in sorted(
                        by_step.get(step["id"], []),
                        key=lambda x: (x.created_at, x.operation_id),
                    ):
                        change = op.binding_json.get("change") or {}
                        approval = approvals.get(op.operation_id)
                        if (
                            approval
                            and approval.finished_at is not None
                            and approval.status in ("approved", "rejected")
                        ):
                            messages.append(
                                {
                                    "id": approval.interaction_id,
                                    "actor": "candidate",
                                    "kind": "approval",
                                    "status": approval.status,
                                    "occurred_at": approval.finished_at,
                                }
                            )
                        messages.append(
                            {
                                "id": op.operation_id,
                                "actor": "agent",
                                "kind": op.kind,
                                "status": op.status,
                                "approval": approval.status if approval else None,
                                "path": change.get("path"),
                                "diff_id": (
                                    op.operation_id
                                    if change.get("diff_material_id")
                                    or change.get("diff_preview")
                                    else None
                                ),
                                "exit_code": (op.receipt_json or {}).get("exit_code"),
                                "occurred_at": op.updated_at,
                            }
                        )
                for i, item in enumerate(messages):
                    item_stamp = item.get("occurred_at", stamp)
                    entries.append(
                        (
                            item_stamp,
                            cid,
                            order,
                            i,
                            {**item, "occurred_at": item_stamp, "conversation_id": cid},
                        )
                    )
        entries.sort(key=lambda entry: entry[:4])
        if around_event_id:
            position = next(
                (
                    i
                    for i, row in enumerate(entries)
                    if row[-1]["id"] == around_event_id
                ),
                None,
            )
            if position is None:
                raise HTTPException(404, "未找到该事件")
            cursor = max(0, position - limit // 2)
        page = entries[cursor : cursor + limit]
        return {
            "items": [entry[-1] for entry in page],
            "next_cursor": cursor + limit if cursor + limit < len(entries) else None,
            "conversations": [
                {"id": c.conversation_id, "title": c.title} for c in conversations
            ],
            "coverage": "persisted_agent_turns_and_operations",
        }


@router.get("/{room_id}/stages/{stage_id}/diffs/{diff_id}")
def operation_diff(
    room_id: str,
    stage_id: str,
    diff_id: str,
    request: Request,
    cursor: int = Query(0, ge=0, le=8_388_608),
):
    with factory(request)() as db:
        stage = find_stage(room_row(db, room_id), stage_id)
        op = db.scalar(
            select(AgentOperation).where(
                AgentOperation.operation_id == diff_id,
                AgentOperation.conversation_id.in_(
                    scoped_conversations(stage).with_only_columns(
                        AgentConversation.conversation_id
                    )
                ),
            )
        )
        change = op.binding_json.get("change") if op else None
        material = (
            db.scalar(
                select(AgentMaterial).where(
                    AgentMaterial.material_id == change.get("diff_material_id"),
                    AgentMaterial.exam_session_id == op.exam_session_id,
                )
            )
            if change and change.get("diff_material_id")
            else None
        )
        diff = material.content_text if material else (change or {}).get("diff_preview")
        if diff is None:
            raise HTTPException(404, "暂无该事件的差异记录")
        end = cursor + 120000
        return {
            "path": change.get("path"),
            "diff": diff[cursor:end],
            "status": op.status,
            "applied": op.status == "succeeded",
            "next_cursor": end if len(diff) > end else None,
            "truncated": material is None and bool(change.get("diff_preview_has_more")),
        }

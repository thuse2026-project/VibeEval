"""Project candidate-visible Agent facts and echoed terminal output at the coding fence."""

import hashlib
import re

from sqlalchemy import select

from app.db.models import (
    AgentAttempt,
    AgentConversation,
    AgentEvent,
    AgentInteraction,
    AgentMaterial,
    AgentOperation,
    StageTerminal,
)

from .evidence import append

TRANSCRIPT_LIMIT = 65536
ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")


def record_output(db, workspace, terminal_id, text):
    session = db.get(StageTerminal, terminal_id)
    room = TRANSCRIPT_LIMIT - len(session.transcript)
    text = text.replace("\x00", "�")
    if room > 0:
        session.transcript += text[:room]
    if len(text) > room:
        session.truncated = True
        workspace.evidence_status = "partial"


def freeze(db, phase_id):
    facts = []

    def add(source, kind, actor, stamp, **data):
        event_id = kind + "-" + hashlib.sha256(source.encode()).hexdigest()[:40]
        facts.append(dict(id=event_id, kind=kind, actor=actor, at=stamp, data=data))

    for conv in db.scalars(
        select(AgentConversation).where(AgentConversation.exam_session_id == phase_id)
    ):
        cid = conv.conversation_id
        queued = {
            e.data_json.get("turn_id"): e.created_at
            for e in db.scalars(
                select(AgentEvent).where(
                    AgentEvent.conversation_id == cid, AgentEvent.kind == "run.queued"
                )
            )
        }
        replies = dict(
            db.execute(
                select(AgentAttempt.step_id, AgentAttempt.finished_at).where(
                    AgentAttempt.conversation_id == cid,
                    AgentAttempt.kind == "model",
                    AgentAttempt.status == "adopted",
                    AgentAttempt.finished_at.is_not(None),
                )
            ).all()
        )
        for turn in conv.state_json.get("turns", []):
            stamp = queued.get(turn["id"], conv.created_at)
            add(
                cid + "/" + turn["id"],
                "message",
                "candidate",
                stamp,
                conversation_id=cid,
                text=turn["user_message"],
            )
            for step in turn.get("steps", []):
                text = ((step.get("response") or {}).get("message") or {}).get(
                    "content"
                )
                if text:
                    add(
                        cid + "/" + step["id"],
                        "message",
                        "agent",
                        replies.get(step["id"], stamp),
                        conversation_id=cid,
                        text=text,
                    )
    for approval in db.scalars(
        select(AgentInteraction).where(
            AgentInteraction.exam_session_id == phase_id,
            AgentInteraction.kind == "approval",
        )
    ):
        decided = approval.status in {"rejected", "approved"}
        add(
            approval.interaction_id,
            "approval",
            "candidate" if decided else "system",
            approval.finished_at or approval.created_at,
            status=approval.status,
            conversation_id=approval.conversation_id,
            operation_id=approval.request_json.get("operation_id"),
        )
    for op in db.scalars(
        select(AgentOperation).where(AgentOperation.exam_session_id == phase_id)
    ):
        change = op.binding_json.get("change") or {}
        data = dict(
            conversation_id=op.conversation_id,
            operation_id=op.operation_id,
            status=op.status,
        )
        if op.kind == "file_edit":
            preview = change.get("diff_preview")
            diff_id = change.get("diff_material_id")
            if diff_id:
                material = db.get(AgentMaterial, diff_id)
                if material is not None and material.exam_session_id == phase_id:
                    preview = material.content_text
            data.update(
                before_sha256=change.get("before_sha256"),
                after_sha256=change.get("after_sha256"),
                path=change.get("path") or op.args_json.get("path"),
                diff=preview[:8000] if preview else None,
                diff_truncated=bool(preview and len(preview) > 8000)
                or change.get("diff_preview_has_more", False),
            )
            add(op.operation_id, "edit", "agent", op.updated_at, **data)
        else:
            data.update(
                argv=op.args_json.get("argv"),
                exit_code=(op.receipt_json or {}).get("exit_code"),
            )
            add(op.operation_id, "command", "agent", op.updated_at, **data)
    for session in db.scalars(
        select(StageTerminal).where(StageTerminal.phase_id == phase_id)
    ):
        add(
            session.id,
            "terminal",
            "candidate",
            session.opened_at,
            session_id=session.id,
            output=ESCAPE.sub("", session.transcript).replace("\r\n", "\n"),
            truncated=session.truncated,
        )
    for fact in sorted(facts, key=lambda e: (e["at"], e["id"])):
        append(db, phase_id, fact)

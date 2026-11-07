"""Transactional run ledger around the immutable Agent core.

Every mutation locks exam -> conversation -> run. Provider I/O never holds these
locks. Prepared attempts can be resumed; dispatched attempts are never replayed.
"""

import time
import uuid
from contextlib import contextmanager

from sqlalchemy import func, select

from app.agent import (
    accept_response,
    begin_turn,
    fail_step,
    interrupt_turn,
    start_step,
    supply_tool_results,
)
from app.agent.compaction import CompactionResult
from app.agent.session import release_context_rejected_step
from app.agent.session_types import SessionConflict, SessionError
from app.db.models import AgentAttempt, AgentInteraction, AgentRun
from app.db.session import session_scope
from app.llm.config import LLMError

from .material_store import MaterialStore, call_digest
from .materials import digest, encoded, validate_json_text
from .runtime_types import LeaseLost, RunBudgetExceeded, RunBusy, RunInput, RunPolicy
from .store import ConversationClosed, ConversationNotFound
from .window import (
    adopt_window_compaction,
    compaction_manifest,
    plan_compaction,
    prepare_window_request,
)

ACTIVE = (
    "queued",
    "running",
    "waiting_approval",
    "waiting_user_input",
    "waiting_operation",
)


class RunStore:
    def __init__(self, factory, *, clock=time.time):
        self.factory, self.clock = factory, clock
        self.materials = MaterialStore(factory, clock=clock)
        self.sessions = self.materials.conversations

    @contextmanager
    def locked(self, scope, cid, run_id, *, open_required=True):
        with session_scope(self.factory) as db:
            if open_required:
                self.materials._open(db, scope, lock=True)
            else:
                # Keep the same lock order, including late receipt recording.
                from app.db.models import ExamSession

                db.scalar(
                    select(ExamSession)
                    .where(ExamSession.session_id == scope)
                    .with_for_update()
                )
            conv = self.sessions._row(db, scope, cid, lock=True)
            run = db.scalar(
                select(AgentRun)
                .where(
                    AgentRun.run_id == run_id,
                    AgentRun.conversation_id == cid,
                    AgentRun.exam_session_id == scope,
                )
                .with_for_update()
            )
            if run is None:
                raise ConversationNotFound("run unavailable")
            yield db, conv, run

    def event(self, db, conv, event_kind, run, **data):
        run.updated_at = max(run.updated_at, int(self.clock()))
        self.materials._event(
            db, conv, event_kind, {"run_id": run.run_id, "turn_id": run.turn_id, **data}
        )

    def save_state(self, db, conv, state):
        if conv.state_json == state.model_dump(mode="json"):
            return
        if state.revision != conv.revision + 1:
            raise SessionConflict("core transition must advance exactly one revision")
        conv.state_json, conv.revision = state.model_dump(mode="json"), state.revision
        conv.updated_at = max(conv.updated_at, int(self.clock()))
        self.sessions._event(db, conv, state, "conversation.updated")

    @staticmethod
    def usage(db, run):
        attempts = db.scalars(
            select(AgentAttempt).where(
                AgentAttempt.run_id == run.run_id, AgentAttempt.sent_at.is_not(None)
            )
        ).all()
        counts = [
            a.usage_json.get("total_tokens") if a.usage_json else None for a in attempts
        ]
        complete = all(type(n) is int for n in counts)
        known = sum(n for n in counts if type(n) is int)
        return {
            "known_total_tokens": known,
            "usage_complete": complete,
            "total_tokens": known if complete else None,
        }

    def view(self, db, run):
        return {
            "run_id": run.run_id,
            "conversation_id": run.conversation_id,
            "turn_id": run.turn_id,
            "kind": run.kind,
            "request_key": run.request_key,
            "status": run.status,
            "error_code": run.error_code,
            "mode": run.policy_json["runtime"]["mode"],
            "model_calls": run.model_calls,
            "compaction_calls": run.compaction_calls,
            "tool_calls": run.tool_calls,
            "compact_requested": run.compact_requested,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
            "deadline_at": run.deadline_at,
            **self.usage(db, run),
        }

    def get(self, scope, cid, run_id):
        with self.locked(scope, cid, run_id, open_required=False) as (db, _, run):
            return self.view(db, run)

    def list(self, scope, cid, *, offset=0, limit=20):
        with session_scope(self.factory) as db:
            self.sessions._row(db, scope, cid)
            rows = db.scalars(
                select(AgentRun)
                .where(AgentRun.conversation_id == cid)
                .order_by(AgentRun.created_at.desc(), AgentRun.run_id.desc())
                .offset(offset)
                .limit(limit + 1)
            ).all()
            return {
                "items": [self.view(db, r) for r in rows[:limit]],
                "offset": offset,
                "has_more": len(rows) > limit,
            }

    def active(self, scope, cid):
        with session_scope(self.factory) as db:
            self.sessions._row(db, scope, cid)
            return db.scalar(
                select(AgentRun.run_id).where(
                    AgentRun.conversation_id == cid, AgentRun.status.in_(ACTIVE)
                )
            )

    def _attachments(self, db, scope, cid, ids):
        from app.db.models import AgentInputReference

        data, remaining = [], 8000
        if len(set(ids)) != len(ids):
            raise SessionError("reference IDs must be unique")
        for ref_id in ids:
            row = db.scalar(
                select(AgentInputReference).where(
                    AgentInputReference.reference_id == ref_id,
                    AgentInputReference.conversation_id == cid,
                    AgentInputReference.exam_session_id == scope,
                )
            )
            if row is None:
                raise ConversationNotFound("reference unavailable")
            item = {
                "reference_id": ref_id,
                "reference": row.reference_json,
                "material_id": row.material_id,
            }
            if row.reference_json["kind"] == "selection" and row.material_id:
                snapshot = self.materials._material(db, scope, row.material_id)
                text = snapshot.text[: min(2000, remaining)]
                remaining -= len(text)
                item.update(
                    text=text,
                    unsaved=True,
                    source_version=snapshot.version,
                    returned_sha256=digest(text),
                    has_more=len(text) < len(snapshot.text),
                )
            data.append(item)
        context = encoded({"references": data}) if data else None
        if context is not None and len(context) > 16000:
            raise SessionError(
                "attached material exceeds input budget; attach fewer references"
            )
        return context

    def submit(self, scope, cid, body, *, policy, tools_sha256, kind="turn"):
        intent = body.model_dump(mode="json")
        validate_json_text(intent)
        intent_hash = digest(encoded({"kind": kind, "intent": intent}))
        with session_scope(self.factory) as db:
            self.materials._open(db, scope, lock=True)
            conv = self.sessions._row(db, scope, cid, lock=True)
            previous = db.scalar(
                select(AgentRun).where(
                    AgentRun.conversation_id == cid,
                    AgentRun.request_key == body.request_key,
                )
            )
            if previous:
                if previous.input_sha256 != intent_hash:
                    raise SessionConflict("request key has different input")
                return self.view(db, previous)
            if db.scalar(
                select(AgentRun.run_id).where(
                    AgentRun.conversation_id == cid, AgentRun.status.in_(ACTIVE)
                )
            ):
                raise RunBusy("conversation already has a managed run")
            state = self.sessions._state(conv)
            if kind == "turn":
                body = RunInput.model_validate(body)
                if any(t.request_key == body.request_key for t in state.turns):
                    raise SessionConflict(
                        "request key belongs to unmanaged historical input"
                    )
                context = self._attachments(db, scope, cid, body.reference_ids)
                state = begin_turn(
                    state,
                    body.text,
                    request_key=body.request_key,
                    input_context=context,
                )
                self.save_state(db, conv, state)
            elif state.active_turn and state.active_turn.status != "ready":
                raise RunBusy("compaction requires a safe boundary")
            if not (state.config.model or policy["model"]):
                raise LLMError(503, "尚未配置 LLM_MODEL")
            policy = {**policy, "model": state.config.model or policy["model"]}
            now = int(self.clock())
            run = AgentRun(
                run_id=uuid.uuid4().hex,
                conversation_id=cid,
                exam_session_id=scope,
                turn_id=state.active_turn.id if state.active_turn else None,
                request_key=body.request_key,
                input_sha256=intent_hash,
                kind=kind,
                status="queued",
                input_json=intent,
                policy_json=policy,
                tools_sha256=tools_sha256,
                model_calls=0,
                compaction_calls=0,
                tool_calls=0,
                overflow_retries=0,
                compact_requested="manual" if kind == "compact" else None,
                created_at=now,
                updated_at=now,
                deadline_at=now + policy["runtime"]["max_run_seconds"],
            )
            db.add(run)
            self.event(db, conv, "run.queued", run, kind=kind)
            db.flush()
            return self.view(db, run)

    def _lease(self, run, token):
        if (
            run.status != "running"
            or run.lease_token != token
            or run.lease_expires_at is None
            or run.lease_expires_at <= int(self.clock())
        ):
            raise LeaseLost("run lease no longer belongs to this worker")

    def _end(self, db, conv, run, status, code=None):
        state = self.sessions._state(conv)
        if (
            run.kind == "turn"
            and state.active_turn
            and state.active_turn.id == run.turn_id
        ):
            self.save_state(db, conv, interrupt_turn(state, run.turn_id))
        run.status, run.error_code = status, code
        run.lease_token = run.lease_expires_at = None
        for attempt in db.scalars(
            select(AgentAttempt).where(
                AgentAttempt.run_id == run.run_id,
                AgentAttempt.status.in_(("prepared", "running")),
            )
        ):
            attempt.status = "unknown" if attempt.sent_at is not None else "failed"
            attempt.error_code = code or "RUN_STOPPED"
        for row in db.scalars(
            select(AgentInteraction).where(
                AgentInteraction.run_id == run.run_id,
                AgentInteraction.status == "pending",
            )
        ):
            row.status, row.finished_at = "cancelled", int(self.clock())
        self.event(db, conv, "run." + status, run, error_code=code)

    def claim(self, scope, cid, run_id):
        with self.locked(scope, cid, run_id, open_required=False) as (db, conv, run):
            if run.status == "waiting_operation":
                run.status = "queued"
            if run.status in ("waiting_approval", "waiting_user_input"):
                row = db.scalar(
                    select(AgentInteraction).where(
                        AgentInteraction.run_id == run_id,
                        AgentInteraction.status.in_(("pending", "expired")),
                    )
                )
                if row and row.expires_at <= int(self.clock()):
                    row.status, row.finished_at = "expired", int(self.clock())
                    run.status = "queued"
            if run.status not in ("queued", "running"):
                return None
            now = int(self.clock())
            if (
                run.status == "running"
                and run.lease_expires_at
                and run.lease_expires_at > now
            ):
                raise RunBusy("run has a live worker")
            pending = db.scalar(
                select(AgentAttempt).where(
                    AgentAttempt.run_id == run_id, AgentAttempt.status == "running"
                )
            )
            if pending:
                pending.status, pending.error_code = "unknown", "WORKER_OUTCOME_UNKNOWN"
                self._end(db, conv, run, "unknown", "WORKER_OUTCOME_UNKNOWN")
                return None
            try:
                self.materials._open(db, scope)
                if now >= run.deadline_at:
                    raise ConversationClosed()
            except ConversationClosed:
                self._end(db, conv, run, "expired", "SESSION_CLOSED")
                return None
            token = uuid.uuid4().hex
            run.status, run.lease_token = "running", token
            run.lease_expires_at = now + run.policy_json["runtime"]["lease_seconds"]
            self.event(db, conv, "run.claimed", run)
            return token

    def snapshot(self, scope, cid, run_id, token):
        with self.locked(scope, cid, run_id) as (db, conv, run):
            self._lease(run, token)
            if int(self.clock()) >= run.deadline_at:
                raise ConversationClosed()
            run.lease_expires_at = (
                int(self.clock()) + run.policy_json["runtime"]["lease_seconds"]
            )
            return (
                self.sessions._state(conv),
                dict(run.policy_json),
                self.view(db, run),
                run.tools_sha256,
            )

    def reserve(
        self,
        scope,
        cid,
        run_id,
        token,
        *,
        kind,
        tools,
        settings,
        window_policy,
        trigger,
    ):
        with self.locked(scope, cid, run_id) as (db, conv, run):
            self._lease(run, token)
            existing = db.scalar(
                select(AgentAttempt).where(
                    AgentAttempt.run_id == run_id, AgentAttempt.status == "prepared"
                )
            )
            if existing:
                return existing.attempt_id, existing.kind, existing.request_json
            policy = RunPolicy.model_validate(run.policy_json["runtime"])
            if (
                self.usage(db, run)["known_total_tokens"] >= policy.max_total_tokens
                or kind == "model"
                and run.model_calls >= policy.max_model_calls
                or kind == "compaction"
                and run.compaction_calls >= policy.max_compactions
            ):
                raise RunBudgetExceeded()
            state = self.sessions._state(conv)
            if kind == "model":
                updated = start_step(state)
                request, manifest = prepare_window_request(
                    updated, tools=tools, settings=settings, policy=window_policy
                )
                self.save_state(db, conv, updated)
                run.model_calls += 1
            else:
                planning = (
                    window_policy.model_copy(update={"keep_recent_turns": 0})
                    if trigger in ("manual", "overflow", "agent")
                    else window_policy
                )
                request = plan_compaction(
                    state,
                    tools=tools,
                    settings=settings,
                    policy=planning,
                    trigger="threshold" if trigger == "threshold" else "manual",
                )
                if request is None:
                    return None
                manifest = compaction_manifest(request)
                run.compaction_calls += 1
            sequence = (
                db.scalar(
                    select(func.coalesce(func.max(AgentAttempt.sequence), 0)).where(
                        AgentAttempt.run_id == run_id
                    )
                )
                + 1
            )
            row = AgentAttempt(
                attempt_id=request.id if kind == "compaction" else uuid.uuid4().hex,
                run_id=run_id,
                conversation_id=cid,
                exam_session_id=scope,
                sequence=sequence,
                kind=kind,
                trigger=trigger,
                step_id=request.step_id if kind == "model" else None,
                source_revision=(
                    request.revision if kind == "model" else request.source_revision
                ),
                status="prepared",
                request_json=request.model_dump(mode="json"),
                manifest_json=manifest,
                created_at=int(self.clock()),
            )
            db.add(row)
            self.event(
                db,
                conv,
                kind + ".prepared",
                run,
                attempt_id=row.attempt_id,
                step_id=row.step_id,
            )
            db.flush()
            return row.attempt_id, kind, row.request_json

    def prepared(self, scope, cid, run_id, token):
        with self.locked(scope, cid, run_id) as (db, _, run):
            self._lease(run, token)
            row = db.scalar(
                select(AgentAttempt).where(
                    AgentAttempt.run_id == run_id, AgentAttempt.status == "prepared"
                )
            )
            return (row.attempt_id, row.kind, row.request_json) if row else None

    def dispatch(self, scope, cid, run_id, token, attempt_id):
        with self.locked(scope, cid, run_id) as (db, conv, run):
            self._lease(run, token)
            if int(self.clock()) >= run.deadline_at:
                raise ConversationClosed()
            row = db.get(AgentAttempt, attempt_id)
            if row is None or row.run_id != run_id or row.status != "prepared":
                raise SessionConflict("attempt was already dispatched")
            if self.sessions._state(conv).revision != row.source_revision:
                raise SessionConflict("prepared model source changed")
            row.status, row.sent_at = "running", int(self.clock())
            if row.manifest_json:
                row.manifest_json = {**row.manifest_json, "stage": "dispatched"}
            run.lease_expires_at = (
                int(self.clock()) + run.policy_json["runtime"]["lease_seconds"]
            )
            self.event(db, conv, row.kind + ".dispatched", run, attempt_id=attempt_id)

    def finish(
        self,
        scope,
        cid,
        run_id,
        token,
        attempt_id,
        *,
        response,
        usage,
        error_code,
        uncertain=False,
        context_limit=False,
        tools,
        settings,
        window_policy,
    ):
        from app.agent.compaction import PreparedCompaction

        with self.locked(scope, cid, run_id, open_required=False) as (db, conv, run):
            attempt = db.get(AgentAttempt, attempt_id)
            if attempt is None or attempt.run_id != run_id:
                raise ConversationNotFound()
            if attempt.finished_at is not None:
                return self.view(db, run)
            attempt.response_json = (
                response.model_dump(mode="json") if response else None
            )
            attempt.usage_json, attempt.finished_at = usage, int(self.clock())
            attempt.error_code = error_code
            state = self.sessions._state(conv)
            try:
                self._lease(run, token)
                self.materials._open(db, scope)
                if (
                    int(self.clock()) >= run.deadline_at
                    or state.revision != attempt.source_revision
                ):
                    raise LeaseLost()
            except (LeaseLost, ConversationClosed) as stale:
                attempt.status = "late"
                if run.status == "running" and run.lease_token == token:
                    self._end(
                        db,
                        conv,
                        run,
                        (
                            "expired"
                            if isinstance(stale, ConversationClosed)
                            or int(self.clock()) >= run.deadline_at
                            else "unknown"
                        ),
                        "LATE_MODEL_RECEIPT",
                    )
                self.event(db, conv, attempt.kind + ".late", run, attempt_id=attempt_id)
                return self.view(db, run)
            if context_limit and attempt.kind == "model" and run.overflow_retries == 0:
                self.save_state(
                    db, conv, release_context_rejected_step(state, attempt.step_id)
                )
                run.overflow_retries, run.compact_requested = 1, "overflow"
                attempt.status = "rejected"
            elif error_code or response is None:
                attempt.status = "unknown" if uncertain else "failed"
                if attempt.kind == "model":
                    self.save_state(
                        db,
                        conv,
                        fail_step(
                            state,
                            attempt.step_id,
                            error_code=error_code or "MODEL_RESPONSE_INVALID",
                        ),
                    )
                self._end(
                    db,
                    conv,
                    run,
                    (
                        "unknown"
                        if uncertain
                        else "capacity_exceeded" if context_limit else "failed"
                    ),
                    error_code,
                )
            elif attempt.kind == "model":
                updated = accept_response(state, attempt.step_id, response)
                self.save_state(db, conv, updated)
                attempt.status = "adopted"
                if updated.active_turn is None:
                    final = updated.turns[-1]
                    run.status = (
                        "completed" if final.status == "completed" else "failed"
                    )
                    run.error_code = final.steps[-1].error_code
                    if run.status == "completed" and run.compact_requested:
                        run.status = "running"
                    else:
                        run.lease_token = run.lease_expires_at = None
                        self.event(
                            db,
                            conv,
                            "run." + run.status,
                            run,
                            error_code=run.error_code,
                        )
            else:
                try:
                    result = CompactionResult(
                        request=PreparedCompaction.model_validate(attempt.request_json),
                        response=response,
                    )
                    updated = adopt_window_compaction(
                        state,
                        result,
                        tools=tools,
                        settings=settings,
                        policy=window_policy,
                    )
                    self.save_state(db, conv, updated)
                except (ValueError, SessionError):
                    attempt.status, attempt.error_code = (
                        "rejected",
                        "COMPACTION_NOT_ADOPTED",
                    )
                    self._end(db, conv, run, "failed", attempt.error_code)
                else:
                    attempt.status = "adopted"
                    run.compact_requested = None
                    if run.kind == "compact":
                        self._end(db, conv, run, "completed")
            self.event(
                db,
                conv,
                attempt.kind + "." + attempt.status,
                run,
                attempt_id=attempt_id,
                error_code=attempt.error_code,
                usage=usage,
            )
            db.flush()
            return self.view(db, run)

    def stop(self, scope, cid, run_id, status, code=None, *, token=None):
        with self.locked(scope, cid, run_id, open_required=False) as (db, conv, run):
            if token is not None and run.lease_token != token:
                return self.view(db, run)
            if run.status in ACTIVE:
                for attempt in db.scalars(
                    select(AgentAttempt).where(
                        AgentAttempt.run_id == run_id,
                        AgentAttempt.status.in_(("prepared", "running")),
                    )
                ):
                    attempt.status = (
                        "unknown" if attempt.sent_at is not None else "failed"
                    )
                    attempt.error_code = code or "RUN_INTERRUPTED"
                self._end(db, conv, run, status, code)
            return self.view(db, run)

    def supply(self, scope, cid, run_id, token, step_id, result):
        with self.locked(scope, cid, run_id) as (db, conv, run):
            self._lease(run, token)
            if int(self.clock()) >= run.deadline_at:
                raise ConversationClosed()
            if run.tool_calls >= run.policy_json["runtime"]["max_tool_calls"]:
                raise RunBudgetExceeded()
            state = self.sessions._state(conv)
            updated = supply_tool_results(state, step_id, [result])
            if updated != state:
                run.tool_calls += 1
                self.save_state(db, conv, updated)

    def request_compaction(self, scope, cid, run_id, *, trigger="manual"):
        with self.locked(scope, cid, run_id) as (db, conv, run):
            if run.status not in ACTIVE:
                raise SessionConflict("run no longer accepts compaction requests")
            if run.compact_requested is None:
                run.compact_requested = trigger
                self.event(db, conv, "compaction.requested", run, trigger=trigger)
            return self.view(db, run)

    def clear_compaction(self, scope, cid, run_id, token):
        with self.locked(scope, cid, run_id) as (db, conv, run):
            self._lease(run, token)
            run.compact_requested = None
            self.event(
                db, conv, "compaction.skipped", run, reason_code="NO_ELIGIBLE_HISTORY"
            )

    @staticmethod
    def interaction_view(row):
        return {
            "interaction_id": row.interaction_id,
            "run_id": row.run_id,
            "kind": row.kind,
            "step_id": row.step_id,
            "tool_call_id": row.tool_call_id,
            "status": row.status,
            "request": row.request_json,
            "binding": row.binding_json,
            "answer": row.answer_json,
            "created_at": row.created_at,
            "expires_at": row.expires_at,
        }

    def interaction(self, scope, cid, run_id, step_id, call):
        with self.locked(scope, cid, run_id, open_required=False) as (db, _, run):
            row = db.scalar(
                select(AgentInteraction).where(
                    AgentInteraction.run_id == run_id,
                    AgentInteraction.step_id == step_id,
                    AgentInteraction.tool_call_id == call.id,
                )
            )
            if row and (
                row.call_sha256 != call_digest(call)
                or row.binding_json["policy_sha256"] != digest(encoded(run.policy_json))
            ):
                raise SessionConflict("interaction binding changed")
            if (
                row
                and row.status in ("pending", "approved")
                and row.expires_at <= int(self.clock())
            ):
                row.status, row.finished_at = "expired", int(self.clock())
            return self.interaction_view(row) if row else None

    def wait(
        self, scope, cid, run_id, token, step_id, call, *, kind, request, binding=None
    ):
        with self.locked(scope, cid, run_id) as (db, conv, run):
            self._lease(run, token)
            if int(self.clock()) >= run.deadline_at:
                raise ConversationClosed()
            state = self.sessions._state(conv)
            turn, step, actual = self.materials._call(state, step_id, call.id)
            if (
                turn is not state.active_turn
                or step.status != "awaiting_tools"
                or actual != call
            ):
                raise SessionConflict("interaction source is not pending")
            row = db.scalar(
                select(AgentInteraction).where(
                    AgentInteraction.run_id == run_id,
                    AgentInteraction.step_id == step_id,
                    AgentInteraction.tool_call_id == call.id,
                )
            )
            if row is None:
                now = int(self.clock())
                row = AgentInteraction(
                    interaction_id=uuid.uuid4().hex,
                    run_id=run_id,
                    conversation_id=cid,
                    exam_session_id=scope,
                    step_id=step_id,
                    tool_call_id=call.id,
                    kind=kind,
                    call_sha256=call_digest(call),
                    binding_json={
                        **(binding or {}),
                        "policy_sha256": digest(encoded(run.policy_json)),
                        "call_sha256": call_digest(call),
                        "scope_id": scope,
                        "version_binding": "call_arguments; unpinned reads observe live version",
                    },
                    request_json=request,
                    status="pending",
                    created_at=now,
                    expires_at=min(
                        run.deadline_at,
                        now + run.policy_json["runtime"]["interaction_seconds"],
                    ),
                )
                db.add(row)
            run.status = (
                "waiting_approval" if kind == "approval" else "waiting_user_input"
            )
            run.lease_token = run.lease_expires_at = None
            self.event(
                db,
                conv,
                "interaction.requested",
                run,
                interaction_id=row.interaction_id,
                kind=kind,
                step_id=step_id,
                tool_call_id=call.id,
            )
            db.flush()
            return self.interaction_view(row)

    def resolve(self, scope, cid, run_id, interaction_id, *, kind, answer):
        validate_json_text(answer)
        with self.locked(scope, cid, run_id) as (db, conv, run):
            row = db.scalar(
                select(AgentInteraction)
                .where(
                    AgentInteraction.interaction_id == interaction_id,
                    AgentInteraction.run_id == run_id,
                )
                .with_for_update()
            )
            if row is None or row.kind != kind:
                raise ConversationNotFound("interaction unavailable")
            target = (
                "answered"
                if kind == "user_input"
                else "approved" if answer["decision"] == "approve" else "rejected"
            )
            if row.status == target and row.answer_json == answer:
                return self.interaction_view(row)
            if row.status != "pending" or run.status not in (
                "waiting_approval",
                "waiting_user_input",
            ):
                raise SessionConflict("interaction already resolved")
            state = self.sessions._state(conv)
            turn, step, call = self.materials._call(
                state, row.step_id, row.tool_call_id
            )
            if "operation_id" in row.binding_json:
                from app.db.models import AgentOperation

                operation = db.get(AgentOperation, row.binding_json["operation_id"])
                if (
                    operation is None
                    or operation.run_id != run_id
                    or operation.binding_sha256
                    != row.binding_json["operation_binding_sha256"]
                ):
                    raise SessionConflict("operation approval binding changed")
            if (
                turn is not state.active_turn
                or step.status != "awaiting_tools"
                or call_digest(call) != row.call_sha256
                or digest(encoded(run.policy_json)) != row.binding_json["policy_sha256"]
            ):
                raise SessionConflict("interaction source changed")
            row.status = "expired" if row.expires_at <= int(self.clock()) else target
            row.answer_json, row.finished_at = answer, int(self.clock())
            run.status = "queued"
            self.event(
                db,
                conv,
                "interaction." + row.status,
                run,
                interaction_id=interaction_id,
            )
            return self.interaction_view(row)

    def interactions(self, scope, cid, run_id):
        with self.locked(scope, cid, run_id, open_required=False) as (db, _, run):
            rows = db.scalars(
                select(AgentInteraction)
                .where(AgentInteraction.run_id == run_id)
                .order_by(AgentInteraction.created_at, AgentInteraction.interaction_id)
            ).all()
            return {"items": [self.interaction_view(row) for row in rows]}

    def attempts(self, scope, cid, run_id, *, offset=0, limit=20):
        with self.locked(scope, cid, run_id, open_required=False) as (db, _, run):
            rows = db.scalars(
                select(AgentAttempt)
                .where(AgentAttempt.run_id == run_id)
                .order_by(AgentAttempt.sequence)
                .offset(offset)
                .limit(limit + 1)
            ).all()
            return {
                "items": [
                    {
                        "attempt_id": a.attempt_id,
                        "sequence": a.sequence,
                        "kind": a.kind,
                        "trigger": a.trigger,
                        "step_id": a.step_id,
                        "status": a.status,
                        "source_revision": a.source_revision,
                        "request_sha256": a.request_json["payload_sha256"],
                        "manifest": a.manifest_json,
                        "usage": a.usage_json,
                        "error_code": a.error_code,
                        "created_at": a.created_at,
                        "sent_at": a.sent_at,
                        "finished_at": a.finished_at,
                    }
                    for a in rows[:limit]
                ],
                "offset": offset,
                "has_more": len(rows) > limit,
            }

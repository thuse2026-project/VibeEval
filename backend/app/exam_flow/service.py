"""Persistent examination flow; adapters own questions, Agent work and sandboxes."""

import time
from uuid import uuid4

from .store import FlowStore
from .types import (
    Artifact,
    Draft,
    ExamDefinition,
    FlowError,
    FlowState,
    Operation,
    PhaseContext,
    PhaseState,
    PublicQuestion,
    QuestionProvider,
    Receipt,
    ResourceGateway,
    StageState,
    digest,
)


def identifier(prefix):
    return prefix + "_" + uuid4().hex


class ExamFlowService:
    def __init__(
        self,
        store: FlowStore,
        questions: QuestionProvider,
        resources: ResourceGateway,
        *,
        clock=time.time,
        retry_seconds=5,
    ):
        for adapter, names in (
            (questions, ("questions", "validate_answer")),
            (resources, ("prepare", "activate_phase", "close_phase", "seal")),
        ):
            if not all(callable(getattr(adapter, name, None)) for name in names):
                raise TypeError("Complete question and resource adapters are required")
        if not 1 <= retry_seconds <= 3600:
            raise ValueError("retry_seconds must be between 1 and 3600")
        self.store, self.questions, self.resources = store, questions, resources
        self.clock, self.retry_seconds = clock, retry_seconds

    def _now(self):
        value = int(self.clock())
        if value < 0:
            raise ValueError("server clock must be nonnegative")
        return value

    def attach(self, session_id: str, definition: ExamDefinition):
        """Trusted provisioning entry, never accept definitions from candidates."""
        definition = ExamDefinition.model_validate(definition)
        state = FlowState(
            id=identifier("attempt"),
            session_id=session_id,
            definition=definition,
            stages=[
                StageState(
                    id=identifier("stage"),
                    workspace_id=identifier("workspace"),
                    definition=stage,
                    phases=[
                        PhaseState(id=identifier("phase"), definition=p)
                        for p in stage.phases
                    ],
                )
                for stage in definition.stages
            ],
        )
        self.store.create(state)
        return self.current(session_id)

    def manages(self, session_id):
        try:
            self.store.load(session_id)
            return True
        except FlowError as error:
            if error.code != "ATTEMPT_NOT_FOUND":
                raise
            return False

    @staticmethod
    def _context(state):
        return PhaseContext(
            session_id=state.session_id,
            attempt_id=state.id,
            stage_id=state.stage.id,
            phase_id=state.phase.id,
            workspace_id=state.stage.workspace_id,
            task_version=state.stage.definition.task_version,
            permission_epoch=state.phase.permission_epoch,
            deadline_at=state.phase.deadline_at,
        )

    def _event(self, state, kind):
        state.events.append(
            {
                "sequence": len(state.events) + 1,
                "kind": kind,
                "at": self._now(),
                "stage_id": state.stage.id,
                "phase_id": state.phase.id,
            }
        )

    @staticmethod
    def _schedule(state, kind):
        state.operation = Operation(id=identifier("operation"), kind=kind)
        if kind == "open":
            state.phase.permission_epoch += 1
        state.retry_at = 0
        state.error_code = None

    @staticmethod
    def _command(state, key, action, target, expected, payload=None):
        if (
            not isinstance(key, str)
            or not key
            or len(key) > 128
            or not all(c.isascii() and (c.isalnum() or c in "_-") for c in key)
        ):
            raise FlowError("INVALID_REQUEST_KEY", 422)
        fingerprint = digest([action, target, payload])
        if key in state.commands:
            if state.commands[key] != fingerprint:
                raise FlowError("IDEMPOTENCY_CONFLICT")
            return False
        if type(expected) is not int or expected != state.revision:
            raise FlowError("STATE_VERSION_CONFLICT")
        # Commands are inserted only after validating a legal transition.
        return fingerprint

    def _begin_close(self, state, reason):
        state.phase.status = "closing"
        state.phase.close_reason = reason
        state.phase.permission_epoch += 1
        self._schedule(state, "close")
        self._event(state, "phase.closing")

    def prepare(self, session_id, expected, key):
        self.reconcile(session_id)
        with self.store.edit(session_id) as state:
            value = self._command(state, key, "prepare", state.id, expected)
            if value:
                if state.status != "admitted":
                    raise FlowError("ATTEMPT_NOT_ADMITTED")
                state.commands[key] = value
                state.status, state.stage.status = "preparing", "preparing"
                self._schedule(state, "prepare")
                self._event(state, "stage.preparing")
        return self.current(session_id)

    def start(self, session_id, stage_id, expected, key):
        self.reconcile(session_id)
        with self.store.edit(session_id) as state:
            value = self._command(state, key, "start", stage_id, expected)
            if value:
                if (
                    state.stage.id != stage_id
                    or state.stage.status != "ready"
                    or state.operation
                ):
                    raise FlowError("STAGE_NOT_READY")
                state.commands[key] = value
                state.transition_deadline_at = None
                self._schedule(state, "open")
        return self.current(session_id)

    def finish_phase(self, session_id, phase_id, expected, key):
        self.reconcile(session_id)
        with self.store.edit(session_id) as state:
            value = self._command(state, key, "finish", phase_id, expected)
            if value:
                self._active(state, phase_id)
                state.commands[key] = value
                self._begin_close(state, "manual")
        return self.current(session_id)

    def submit(self, session_id, stage_id, expected, key, summary=None):
        self.reconcile(session_id)
        with self.store.edit(session_id) as state:
            # A completed stage always returns its original receipt, including a
            # timeout/manual race; conflicting reuse of a known key still fails.
            previous = next((s for s in state.stages if s.id == stage_id), None)
            if previous is None:
                raise FlowError("STAGE_NOT_FOUND", 404)
            if previous.receipt is not None:
                known = state.commands.get(key)
                if known and known != digest(["submit", stage_id, summary]):
                    raise FlowError("IDEMPOTENCY_CONFLICT")
                return previous.receipt.model_dump(mode="json")
            value = self._command(state, key, "submit", stage_id, expected, summary)
            if value:
                if (
                    state.stage.id != stage_id
                    or state.stage.phase_index != len(state.stage.phases) - 1
                ):
                    raise FlowError("STAGE_NOT_SUBMITTABLE")
                self._active(state, state.phase.id)
                if summary is not None:
                    self._set_summary(state, summary)
                state.commands[key] = value
                self._begin_close(state, "manual")
        self.reconcile(session_id)
        return self.receipt(session_id, stage_id)

    def _active(self, state, phase_id):
        if (
            state.phase.id != phase_id
            or state.phase.status != "active"
            or (
                state.phase.deadline_at is None
                or self._now() >= state.phase.deadline_at
            )
        ):
            raise FlowError("PHASE_CLOSED")

    def _permit(self, state, phase_id, capability):
        self._active(state, phase_id)
        if capability not in state.phase.definition.capabilities:
            raise FlowError("CAPABILITY_DENIED", 403)

    def authorize(self, session_id, phase_id, capability, *, permission_epoch=None):
        """Server-only tool/file/PTY guard; adapters must also fence old grants."""
        state = self.store.load(session_id)
        self._permit(state, phase_id, capability)
        if (
            permission_epoch is not None
            and permission_epoch != state.phase.permission_epoch
        ):
            raise FlowError("AUTHORIZATION_REVOKED", 403)
        return self._context(state)

    def _questions(self, state):
        reference = state.phase.definition.question_ref
        if reference is None:
            raise FlowError("QUESTIONS_NOT_AVAILABLE", 403)
        try:
            values = [
                PublicQuestion.model_validate(q)
                for q in self.questions.questions(self._context(state), reference)
            ]
            if len(values) > 100 or len({q.id for q in values}) != len(values):
                raise ValueError("invalid public question list")
            return values
        except Exception:
            raise FlowError("QUESTION_PROVIDER_UNAVAILABLE", 503) from None

    def get_questions(self, session_id, phase_id):
        self.reconcile(session_id)
        state = self.store.load(session_id)
        self._active(state, phase_id)
        return [q.model_dump(mode="json") for q in self._questions(state)]

    def save_answer(self, session_id, phase_id, item_id, expected_revision, value):
        self.reconcile(session_id)
        # Validate bounded JSON before calling a question adapter or opening a lock.
        draft = Draft(value=value)
        if len(draft.model_dump_json().encode()) > 65536:
            raise FlowError("ANSWER_TOO_LARGE", 422)
        with self.store.edit(session_id) as state:
            self._permit(state, phase_id, "edit_answers")
            question = next(
                (q for q in self._questions(state) if q.id == item_id), None
            )
            if question is None:
                raise FlowError("QUESTION_NOT_FOUND", 404)
            try:
                self.questions.validate_answer(self._context(state), question, value)
            except ValueError:
                raise FlowError("ANSWER_INVALID", 422) from None
            except Exception:
                raise FlowError("QUESTION_PROVIDER_UNAVAILABLE", 503) from None
            current = state.phase.answers.setdefault(item_id, Draft())
            self._save(current, expected_revision, value)
            result = current.model_dump(mode="json")
        return result

    def answers(self, session_id, phase_id):
        self.reconcile(session_id)
        state = self.store.load(session_id)
        self._permit(state, phase_id, "edit_answers")
        return {
            key: value.model_dump(mode="json")
            for key, value in state.phase.answers.items()
        }

    def _save(self, draft, expected, value):
        if type(expected) is not int or expected != draft.revision:
            raise FlowError("REVISION_CONFLICT")
        draft.value, draft.saved_at = value, self._now()
        draft.revision += 1

    def save_notes(self, session_id, phase_id, expected, text):
        self.reconcile(session_id)
        if not isinstance(text, str) or len(text) > 20000:
            raise FlowError("NOTES_INVALID", 422)
        with self.store.edit(session_id) as state:
            self._permit(state, phase_id, "edit_notes")
            self._save(state.stage.notes, expected, text)
            result = state.stage.notes.model_dump(mode="json")
        return result

    def notes(self, session_id, phase_id):
        self.reconcile(session_id)
        state = self.store.load(session_id)
        self._permit(state, phase_id, "read_notes")
        return state.stage.notes.model_dump(mode="json")

    def _set_summary(self, state, text):
        if (
            not isinstance(text, str)
            or len(text) > state.stage.definition.summary_limit
        ):
            raise FlowError("SUMMARY_INVALID", 422)
        self._save(state.stage.summary, state.stage.summary.revision, text)

    def save_summary(self, session_id, stage_id, expected, text):
        self.reconcile(session_id)
        with self.store.edit(session_id) as state:
            self._active(state, state.phase.id)
            if (
                state.stage.id != stage_id
                or state.stage.phase_index != len(state.stage.phases) - 1
            ):
                raise FlowError("STAGE_NOT_SUBMITTABLE")
            if type(expected) is not int or expected != state.stage.summary.revision:
                raise FlowError("REVISION_CONFLICT")
            self._set_summary(state, text)
            result = state.stage.summary.model_dump(mode="json")
        return result

    def receipt(self, session_id, stage_id):
        state = self.store.load(session_id)
        stage = next((s for s in state.stages if s.id == stage_id), None)
        if stage is None:
            raise FlowError("STAGE_NOT_FOUND", 404)
        if stage.receipt is None:
            raise FlowError(
                (
                    "SUBMISSION_IN_PROGRESS"
                    if stage.status == "sealing"
                    or any(p.status == "closing" for p in stage.phases)
                    else "SUBMISSION_NOT_FOUND"
                ),
                409 if stage.status != "locked" else 404,
            )
        return stage.receipt.model_dump(mode="json")

    def export_submission(self, session_id, stage_id):
        """Trusted grading handoff; never mounted as a candidate HTTP endpoint."""
        state = self.store.load(session_id)
        stage = next((s for s in state.stages if s.id == stage_id), None)
        if stage is None or stage.submission is None:
            raise FlowError("SUBMISSION_NOT_FOUND", 404)
        return {
            "receipt": stage.receipt.model_dump(mode="json"),
            "content": stage.submission,
        }

    def events(self, session_id, after=0, limit=100):
        if type(after) is not int or after < 0 or not 1 <= limit <= 100:
            raise FlowError("EVENT_CURSOR_INVALID", 422)
        state = self.store.load(session_id)
        return {
            "items": state.events[after : after + limit],
            "head_sequence": len(state.events),
        }

    def _complete(self, state, result):
        kind = state.operation.kind
        stage, phase = state.stage, state.phase
        state.operation, state.error_code, state.retry_at = None, None, 0
        if kind == "prepare":
            stage.status = "ready"
            if state.stage_index == 0:
                state.status = "ready"
            else:
                state.transition_deadline_at = (
                    self._now() + state.definition.transition_seconds
                )
            self._event(state, "stage.ready")
        elif kind == "open":
            state.status, stage.status, phase.status = "running", "active", "active"
            phase.started_at = self._now()
            phase.deadline_at = phase.started_at + phase.definition.duration_seconds
            self._event(state, "phase.started")
        elif kind == "close":
            phase.status, phase.closed_at = "closed", self._now()
            self._event(state, "phase.closed")
            if stage.phase_index + 1 < len(stage.phases):
                stage.phase_index += 1
                self._schedule(state, "open")
            else:
                stage.status = "sealing"
                self._schedule(state, "seal")
        elif kind == "seal":
            content = {
                "definition": state.definition.key,
                "definition_version": state.definition.version,
                "task_version": stage.definition.task_version,
                "stage_id": stage.id,
                "workspace_id": stage.workspace_id,
                "phases": [p.model_dump(mode="json") for p in stage.phases],
                "notes": stage.notes.model_dump(mode="json"),
                "summary": stage.summary.model_dump(mode="json"),
                "artifact": None if result is None else result.model_dump(mode="json"),
            }
            stage.submission = content
            stage.receipt = Receipt(
                id=identifier("submission"),
                stage_id=stage.id,
                kind=stage.definition.submission_kind,
                content_sha256=digest(content),
                submitted_at=self._now(),
                close_reason=phase.close_reason,
            )
            stage.status = "submitted"
            self._event(state, "stage.submitted")
            if state.stage_index + 1 == len(state.stages):
                state.status = "completed"
                self._event(state, "exam.completed")
            else:
                state.stage_index += 1
                state.stage.status = "preparing"
                self._schedule(state, "prepare")

    def reconcile(self, session_id):
        """Bounded, restart-safe progress; external calls run outside DB locks."""
        for _ in range(32):
            with self.store.edit(session_id) as state:
                if state.operation is None:
                    if (
                        state.phase.status == "active"
                        and self._now() >= state.phase.deadline_at
                    ):
                        self._begin_close(state, "expired")
                    elif (
                        state.transition_deadline_at is not None
                        and self._now() >= state.transition_deadline_at
                    ):
                        state.transition_deadline_at = None
                        self._schedule(state, "open")
                    else:
                        return
                if state.retry_at > self._now():
                    return
                operation, context = state.operation, self._context(state)
                definition, phase_definition = (
                    state.stage.definition,
                    state.phase.definition,
                )
            try:
                result = None
                if operation.kind == "prepare":
                    self.resources.prepare(context, definition, operation.id)
                elif operation.kind == "open":
                    self.resources.activate_phase(
                        context, phase_definition, operation.id
                    )
                elif operation.kind == "close":
                    self.resources.close_phase(context, operation.id)
                else:
                    result = self.resources.seal(context, definition, operation.id)
                    if definition.submission_kind == "code":
                        result = Artifact.model_validate(result)
                        if result.workspace_id != context.workspace_id:
                            raise ValueError("artifact belongs to another workspace")
                    elif result is not None:
                        raise ValueError("text stage must not export a code artifact")
            except Exception:
                with self.store.edit(session_id) as current:
                    if current.operation == operation:
                        current.error_code = (
                            "RESOURCE_" + operation.kind.upper() + "_FAILED"
                        )
                        current.failure_count += 1
                        current.retry_at = self._now() + self.retry_seconds
                return
            with self.store.edit(session_id) as current:
                if current.operation == operation:
                    self._complete(current, result)

    def run_due(self, limit=100):
        """Call from the host's background scheduler, independently of browsers."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        sessions = self.store.due(self._now(), limit)
        failures = []
        for session_id in sessions:
            try:
                self.reconcile(session_id)
            except Exception:
                failures.append(session_id)
        return {"scanned": len(sessions), "failures": failures}

    def current(self, session_id):
        self.reconcile(session_id)
        state = self.store.load(session_id)
        active = (
            state.phase.status == "active" and self._now() < state.phase.deadline_at
        )
        return {
            "attempt_id": state.id,
            "status": state.status,
            "state_version": state.revision,
            "server_now": self._now(),
            "transition_deadline_at": state.transition_deadline_at,
            "error_code": state.error_code,
            "failure_count": state.failure_count,
            "current_stage": {
                "id": state.stage.id,
                "key": state.stage.definition.key,
                "ordinal": state.stage_index + 1,
                "title": state.stage.definition.title,
                "status": state.stage.status,
                "workspace_id": state.stage.workspace_id,
            },
            "current_phase": {
                "id": state.phase.id,
                "key": state.phase.definition.key,
                "title": state.phase.definition.title,
                "status": state.phase.status,
                "permission_epoch": state.phase.permission_epoch,
                "started_at": state.phase.started_at,
                "deadline_at": state.phase.deadline_at,
            },
            "capabilities": (
                sorted(state.phase.definition.capabilities) if active else []
            ),
            "stages": [
                {
                    "id": s.id,
                    "ordinal": i + 1,
                    "title": s.definition.title,
                    "status": s.status,
                    "receipt": s.receipt.model_dump(mode="json") if s.receipt else None,
                }
                for i, s in enumerate(state.stages)
            ],
        }

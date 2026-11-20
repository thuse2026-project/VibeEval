"""Explicit test doubles; production callers must supply durable resource adapters."""

from contextlib import contextmanager
from threading import RLock

from app.exam_flow import Artifact, ExamDefinition, FlowError, PublicQuestion
from app.exam_flow.types import digest


class MemoryFlowStore:
    def __init__(self):
        self.values, self.lock = {}, RLock()

    def create(self, state):
        with self.lock:
            previous = self.values.get(state.session_id)
            if previous:
                if previous.definition != state.definition:
                    raise FlowError("DEFINITION_PINNED")
                return
            self.values[state.session_id] = state.model_copy(deep=True)

    def load(self, session_id):
        with self.lock:
            if session_id not in self.values:
                raise FlowError("ATTEMPT_NOT_FOUND", 404)
            return self.values[session_id].model_copy(deep=True)

    @contextmanager
    def edit(self, session_id):
        with self.lock:
            original = self.load(session_id)
            state = original.model_copy(deep=True)
            yield state
            if state != original:
                state.revision = original.revision + 1
                self.values[session_id] = state.model_copy(deep=True)

    def due(self, now, limit=100):
        with self.lock:
            return [
                s.session_id
                for s in self.values.values()
                if s.wake_at is not None and s.wake_at <= now
            ][:limit]


class Questions:
    def __init__(self):
        self.calls = []

    def questions(self, context, reference):
        self.calls.append((context, reference))
        return [PublicQuestion(id="item", title="Provided question", body=reference)]

    def validate_answer(self, context, question, value):
        if not isinstance(value, (str, dict)) or len(str(value)) > 1500:
            raise ValueError("fixture answer does not match its schema")


class Resources:
    def __init__(self):
        self.done, self.calls, self.fail, self.lock = {}, [], {}, RLock()
        self.wrong_workspace = False

    def operation(self, kind, context, operation_id, result=None):
        with self.lock:
            self.calls.append((kind, context, operation_id))
            if self.fail.get(kind, 0):
                self.fail[kind] -= 1
                raise RuntimeError("private adapter credentials")
            return self.done.setdefault(operation_id, result)

    def prepare(self, context, definition, operation_id):
        return self.operation("prepare", context, operation_id)

    def activate_phase(self, context, definition, operation_id):
        return self.operation("open", context, operation_id)

    def close_phase(self, context, operation_id):
        return self.operation("close", context, operation_id)

    def seal(self, context, definition, operation_id):
        artifact = None
        if definition.submission_kind == "code":
            artifact = Artifact(
                workspace_id="wrong" if self.wrong_workspace else context.workspace_id,
                reference="fixture-archive-" + context.workspace_id,
                sha256=digest(context.workspace_id),
            )
        return self.operation("seal", context, operation_id, artifact)


def definition():
    exploration = dict(
        key="explore",
        title="Explore",
        duration_seconds=10,
        capabilities=["read_repository", "use_agent", "read_notes", "edit_notes"],
    )
    answering = dict(
        key="answer",
        title="Answer",
        duration_seconds=15,
        capabilities=["read_repository", "edit_answers", "read_notes"],
        question_ref="provided-questions",
    )
    review = dict(
        key="review",
        title="Review",
        duration_seconds=20,
        capabilities=["read_repository", "edit_answers"],
        question_ref="provided-review",
    )
    coding = dict(
        key="coding",
        title="Code",
        duration_seconds=30,
        capabilities=[
            "read_repository",
            "write_repository",
            "use_terminal",
            "use_agent",
        ],
    )
    return ExamDefinition.model_validate(
        dict(
            key="external-exam",
            version="v1",
            title="External exam",
            transition_seconds=60,
            stages=[
                dict(
                    key=f"stage{i}",
                    title=f"Stage {i}",
                    task_version=f"provided-v{i}",
                    resource_ref=f"provided-resource-{i}",
                    submission_kind="text" if i < 3 else "code",
                    phases=phases,
                )
                for i, phases in enumerate(
                    ([exploration, answering], [review], [coding], [coding]), 1
                )
            ],
        )
    )

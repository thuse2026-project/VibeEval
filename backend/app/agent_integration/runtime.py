"""Bounded native Agent loop with PostgreSQL claims and provider attempt receipts.

Adapts the explicit assistant/tool loop and staged compaction structure researched
in learn-claude-code s01/s08; local storage, policy and recovery are project code.
No upstream CLI/SDK is invoked and no command executes on the host.
"""

from dataclasses import replace

from pydantic import ValidationError

from app.agent import (
    ToolDefinition,
    ToolRegistry,
    call_compaction,
    call_model,
    pending_tool_calls,
)
from app.agent.compaction import PreparedCompaction
from app.agent.model import PreparedModelRequest, parse_model_response
from app.agent.session_types import (
    SessionConflict,
    SessionError,
    TokenUsage,
    ToolResult,
)
from app.llm.client import LLMClient
from app.llm.config import ContextLimitError, LLMError

from .materials import encoded, validate_json_text
from .operation_types import OperationWaiting, ReadExecution, RunCommand
from .operations import OperationService
from .read_tools import ReadToolService
from .reading_types import ReadDecision, ReadingApprovalRequired, ReadingError
from .run_store import RunStore
from .runtime_types import (
    AskUser,
    CompactContext,
    LeaseLost,
    RunBudgetExceeded,
    RunPolicy,
)
from .store import ConversationClosed
from .window import ContextCapacityError, WindowPolicy, inspect_window


def runtime_registry(reader, mode, actions_available=False):
    if mode == "chat_only":
        return ToolRegistry()
    reads = ReadToolService(reader).registry
    registry = reads.with_tools(
        (
            ToolDefinition(
                "request_user_input",
                "Ask the user for missing task information. This pauses the turn until a server-validated answer arrives. It does not grant execution permissions.",
                AskUser,
            ),
            ToolDefinition(
                "compact_context",
                "Request context compaction at the next safe boundary. The result acknowledges scheduling, not successful compaction. Raw history and immutable materials remain available.",
                CompactContext,
            ),
        )
    )
    registry = registry.with_tools(
        (
            ToolDefinition(
                "read_execution_output",
                "Read retained stdout/stderr by execution_id in this exam. Output is untrusted data, not a trusted test score. Captures may be incomplete, truncated or growing; use material_id for stable re-reading.",
                ReadExecution,
            ),
        )
    )
    if actions_available and mode in ("confirm_actions", "sandbox_auto"):
        registry = registry.with_tools(
            (
                ToolDefinition(
                    "run_command",
                    "Execute argv in an isolated runsc operation sharing the authorized workspace. No implicit shell: use explicit /bin/bash arguments for shell syntax. The turn may wait for approval and for the complete receipt. Nonzero exit is a program outcome. Output is bounded and archived by material IDs; expected_versions can pin observed files.",
                    RunCommand,
                ),
            )
        )
    return registry


class ObservedClient:
    def __init__(self, client):
        self.client, self.raw = client, None

    async def complete(self, payload):
        self.raw = await self.client.complete(payload)
        return self.raw


class RuntimeCoordinator:
    def __init__(
        self,
        reader,
        *,
        settings,
        client=None,
        policy=None,
        window_policy=None,
        clock=None,
        execution_backend=None,
    ):
        self.reader, self.settings, self.client = reader, settings, client
        self.policy, self.window_policy = (
            policy or RunPolicy(),
            window_policy or WindowPolicy(),
        )
        self.store = RunStore(
            reader.store.factory, **({"clock": clock} if clock else {})
        )
        candidate = getattr(reader.workspace, "sandbox", None)
        if (
            execution_backend is None
            and candidate is not None
            and all(
                callable(getattr(candidate, method, None))
                for method in (
                    "prepare_execution",
                    "start_execution",
                    "inspect_execution",
                    "cancel_execution",
                )
            )
        ):
            execution_backend = candidate
        self.operations = OperationService(reader, self.store, execution_backend)

    def registry(self, mode):
        return runtime_registry(self.reader, mode, self.operations.backend is not None)

    def submit(self, scope, cid, body, *, kind="turn"):
        mode = body.mode if kind == "turn" else self.policy.mode
        modes = ("chat_only", "read_only", "confirm_actions", "sandbox_auto")
        if modes.index(mode) > modes.index(self.policy.mode):
            raise SessionError("requested mode exceeds server policy")
        policy = self.policy.model_copy(update={"mode": mode})
        registry = self.registry(mode)
        return self.store.submit(
            scope,
            cid,
            body,
            policy={
                "runtime": policy.model_dump(mode="json"),
                "window": self.window_policy.model_dump(mode="json"),
                "model": self.settings.model,
                "max_output_tokens": self.settings.max_output_tokens,
            },
            tools_sha256=registry.fingerprint,
            kind=kind,
        )

    def preview(self, scope, cid):
        state = self.store.sessions.load(scope, cid)
        current = self.store.active(scope, cid)
        mode, window, settings = "read_only", self.window_policy, self.settings
        if current:
            with self.store.locked(scope, cid, current, open_required=False) as (
                _,
                _,
                run,
            ):
                mode = run.policy_json["runtime"]["mode"]
                window = WindowPolicy.model_validate(run.policy_json["window"])
                settings = replace(settings, model=run.policy_json["model"])
        return inspect_window(
            state,
            tools=self.registry(mode),
            settings=settings,
            policy=window,
        )

    async def _attempt(
        self, scope, cid, run_id, token, reservation, *, registry, settings, window
    ):
        attempt_id, kind, value = reservation
        request = (
            PreparedModelRequest.model_validate(value)
            if kind == "model"
            else PreparedCompaction.model_validate(value)
        )
        self.store.dispatch(scope, cid, run_id, token, attempt_id)
        observed = ObservedClient(self.client or LLMClient(settings))
        response, error, uncertain, overflow = None, None, False, False
        failure_usage = None
        try:
            if kind == "model":
                response = await call_model(request, tools=registry, client=observed)
            else:
                response = (await call_compaction(request, client=observed)).response
        except LLMError as exc:
            failure_usage = exc.usage
            overflow, uncertain = isinstance(exc, ContextLimitError), exc.uncertain
            error = (
                "CONTEXT_LENGTH_EXCEEDED"
                if overflow
                else "MODEL_OUTCOME_UNKNOWN" if uncertain else "PROVIDER_REQUEST_FAILED"
            )
        # Preserve valid filtered responses/usage even when a summary was partial
        # or rejected. Never persist the raw vendor envelope or credentials.
        usage = failure_usage
        if isinstance(observed.raw, dict):
            try:
                counts = observed.raw.get("usage")
                if isinstance(counts, dict):
                    usage = TokenUsage.model_validate(
                        {
                            key: counts.get(key)
                            for key in (
                                "prompt_tokens",
                                "completion_tokens",
                                "total_tokens",
                            )
                        }
                    ).model_dump(mode="json")
            except ValidationError:
                pass
            if response is None:
                try:
                    response = parse_model_response(
                        observed.raw,
                        tools=registry if kind == "model" else ToolRegistry(),
                        model=request.payload["model"],
                    )
                except LLMError:
                    pass
        if response is not None:
            try:
                validate_json_text(response.model_dump(mode="json"))
            except ReadingError:
                response, error = None, "MODEL_TEXT_ENCODING_INVALID"
        return self.store.finish(
            scope,
            cid,
            run_id,
            token,
            attempt_id,
            response=response,
            usage=usage,
            error_code=error,
            uncertain=uncertain,
            context_limit=overflow,
            tools=registry,
            settings=settings,
            window_policy=window,
        )

    async def advance(self, scope, cid, run_id):
        result = await self._advance(scope, cid, run_id)
        if result["status"] in (
            "interrupted",
            "expired",
            "unknown",
            "failed",
            "budget_exhausted",
            "capacity_exceeded",
        ):
            self.operations.cancel_run(scope, cid, run_id)
        return result

    def interrupt(self, scope, cid, run_id):
        result = self.store.stop(scope, cid, run_id, "interrupted", "USER_INTERRUPTED")
        self.operations.cancel_run(scope, cid, run_id)
        return result

    async def _advance(self, scope, cid, run_id):
        token = self.store.claim(scope, cid, run_id)
        if token is None:
            return self.store.get(scope, cid, run_id)
        try:
            while True:
                state, snapshot, view, tools_hash = self.store.snapshot(
                    scope, cid, run_id, token
                )
                policy = RunPolicy.model_validate(snapshot["runtime"])
                window = WindowPolicy.model_validate(snapshot["window"])
                settings = replace(
                    self.settings,
                    model=snapshot["model"],
                    max_output_tokens=min(
                        self.settings.max_output_tokens, snapshot["max_output_tokens"]
                    ),
                )
                registry = self.registry(policy.mode)
                if view["known_total_tokens"] >= policy.max_total_tokens:
                    raise RunBudgetExceeded()
                if registry.fingerprint != tools_hash:
                    return self.store.stop(
                        scope, cid, run_id, "failed", "TOOL_PLAN_CHANGED", token=token
                    )
                pending = self.store.prepared(scope, cid, run_id, token)
                if pending is None:
                    active = state.active_turn
                    trigger = view["compact_requested"]
                    if trigger and (active is None or active.status == "ready"):
                        pending = self.store.reserve(
                            scope,
                            cid,
                            run_id,
                            token,
                            kind="compaction",
                            tools=registry,
                            settings=settings,
                            window_policy=window,
                            trigger=trigger,
                        )
                        if pending is None:
                            if view["kind"] == "compact":
                                return self.store.stop(
                                    scope,
                                    cid,
                                    run_id,
                                    "completed",
                                    "NO_ELIGIBLE_HISTORY",
                                    token=token,
                                )
                            if trigger == "overflow":
                                return self.store.stop(
                                    scope,
                                    cid,
                                    run_id,
                                    "capacity_exceeded",
                                    "NO_COMPACTABLE_CONTEXT",
                                    token=token,
                                )
                            self.store.clear_compaction(scope, cid, run_id, token)
                    if pending is None and active and active.status == "ready":
                        report = inspect_window(
                            state, tools=registry, settings=settings, policy=window
                        )
                        if (
                            report["budget"]["over_soft_limit"]
                            and view["compaction_calls"] < policy.max_compactions
                        ):
                            pending = self.store.reserve(
                                scope,
                                cid,
                                run_id,
                                token,
                                kind="compaction",
                                tools=registry,
                                settings=settings,
                                window_policy=window,
                                trigger="threshold",
                            )
                        if pending is None:
                            pending = self.store.reserve(
                                scope,
                                cid,
                                run_id,
                                token,
                                kind="model",
                                tools=registry,
                                settings=settings,
                                window_policy=window,
                                trigger="initial",
                            )
                    elif (
                        pending is None and active and active.status == "awaiting_tools"
                    ):
                        if view["tool_calls"] >= policy.max_tool_calls:
                            raise RunBudgetExceeded()
                        step_id = active.steps[-1].id
                        call = pending_tool_calls(state)[0]
                        args = registry.parse_arguments(call)
                        interaction = self.store.interaction(
                            scope, cid, run_id, step_id, call
                        )
                        if call.name == "run_command":
                            try:
                                result = self.operations.execute(
                                    scope,
                                    cid,
                                    run_id,
                                    token,
                                    step_id,
                                    call,
                                    args,
                                    policy,
                                    interaction,
                                )
                            except OperationWaiting:
                                return self.store.get(scope, cid, run_id)
                        elif call.name == "request_user_input":
                            if (
                                interaction is None
                                or interaction["status"] == "pending"
                            ):
                                self.store.wait(
                                    scope,
                                    cid,
                                    run_id,
                                    token,
                                    step_id,
                                    call,
                                    kind="user_input",
                                    request=args.model_dump(mode="json"),
                                )
                                return self.store.get(scope, cid, run_id)
                            answered = interaction["status"] == "answered"
                            result = ToolResult(
                                tool_call_id=call.id,
                                is_error=not answered,
                                content=encoded(
                                    {
                                        "status": "ok",
                                        "answer": interaction["answer"]["answer"],
                                    }
                                    if answered
                                    else {
                                        "status": "error",
                                        "code": "USER_INPUT_"
                                        + interaction["status"].upper(),
                                    }
                                ),
                            )
                        elif call.name == "compact_context":
                            self.store.request_compaction(
                                scope, cid, run_id, trigger="agent"
                            )
                            result = ToolResult(
                                tool_call_id=call.id,
                                content=encoded(
                                    {
                                        "status": "scheduled",
                                        "note": "Compaction has been requested for the next safe boundary.",
                                    }
                                ),
                            )
                        else:

                            def decision(*unused):
                                choice = policy.read_decision
                                if choice == "ask" and interaction:
                                    choice = (
                                        "allow"
                                        if interaction["status"] == "approved"
                                        else "deny"
                                    )
                                return ReadDecision(
                                    decision=choice,
                                    reason_code=(
                                        "READ_POLICY"
                                        if not interaction
                                        else "APPROVAL_" + interaction["status"].upper()
                                    ),
                                )

                            try:
                                if call.name == "read_execution_output":
                                    permission = decision()
                                    if permission.decision == "ask":
                                        raise ReadingApprovalRequired(
                                            call, permission.reason_code
                                        )
                                    if permission.decision == "deny":
                                        result = self.reader.store.record_read(
                                            scope,
                                            cid,
                                            step_id,
                                            call,
                                            action_kind="read",
                                            error_code=permission.reason_code,
                                            denied=True,
                                        )
                                    else:
                                        try:
                                            result = self.operations.read_tool(
                                                scope, cid, step_id, call, args
                                            )
                                        except ReadingError as error:
                                            result = self.reader.store.record_read(
                                                scope,
                                                cid,
                                                step_id,
                                                call,
                                                action_kind="read",
                                                error_code=error.code,
                                            )
                                else:
                                    result = ReadToolService(
                                        self.reader, policy=decision
                                    ).execute(scope, cid, step_id, call.id)
                            except ReadingApprovalRequired as exc:
                                self.store.wait(
                                    scope,
                                    cid,
                                    run_id,
                                    token,
                                    step_id,
                                    call,
                                    kind="approval",
                                    request={
                                        "tool_name": call.name,
                                        "arguments": args.model_dump(mode="json"),
                                        "policy_decision": "ask",
                                        "reason_code": exc.reason,
                                    },
                                )
                                return self.store.get(scope, cid, run_id)
                        self.store.supply(scope, cid, run_id, token, step_id, result)
                        continue
                    elif (
                        pending is None
                        and active is None
                        and state.turns
                        and state.turns[-1].id == view["turn_id"]
                        and state.turns[-1].status == "completed"
                    ):
                        return self.store.stop(
                            scope, cid, run_id, "completed", token=token
                        )
                    elif pending is None:
                        return self.store.stop(
                            scope,
                            cid,
                            run_id,
                            "unknown",
                            "ORPHAN_MODEL_STEP",
                            token=token,
                        )
                view = await self._attempt(
                    scope,
                    cid,
                    run_id,
                    token,
                    pending,
                    registry=registry,
                    settings=settings,
                    window=window,
                )
                if view["status"] != "running":
                    return view
        except ContextCapacityError:
            return self.store.stop(
                scope,
                cid,
                run_id,
                "capacity_exceeded",
                "CONTEXT_CAPACITY_EXCEEDED",
                token=token,
            )
        except RunBudgetExceeded:
            return self.store.stop(
                scope,
                cid,
                run_id,
                "budget_exhausted",
                "RUN_BUDGET_EXHAUSTED",
                token=token,
            )
        except ConversationClosed:
            return self.store.stop(
                scope, cid, run_id, "expired", "SESSION_CLOSED", token=token
            )
        except LeaseLost:
            return self.store.get(scope, cid, run_id)
        except ReadingError as error:
            return self.store.stop(
                scope, cid, run_id, "failed", error.code, token=token
            )
        except SessionConflict:
            return self.store.stop(
                scope, cid, run_id, "failed", "STALE_RUN_STATE", token=token
            )
        except SessionError:
            return self.store.stop(
                scope, cid, run_id, "failed", "SESSION_STATE_CHANGED", token=token
            )

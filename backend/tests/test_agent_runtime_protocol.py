"""Pure contracts for attachment history and explicit provider rejection signals."""

import hashlib
import json
import unittest

import httpx
from pydantic import ValidationError

from app.agent import begin_turn, create_conversation, render_history, start_step
from app.agent.session import release_context_rejected_step
from app.agent.session_types import turns_digest
from app.agent_integration.operation_types import RunCommand
from app.agent_integration.runtime_types import AskUser, RunInput
from app.llm.client import LLMClient
from app.llm.config import ContextLimitError, LLMError, LLMSettings


class RuntimeProtocolTests(unittest.TestCase):
    def test_command_arguments_are_explicit_bounded_and_cannot_choose_sandbox(self):
        self.assertEqual(RunCommand(argv=("python", "-c", "")).argv[-1], "")
        for body in (
            {"argv": []},
            {"argv": [""]},
            {"argv": ["bad\x00exe"]},
            {"argv": ["python"], "cwd": "../"},
            {"argv": ["python"], "sandbox_id": "other"},
            {"argv": ["python"], "timeout_seconds": 121},
            {"argv": ["python"], "expected_versions": {"/etc/passwd": 1}},
        ):
            with self.subTest(body=body), self.assertRaises(ValidationError):
                RunCommand.model_validate(body)

    def test_old_turn_digest_is_unchanged_without_attachment_context(self):
        state = begin_turn(create_conversation("scope"), "question", request_key="old")
        records = [
            turn.model_dump(mode="json", exclude={"input_context"})
            for turn in state.turns
        ]
        legacy = json.dumps(
            records,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        self.assertEqual(
            turns_digest(state.turns),
            hashlib.sha256(legacy.encode("utf-8")).hexdigest(),
        )

    def test_attachment_context_is_separate_and_request_key_binds_it(self):
        state = begin_turn(
            create_conversation("scope"),
            "原始用户请求",
            request_key="same",
            input_context='{"references":[]}',
        )
        self.assertEqual(state.turns[0].user_message, "原始用户请求")
        self.assertIn("Attached reference data", render_history(state)[0]["content"])
        with self.assertRaises(ValueError):
            begin_turn(
                state, "原始用户请求", request_key="same", input_context="different"
            )

    def test_released_rejected_placeholder_has_no_accepted_message_to_erase(self):
        ready = begin_turn(
            create_conversation("scope"), "current task", request_key="new"
        )
        pending = start_step(ready, step_id="rejected-request")
        released = release_context_rejected_step(pending, "rejected-request")
        self.assertEqual(released.active_turn.status, "ready")
        self.assertEqual(released.active_turn.steps, ())
        self.assertEqual(render_history(released), render_history(pending))
        self.assertEqual(released.revision, pending.revision + 1)

    def test_browser_and_question_schemas_reject_permission_overrides_or_unstorable_prompt(
        self,
    ):
        for body in (
            {"request_key": "k", "text": " "},
            {"request_key": "k", "text": "x", "approved": True},
            {"request_key": "k", "text": "x", "mode": "host_auto"},
        ):
            with self.assertRaises(ValidationError):
                RunInput.model_validate(body)
        with self.assertRaises(ValidationError):
            AskUser(question="bad\x00question")


class RuntimeProviderErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_error_usage_receipt_is_filtered_without_exposing_vendor_body(self):
        settings = LLMSettings(
            "https://fixture.test/v1", "fixture-key", "fixture/model"
        )

        def transport(request):
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "context_length_exceeded",
                        "message": "private vendor details",
                    },
                    "usage": {"total_tokens": 7, "credential": "do-not-retain"},
                },
            )

        with self.assertRaises(ContextLimitError) as raised:
            await LLMClient(
                settings, transport=httpx.MockTransport(transport)
            ).complete({"messages": [], "max_tokens": 10})
        self.assertEqual(
            raised.exception.usage,
            {"prompt_tokens": None, "completion_tokens": None, "total_tokens": 7},
        )
        self.assertNotIn("private", str(raised.exception))

    async def test_only_structured_context_errors_signal_compaction_retry(self):
        settings = LLMSettings(
            "https://fixture.test/v1", "fixture-key", "fixture/model"
        )
        errors = (
            ({"code": "context_length_exceeded"}, True),
            (
                {"code": 400, "metadata": {"error_type": "context_length_exceeded"}},
                True,
            ),
            (
                {
                    "code": 400,
                    "message": "context_length_exceeded maximum context length",
                },
                False,
            ),
            ({"code": 400, "metadata": {"error_type": "token_limit_exceeded"}}, False),
        )
        for payload, expected in errors:
            calls = []

            def transport(request):
                calls.append(request)
                return httpx.Response(400, json={"error": payload})

            with self.subTest(payload=payload), self.assertRaises(LLMError) as raised:
                await LLMClient(
                    settings, transport=httpx.MockTransport(transport)
                ).complete({"messages": [], "max_tokens": 10})
            self.assertEqual(isinstance(raised.exception, ContextLimitError), expected)
            self.assertEqual(len(calls), 1)
            self.assertNotIn("fixture-key", str(raised.exception))

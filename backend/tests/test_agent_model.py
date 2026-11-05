import json
import unittest
from unittest.mock import patch

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent import (
    ToolDefinition,
    ToolRegistry,
    accept_response,
    begin_turn,
    call_model,
    create_conversation,
    interrupt_turn,
    pending_tool_calls,
    prepare_model_request,
    start_step,
    supply_tool_results,
)
from app.agent.model import NativeRequest, PreparedModelRequest, parse_model_response
from app.agent.prompts import GLOBAL_AGENT_PROMPT
from app.agent.session_types import (
    SessionBusy,
    SessionConfig,
    SessionError,
    ToolCall,
    ToolResult,
)
from app.llm.client import LLMClient
from app.llm.config import LLMError, LLMSettings


class ReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1)
    limit: int = Field(default=100, ge=1, le=1000)


def tool_registry(
    description="Read the requested file; return text from an external reader.",
):
    return ToolRegistry([ToolDefinition("read_file", description, ReadArguments)])


def completion(*, text="回答", calls=None, reason=None, details=None):
    message = {"role": "assistant", "content": text, "provider_debug": "never expose"}
    if calls is not None:
        message["tool_calls"] = calls
    if details is not None:
        message["reasoning_details"] = details
    return {
        "id": "completion-1",
        "model": "actual/model",
        "choices": [
            {
                "message": message,
                "finish_reason": reason or ("tool_calls" if calls else "stop"),
            }
        ],
        "usage": {
            "prompt_tokens": 6,
            "completion_tokens": 4,
            "total_tokens": 10,
            "provider_debug": "private",
        },
    }


NATIVE_CALL = {
    "id": "call-1",
    "type": "function",
    "function": {
        "name": "read_file",
        "arguments": '{"path":"a.py"}',
    },
}


class AgentModelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = LLMSettings(
            base_url="https://provider.test/api/v1",
            api_key="local-test-secret",
            model="configured/model",
            max_output_tokens=512,
        )
        self.registry = tool_registry()
        self.state = start_step(
            begin_turn(
                create_conversation("trusted-scope"), "解释 a.py", request_key="m1"
            ),
            step_id="s1",
        )

    def prepare(self, state=None, **kwargs):
        return prepare_model_request(
            state or self.state, tools=self.registry, settings=self.settings, **kwargs
        )

    def test_prepare_exposes_actual_messages_tools_parameters_and_versions(self):
        with patch("app.agent.model.LLMClient", side_effect=AssertionError):
            request = self.prepare(max_tokens=100, temperature=0)
        payload = request.payload
        self.assertEqual(
            payload["messages"],
            [
                {"role": "system", "content": GLOBAL_AGENT_PROMPT},
                {"role": "user", "content": "解释 a.py"},
            ],
        )
        self.assertEqual(payload["tools"], self.registry.specifications())
        self.assertEqual(payload["model"], "configured/model")
        self.assertEqual(payload["max_tokens"], 100)
        self.assertFalse(payload["stream"])
        self.assertEqual(request.step_id, "s1")
        self.assertEqual(request.revision, self.state.revision)
        self.assertNotIn("local-test-secret", request.model_dump_json())
        self.assertEqual(
            PreparedModelRequest.model_validate_json(request.model_dump_json()), request
        )
        payload["messages"][0]["content"] = "modified preview copy"
        self.assertEqual(request.payload["messages"][0]["content"], GLOBAL_AGENT_PROMPT)

    def test_plain_mode_omits_tools_and_task_prompt_is_kept_separate(self):
        state = start_step(
            begin_turn(
                create_conversation(
                    "scope",
                    config=SessionConfig(
                        model="session/model", system_prompt="只解释代码"
                    ),
                ),
                "hello",
                request_key="one",
            )
        )
        prepared = prepare_model_request(state, settings=self.settings)
        self.assertNotIn("tools", prepared.payload)
        self.assertEqual(prepared.payload["model"], "session/model")
        self.assertEqual(
            prepared.payload["messages"][1], {"role": "system", "content": "只解释代码"}
        )

    def test_prepare_rejects_wrong_versions_unstarted_or_waiting_tools(self):
        fresh = create_conversation("scope")
        with self.assertRaises(SessionBusy):
            self.prepare(fresh)
        waiting = accept_response(
            self.state,
            "s1",
            parse_model_response(
                completion(calls=[NATIVE_CALL]), tools=self.registry, model="m"
            ),
        )
        with self.assertRaises(SessionBusy):
            self.prepare(waiting)
        for config in (
            SessionConfig(prompt_version="other"),
            SessionConfig(tools_version="other"),
        ):
            state = start_step(
                begin_turn(
                    create_conversation("scope", config=config),
                    "hello",
                    request_key="m",
                )
            )
            with self.assertRaises(SessionError):
                self.prepare(state)

    def test_prepare_bounds_budget_and_never_silently_discards_history(self):
        for limit in (0, 513, True):
            with self.subTest(limit=limit), self.assertRaises(LLMError):
                self.prepare(max_tokens=limit)
        with self.assertRaises(ValidationError):
            self.prepare(temperature=float("nan"))
        settings = LLMSettings(
            base_url="https://provider.test/v1", api_key="", model=""
        )
        with self.assertRaises(LLMError):
            prepare_model_request(self.state, settings=settings)

    def test_request_validation_rejects_orphan_or_unresolved_tools(self):
        base = self.prepare().payload
        for messages in (
            [{"role": "tool", "tool_call_id": "unknown", "content": "x"}],
            [
                {"role": "assistant", "tool_calls": [NATIVE_CALL]},
                {"role": "user", "content": "next"},
            ],
            [{"role": "assistant", "tool_calls": [NATIVE_CALL]}],
        ):
            with self.subTest(messages=messages), self.assertRaises(ValidationError):
                NativeRequest.model_validate({**base, "messages": messages})

    async def test_native_two_call_roundtrip_preserves_tools_and_continuation(self):
        seen = []
        details = [
            {
                "type": "reasoning.encrypted",
                "data": "opaque-ciphertext",
                "signature": "opaque-signature",
            }
        ]

        def transport(request):
            self.assertEqual(
                str(request.url), "https://provider.test/api/v1/chat/completions"
            )
            self.assertEqual(
                request.headers["authorization"], "Bearer local-test-secret"
            )
            seen.append(json.loads(request.content))
            return httpx.Response(
                200,
                json=(
                    completion(text=None, calls=[NATIVE_CALL], details=details)
                    if len(seen) == 1
                    else completion(text="返回参数平方")
                ),
            )

        client = LLMClient(self.settings, transport=httpx.MockTransport(transport))
        first = await call_model(self.prepare(), tools=self.registry, client=client)
        state = accept_response(self.state, "s1", first)
        self.assertEqual(pending_tool_calls(state)[0].name, "read_file")
        self.assertEqual(
            self.registry.parse_arguments(pending_tool_calls(state)[0]).path, "a.py"
        )
        state = supply_tool_results(
            state,
            "s1",
            [
                ToolResult(
                    tool_call_id="call-1", content="def square(x):\n    return x * x\n"
                )
            ],
        )
        state = start_step(state, step_id="s2")
        second = await call_model(
            self.prepare(state), tools=self.registry, client=client
        )
        state = accept_response(state, "s2", second)
        self.assertEqual(state.turns[-1].status, "completed")
        self.assertEqual(state.usage.total_tokens, 20)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[1]["tools"], seen[0]["tools"])
        assistant = seen[1]["messages"][2]
        self.assertEqual(assistant["tool_calls"], [NATIVE_CALL])
        self.assertEqual(assistant["reasoning_details"], details)
        self.assertEqual(
            seen[1]["messages"][3],
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": "def square(x):\n    return x * x\n",
            },
        )
        self.assertNotIn("provider_debug", state.model_dump_json())

    async def test_plain_text_command_is_not_a_tool_call(self):
        def transport(request):
            return httpx.Response(
                200, json=completion(text="可以运行 python -m unittest")
            )

        result = await call_model(
            self.prepare(),
            tools=self.registry,
            client=LLMClient(self.settings, transport=httpx.MockTransport(transport)),
        )
        self.assertEqual(result.message.tool_calls, ())
        self.assertIn("python -m unittest", result.message.content)

    def test_unknown_tool_and_invalid_arguments_never_escape_as_valid_requests(self):
        for function in (
            {"name": "undeclared", "arguments": "{}"},
            {"name": "read_file", "arguments": '{"path":2}'},
            {"name": "read_file", "arguments": '{"path":"a.py","limit":"5"}'},
            {
                "name": "read_file",
                "arguments": '{"path":"a.py","extra":"private-secret"}',
            },
            {"name": "read_file", "arguments": '{"path":"a.py","path":"b.py"}'},
        ):
            with self.subTest(function=function), self.assertRaises(LLMError) as caught:
                parse_model_response(
                    completion(calls=[{**NATIVE_CALL, "function": function}]),
                    tools=self.registry,
                    model="m",
                )
            self.assertNotIn("private-secret", str(caught.exception))

    def test_malformed_provider_data_is_sanitized(self):
        for payload in (
            {},
            {"choices": []},
            {"choices": [{"message": None}]},
            completion(text=None),
            completion(calls={}),
            completion(calls=[NATIVE_CALL, NATIVE_CALL]),
            completion(reason="unknown"),
        ):
            with self.subTest(payload=payload), self.assertRaises(LLMError):
                parse_model_response(payload, tools=self.registry, model="m")
        partial = parse_model_response(
            completion(text=None, reason="length"), tools=self.registry, model="m"
        )
        self.assertEqual(
            accept_response(self.state, "s1", partial).turns[0].status, "failed"
        )

    def test_provider_message_cannot_claim_a_different_role(self):
        for role in ("user", "system", "tool", None):
            payload = completion()
            payload["choices"][0]["message"]["role"] = role
            with self.subTest(role=role), self.assertRaises(LLMError):
                parse_model_response(payload, tools=self.registry, model="m")

    async def test_incomplete_tool_output_preserves_reason_usage_and_safe_continuation(
        self,
    ):
        for reason, code in (
            ("length", "OUTPUT_TRUNCATED"),
            ("content_filter", "CONTENT_FILTERED"),
        ):
            for raw_calls in (
                [NATIVE_CALL],
                [
                    {
                        **NATIVE_CALL,
                        "function": {"name": "read_file", "arguments": '{"path":'},
                    }
                ],
                [{"function": {"arguments": '{"path":'}}],
            ):
                with self.subTest(reason=reason, raw_calls=raw_calls):
                    seen = []

                    def transport(request):
                        seen.append(request)
                        return httpx.Response(
                            200,
                            json=completion(
                                text="输出未完成", calls=raw_calls, reason=reason
                            ),
                        )

                    response = await call_model(
                        self.prepare(),
                        tools=self.registry,
                        client=LLMClient(
                            self.settings, transport=httpx.MockTransport(transport)
                        ),
                    )
                    self.assertEqual(len(seen), 1)
                    self.assertEqual(response.finish_reason, reason)
                    self.assertEqual(response.message.tool_calls, ())
                    failed = accept_response(self.state, "s1", response)
                    self.assertEqual(failed.turns[0].status, "failed")
                    self.assertEqual(failed.turns[0].steps[0].error_code, code)
                    self.assertEqual(failed.usage.total_tokens, 10)
                    self.assertEqual(pending_tool_calls(failed), ())
                    continued = start_step(
                        begin_turn(failed, "重新提问", request_key="m2"), step_id="s2"
                    )
                    payload = self.prepare(continued).payload
                    NativeRequest.model_validate(payload)
                    self.assertFalse(
                        any(
                            message.get("tool_calls") for message in payload["messages"]
                        )
                    )

    async def test_changed_tools_or_tampered_request_fail_before_any_api_call(self):
        class NeverCall:
            async def complete(self, payload):
                raise AssertionError("should not call provider")

        prepared = self.prepare()
        with self.assertRaises(SessionError):
            await call_model(
                prepared,
                tools=tool_registry("different schema description"),
                client=NeverCall(),
            )
        corrupted = prepared.model_copy(update={"payload_json": "{}"})
        with self.assertRaises(ValidationError):
            await call_model(corrupted, tools=self.registry, client=NeverCall())

    async def test_upstream_failure_is_single_call_and_keeps_step_external(self):
        calls = []

        def transport(request):
            calls.append(request)
            return httpx.Response(429, json={"error": {"message": "private-key"}})

        with self.assertRaises(LLMError) as caught:
            await call_model(
                self.prepare(),
                tools=self.registry,
                client=LLMClient(
                    self.settings, transport=httpx.MockTransport(transport)
                ),
            )
        self.assertEqual(caught.exception.status, 429)
        self.assertNotIn("private-key", str(caught.exception))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.state.active_turn.status, "awaiting_model")

    def test_interruption_closes_old_tool_pairs_before_the_next_user_request(self):
        state = accept_response(
            self.state,
            "s1",
            parse_model_response(
                completion(calls=[NATIVE_CALL]), tools=self.registry, model="m"
            ),
        )
        state = interrupt_turn(state, state.active_turn.id)
        state = start_step(
            begin_turn(state, "换个问题", request_key="m2"), step_id="s2"
        )
        payload = self.prepare(state).payload
        result = next(m for m in payload["messages"] if m["role"] == "tool")
        self.assertEqual(json.loads(result["content"])["status"], "aborted")

    async def test_complete_transport_rechecks_output_cap_and_rejects_streaming(self):
        client = LLMClient(
            self.settings,
            transport=httpx.MockTransport(lambda request: self.fail("must not send")),
        )
        for changes in (
            {"max_tokens": 513},
            {"stream": True},
            {"headers": {"Authorization": "bad"}},
        ):
            with self.subTest(changes=changes), self.assertRaises(LLMError):
                await client.complete({**self.prepare().payload, **changes})


class ToolDefinitionTests(unittest.TestCase):
    def test_schema_comes_from_the_same_model_that_validates_arguments(self):
        registry = tool_registry()
        schema = registry.specifications()[0]["function"]["parameters"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["required"], ["path"])
        call = ToolCall(id="one", name="read_file", arguments='{"path":"a.py"}')
        self.assertEqual(registry.parse_arguments(call).limit, 100)
        spec = registry.specifications()
        spec[0]["function"]["name"] = "mutated"
        self.assertEqual(registry.specifications()[0]["function"]["name"], "read_file")

    def test_invalid_or_duplicate_declarations_are_rejected(self):
        tool = ToolDefinition("read_file", "Read", ReadArguments)
        with self.assertRaises(ValueError):
            ToolRegistry([tool, tool])
        with self.assertRaises(ValueError):
            ToolDefinition("bad name", "Read", ReadArguments)
        with self.assertRaises(ValueError):
            ToolDefinition("read_file", " ", ReadArguments)

        class Permissive(BaseModel):
            path: str

        with self.assertRaises(ValueError):
            ToolDefinition("read_file", "Read", Permissive)

import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app.agent import chat_only, prepare_chat
from app.llm.client import ChatInput, ChatResult, Message
from app.llm.config import LLMError


class RecordingModel:
    def __init__(self, reply="文本回复"):
        self.calls = []
        self.reply = reply

    async def chat(self, body):
        self.calls.append(body)
        return ChatResult(model="test/model", content=self.reply)


class AgentDialogueTests(unittest.IsolatedAsyncioTestCase):
    def test_prepared_input_is_inspectable_without_configuration_or_network(self):
        with (
            patch("app.agent.dialogue.load_settings", side_effect=AssertionError),
            patch("app.agent.dialogue.LLMClient", side_effect=AssertionError),
        ):
            prepared = prepare_chat(
                "解释这段代码",
                history=[Message(role="user", content="    return x\n")],
            )
        self.assertIsNone(prepared.model)
        self.assertEqual(prepared.messages[0].content, "    return x\n")
        self.assertEqual(prepared.messages[-1].content, "解释这段代码")
        restored = ChatInput.model_validate_json(prepared.model_dump_json())
        self.assertEqual(restored, prepared)

    async def test_single_turn_builds_explicit_context_in_order(self):
        model = RecordingModel()
        history = [
            Message(role="user", content="上一轮问题"),
            Message(role="assistant", content="上一轮回答"),
        ]
        result = await chat_only(
            "继续分析",
            history=history,
            system_prompt="帮助阅读代码",
            model="chosen/model",
            max_tokens=100,
            temperature=0,
            client=model,
        )
        self.assertEqual(result.content, "文本回复")
        self.assertEqual(len(model.calls), 1)
        body = model.calls[0]
        self.assertEqual(
            [message.role for message in body.messages],
            ["system", "user", "assistant", "user"],
        )
        self.assertEqual(body.messages[-1].content, "继续分析")
        self.assertEqual(body.model, "chosen/model")
        self.assertEqual(body.max_tokens, 100)
        self.assertEqual(body.temperature, 0)
        self.assertEqual(len(history), 2)
        body.messages[1].content = "provider modified its copy"
        self.assertEqual(history[0].content, "上一轮问题")

    async def test_code_indentation_is_preserved_in_prompt_and_history(self):
        model = RecordingModel()
        code = "    return value\n\n"
        await chat_only(
            code, history=[Message(role="user", content=code)], client=model
        )
        self.assertEqual(model.calls[0].messages[0].content, code)
        self.assertEqual(model.calls[0].messages[1].content, code)

    async def test_command_like_output_is_returned_as_text_after_one_call(self):
        command = '{"tool": "shell", "command": "echo hello"}'
        model = RecordingModel(reply=command)
        result = await chat_only("建议一个命令", client=model)
        self.assertEqual(result.content, command)
        self.assertEqual(len(model.calls), 1)
        self.assertNotIn("tools", model.calls[0].model_dump())

    async def test_invalid_prompt_does_not_call_model(self):
        model = RecordingModel()
        for prompt in (" ", "a" * 16001):
            with self.assertRaises(ValidationError):
                await chat_only(prompt, client=model)
        self.assertEqual(model.calls, [])

    async def test_provider_failure_propagates_without_followup_or_retry(self):
        class FailingModel:
            calls = 0

            async def chat(self, body):
                self.calls += 1
                raise LLMError(429, "模型服务限流")

        model = FailingModel()
        with self.assertRaises(LLMError):
            await chat_only("你好", client=model)
        self.assertEqual(model.calls, 1)

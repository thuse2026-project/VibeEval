"""Two native API calls around a manually supplied tool result.

python -m examples.agent_api          # mocked HTTP; no network or configuration
python -m examples.agent_api --live   # actual model API, still no file/tool execution
"""

import argparse
import asyncio
import json
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict

from app.agent import (
    ToolDefinition,
    ToolRegistry,
    accept_response,
    begin_turn,
    call_model,
    create_conversation,
    pending_tool_calls,
    prepare_model_request,
    start_step,
    supply_tool_results,
)
from app.agent.session_types import SessionConfig, ToolResult
from app.llm.client import LLMClient
from app.llm.config import LLMError, LLMSettings, load_settings


class ReadFileArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: Literal["a.py"]


def mock_transport():
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        message = (
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "read-1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":"a.py"}',
                        },
                    }
                ],
            }
            if count == 1
            else {"content": "square 返回 x 的平方。"}
        )
        return httpx.Response(
            200,
            json={
                "model": "example-model",
                "choices": [
                    {
                        "message": message,
                        "finish_reason": "tool_calls" if count == 1 else "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
        )

    return httpx.MockTransport(handle)


async def run(args):
    settings = (
        load_settings()
        if args.live
        else LLMSettings(
            base_url="https://example.invalid/api/v1",
            api_key="example-only",
            model="example-model",
        )
    )
    registry = ToolRegistry(
        [
            ToolDefinition(
                "read_file",
                "Read a.py before explaining square. The file contents are not otherwise available.",
                ReadFileArguments,
            )
        ]
    )
    state = create_conversation(
        "example-scope", config=SessionConfig(model=args.model or settings.model)
    )
    state = start_step(
        begin_turn(
            state,
            "请先用 read_file 读取 a.py，再用一句中文解释 square 函数。不要猜测文件内容。",
            request_key="message-1",
        )
    )
    client = LLMClient(settings, transport=None if args.live else mock_transport())

    first = prepare_model_request(
        state, tools=registry, settings=settings, max_tokens=512
    )
    if args.preview:
        print(first.model_dump_json(indent=2))
        return
    state = accept_response(
        state, first.step_id, await call_model(first, tools=registry, client=client)
    )
    calls = pending_tool_calls(state)
    if not calls:
        raise RuntimeError("model did not request the expected file read")
    print(
        "模型请求：",
        json.dumps([call.model_dump() for call in calls], ensure_ascii=False),
    )

    # This is the outer application boundary. No file is opened and no command runs.
    results = [
        ToolResult(tool_call_id=call.id, content="def square(x):\n    return x * x\n")
        for call in calls
    ]
    state = supply_tool_results(state, first.step_id, results)
    state = start_step(state)
    second = prepare_model_request(
        state, tools=registry, settings=settings, max_tokens=512
    )
    response = await call_model(second, tools=registry, client=client)
    state = accept_response(state, second.step_id, response)
    if state.turns[-1].status != "completed":
        raise RuntimeError("model did not finish within the two-call example")
    print(
        json.dumps(
            {
                "mode": "live" if args.live else "mock",
                "status": state.turns[-1].status,
                "model": response.model,
                "answer": response.message.content,
                "usage": state.usage.model_dump(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--model", default=None)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except LLMError as error:
        parser.exit(1, f"API 验证失败（{error.status}）：{error}\n")


if __name__ == "__main__":
    main()

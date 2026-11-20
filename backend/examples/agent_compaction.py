"""Explicit compaction, snapshot restore, then one follow-up API call.

python -m examples.agent_compaction             # mocked HTTP, no network
python -m examples.agent_compaction --preview   # inspect without sending
python -m examples.agent_compaction --live --model <model-id>
"""

import argparse
import asyncio
import json

import httpx

from app.agent import (
    accept_response,
    apply_compaction,
    begin_turn,
    call_compaction,
    call_model,
    context_info,
    create_conversation,
    prepare_compaction,
    prepare_model_request,
    render_context,
    render_history,
    restore_snapshot,
    start_step,
)
from app.agent.session_types import AssistantOutput, ModelResponse, SessionConfig
from app.llm.client import LLMClient
from app.llm.config import LLMError, LLMSettings, load_settings


def sample_history(model):
    state = create_conversation("example-scope", config=SessionConfig(model=model))
    messages = (
        "项目语言 Python，入口文件 src/calc.py。"
        + "重复背景：这是一个练习项目。" * 150,
        "square(x) 返回 x 的平方，目前没有未解决的问题。"
        + "重复背景：这是一个练习项目。" * 150,
        "最近新增的规则：使用四空格缩进。",
    )
    for index, text in enumerate(messages):
        state = start_step(begin_turn(state, text, request_key=f"history-{index}"))
        # Imported sample dialogue, not extra model API calls or real operations.
        state = accept_response(
            state,
            state.active_turn.steps[-1].id,
            ModelResponse(
                model="imported-example",
                finish_reason="stop",
                message=AssistantOutput(content="已记录这些项目事实。"),
            ),
        )
    return state


def mock_transport():
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        text = (
            "项目使用 Python，入口 src/calc.py；square(x) 返回 x 的平方；没有未解决的问题。"
            if count == 1
            else "语言 Python；入口 src/calc.py；square 返回平方；使用四空格缩进。"
        )
        return httpx.Response(
            200,
            json={
                "model": "example-model",
                "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 30,
                    "total_tokens": 130,
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
    state = sample_history(args.model or settings.model)
    prepared = prepare_compaction(
        state, through_turn=2, settings=settings, max_tokens=512
    )
    if args.preview:
        print(prepared.model_dump_json(indent=2))
        return
    client = LLMClient(settings, transport=None if args.live else mock_transport())
    result = await call_compaction(prepared, client=client)
    print("待采用摘要：", result.response.message.content)
    compacted = apply_compaction(state, result)

    # The outer application can persist this JSON through its own storage adapter.
    restored = restore_snapshot(compacted.model_dump_json(), scope_id="example-scope")
    if render_history(restored) != render_history(state) or render_context(
        restored
    ) != render_context(compacted):
        raise RuntimeError("snapshot did not retain history and the adopted context")
    print("恢复后的上下文：", context_info(restored).model_dump_json())

    restored = start_step(
        begin_turn(
            restored,
            "仅根据已有事实，用一句中文列出语言、入口文件、square 的作用和缩进规则。",
            request_key="follow-up",
        )
    )
    following = prepare_model_request(restored, settings=settings, max_tokens=512)
    response = await call_model(following, client=client)
    restored = accept_response(restored, following.step_id, response)
    if restored.turns[-1].status != "completed":
        raise RuntimeError("follow-up did not finish in one API call")
    print(
        json.dumps(
            {
                "mode": "live" if args.live else "mock",
                "status": restored.turns[-1].status,
                "model": response.model,
                "answer": response.message.content,
                "compaction_usage": (
                    result.response.usage.model_dump()
                    if result.response.usage
                    else None
                ),
                "follow_up_usage": (
                    response.usage.model_dump() if response.usage else None
                ),
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

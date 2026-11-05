"""Manual API interaction, with no network, SDK process or tool execution.

From backend/: python -m examples.agent_session
"""

import json

from app.agent import (
    accept_response,
    begin_turn,
    create_conversation,
    pending_tool_calls,
    render_history,
    start_step,
    supply_tool_results,
)
from app.agent.session_types import AssistantOutput, ModelResponse, ToolCall, ToolResult


def main():
    state = create_conversation("trusted-candidate-scope")
    state = begin_turn(state, "解释 a.py 中的函数。", request_key="user-message-1")
    state = start_step(state, step_id="api-step-1")

    # A future API adapter will supply this response. This example supplies it manually.
    state = accept_response(
        state,
        "api-step-1",
        ModelResponse(
            model="manual-model",
            finish_reason="tool_calls",
            message=AssistantOutput(
                tool_calls=(
                    ToolCall(
                        id="call-read-1", name="read_file", arguments='{"path":"a.py"}'
                    ),
                )
            ),
        ),
    )
    print("外层收到的工具请求：", pending_tool_calls(state)[0].model_dump_json())

    # The external reader returns text. The session code does not read a.py itself.
    state = supply_tool_results(
        state,
        "api-step-1",
        [
            ToolResult(
                tool_call_id="call-read-1", content="def square(x):\n    return x * x\n"
            ),
        ],
    )
    state = start_step(state, step_id="api-step-2")
    state = accept_response(
        state,
        "api-step-2",
        ModelResponse(
            model="manual-model",
            finish_reason="stop",
            message=AssistantOutput(content="square 返回参数 x 的平方。"),
        ),
    )
    print("轮次状态：", state.turns[-1].status)
    print(json.dumps(render_history(state), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

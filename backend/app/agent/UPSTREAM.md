# 上游来源与改写边界

本目录的会话部分在 VibeEval 内用 Python 实现，不调用 Codex/Claude SDK 或 CLI。
本批按下列官方公开源码的结构与处理方式进行移植、精简和适配，保留来源版本与许可证。
Claude Code 官方仓库本身不作为本批完整运行时的代码来源；Claude 部分来自公开的 Agent SDK。

| 本地模块 | 参考/移植内容 | 上游固定版本 |
| --- | --- | --- |
| `session_types.py` | 会话级状态与单轮状态分开，单轮持有待处理调用、取消和用量；Python 类型及可序列化结构为本地适配 | [Codex SessionState](https://github.com/openai/codex/blob/695961ee88a2c84c0919814a4345b94a2d10f3ca/codex-rs/core/src/state/session.rs)、[TurnState](https://github.com/openai/codex/blob/695961ee88a2c84c0919814a4345b94a2d10f3ca/codex-rs/core/src/state/turn.rs) |
| `session_types.py` | 工具调用 ID、工具结果关联 ID、错误结果和助手输出等消息结构；原始工具参数保留为兼容 API 的 JSON 字符串 | [Claude Agent SDK types.py](https://github.com/anthropics/claude-agent-sdk-python/blob/3be907edb9c86269d30d1427a77937dd788d6419/src/claude_agent_sdk/types.py) |
| `session.py` 的 `render_history` | 中断后在模型历史投影中补齐工具调用的明确中断结果，不修改已保存的原始结果 | [Codex normalize.rs](https://github.com/openai/codex/blob/695961ee88a2c84c0919814a4345b94a2d10f3ca/codex-rs/core/src/context_manager/normalize.rs) |

这里没有整文件复制上游实现。状态转换、乐观版本存储、输入校验与测试是围绕本项目 API 边界的 Python 改写。
不声称与上游类型或全部行为兼容；SDK 网络传输、执行器、审批、调度、多 Agent 等不在本批移植范围。
后续移植需继续按具体模块记录来源，不将第三方镜像中的未核实代码作为官方开源实现。

上游声明随附于 `upstream/`：

- `LICENSE-Codex.txt`、`NOTICE-Codex.txt`：Codex，Apache-2.0。
- `LICENSE-Claude-Agent-SDK.txt`：Claude Agent SDK Python，MIT。

本文件记录技术来源及附带声明，不改变仓库其他部分的许可范围。

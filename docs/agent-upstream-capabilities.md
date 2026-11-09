# Agent 后端能力对照与实现边界

2026-11-06 核对公开源码和官方文档。本文是规划与验收矩阵，不表示下表能力已经实现。
目标为两个分支组合后的编程 Agent 后端；不实现前端，不提供联网搜索。
核心调用与状态放在 app.agent，真实来源、执行、策略、记录和协议放在集成层。
通过本项目代码移植/改写，不调用 Codex/Claude CLI 或 SDK 运行时替代实现。

## 来源与可核实范围

- Codex 本次固定版本：[e3fe1ade2ca4cfcea0c192317beefa38c0ff1b5a](https://github.com/openai/codex/tree/e3fe1ade2ca4cfcea0c192317beefa38c0ff1b5a)。既有核心的来源版本继续保留，未自动更新其实现。
- Claude Agent SDK Python 固定版本：[3be907edb9c86269d30d1427a77937dd788d6419](https://github.com/anthropics/claude-agent-sdk-python/tree/3be907edb9c86269d30d1427a77937dd788d6419)。官方 Claude Code 仓库不作为完整 CLI 内部源码的来源。
- Claude Code 复现项目：[shareAI-lab/learn-claude-code@7226f55](https://github.com/shareAI-lab/learn-claude-code/tree/7226f555d48900076eba126acf37c9ed5f002b5d)，MIT；已读取 s01 核心循环、s08 分层压缩和许可证。实际移植时附带许可与来源，教学代码中的宿主执行和归档方式替换为本项目沙箱与数据库实现。
- [Codex App Server](https://learn.chatgpt.com/docs/app-server)：会话、轮次、事件、执行审批与用户输入协议。
- [Claude Agent SDK 能力](https://code.claude.com/docs/en/agent-sdk/overview)、[权限](https://code.claude.com/docs/en/agent-sdk/permissions)：公开可配置的工具、权限与扩展能力；文档行为和可移植源码分别核实。

已读取的源码对应点：

- [Codex write_stdin](https://github.com/openai/codex/blob/e3fe1ade2ca4cfcea0c192317beefa38c0ff1b5a/codex-rs/core/src/tools/handlers/unified_exec/write_stdin.rs)：已有执行会话的输入、轮询和完成反馈。
- [Codex request_user_input](https://github.com/openai/codex/blob/e3fe1ade2ca4cfcea0c192317beefa38c0ff1b5a/codex-rs/core/src/tools/handlers/request_user_input.rs)：提问与答案关联并独立回传。
- [Claude types.py](https://github.com/anthropics/claude-agent-sdk-python/blob/3be907edb9c86269d30d1427a77937dd788d6419/src/claude_agent_sdk/types.py)：permission behavior、工具关联信息、Hooks 和子 Agent 配置。

下列名称为 VibeEval 本地适配，不声称与上游所有工具名、协议和策略细节完全一致。

## 两个分支的目标矩阵

| 能力 | 本地目标 | 落点与顺序 |
| --- | --- | --- |
| 原生模型调用、工具消息、会话与压缩 | 已有基础，继续补齐真实业务接入 | 核心分支已验收；集成层接存储和运行 |
| 用户引用 | 文件/目录/代码段/会话定位，显式文本或未保存片段；独立输入类型 | 集成第 2 批 |
| 工作区查询 | 目录、路径匹配、文本搜索、仓库与依赖定位；不依赖用户先附加引用 | 独立原子服务，第 2 批 |
| 主动阅读 | 文件、行区间、历史、题目、执行输出；来源版本和分页明确 | 独立原子服务，第 2 批 |
| 阅读与上下文管理 | 记录文件/跨会话材料的实际版本与范围；主动、自动压缩及超限补救；压缩后可重读 | 核心任务，见上下文设计；第 1 批定契约，第 2/3 批打通 |
| 工具与用户交互 | 自动继续、等待批准、拒绝、等待补充回答；对外事件和答复接口 | 第 3 批 |
| 沙箱命令 | 前台/后台、轮询、stdin、终止、退出码、输出流与截断 | 第 4 批，复用绑定沙箱 |
| 文件变更 | 创建/编辑/补丁/删除/移动等受控工作区操作，版本冲突和变更回执 | 第 4 批；按具体操作逐项验收 |
| 测试与本地 Git | 运行测试，读取 status/diff/log，受策略控制的本地仓库操作 | 工作区工具与命令适配 |
| 模型流式 | 文字/工具参数增量、最终响应与中断；完整参数前不执行 | 第 5 批，核心接口独立扩展 |
| 会话恢复与分叉 | 多会话、恢复、分叉、显式重试、上下文材料与压缩 | 第 5 批，不改写源历史 |
| 文件检查点 | 记录变更并受版本保护地恢复，与聊天/压缩检查点分开 | 第 5 批，独立授权 |
| 计划与澄清 | 计划进度、结构化提问和答案；不索要内部推理 | 完成基础闭环后独立批次 |
| 项目说明与 Skills | 工作区说明文件、技能目录与按需读取；不能提升考试权限 | 增强批次，服务端启用来源 |
| Hooks | 调用前后、失败、会话与压缩事件；不能绕过现有策略 | 增强批次，受控执行 |
| MCP 与动态工具 | 工具/资源发现与调用适配，复用策略、回执和事件 | 增强批次，显式注册来源 |
| 子 Agent | 创建、输入、等待、中断、结果汇总，继承访问范围与预算 | 后续增强批次；不先另建调度框架 |
| 本地富内容 | 图片/文档等按模型及解析器能力显式启用；类型不支持时明确返回 | 在文本闭环后逐类接入 |
| 联网搜索 | 不提供 WebSearch/WebFetch 搜索取材能力 | 不在目标范围 |

“完整接入”先验收真实后端闭环，再对增强能力逐项实现和标记。只定义类型或接口不能算该功能完成。
接口示例、集成测试和真实环境回执共同作为证据；不同上游未公开或无法验证的行为明确记录差异。

## 必须独立的三个概念

1. 用户引用：用户表达希望模型关注什么，可以只有定位信息。
2. 查询/阅读：Agent 在授权工作区主动寻找和取得信息的真实工具行为。
3. 策略/执行状态：每个真实操作的 allow/ask/deny，以及等待、执行和最终回执。

引用不授予新权限；未引用的工作区文件也能在允许范围内查询阅读。
查询命中不是正文已读；材料曾经读取也不等于其正文仍在当前模型窗口。
读取工具可以按策略直接执行或等待批准，不能让前端通过工具名猜测决定。

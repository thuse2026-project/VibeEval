# 基本 Agent 后端封装交接

2026-11-08。分支 `feat/agent-integration`，包含 `feat/llm-api@8c25e65` 的核心依赖。
本次交付基本后端封装，供组员接入考试业务和前端；不涉及前端实现或生产部署。
完整扩展目标继续保留在 [接入计划](agent-integration-plan.md)，不作为本次分支推送的前置条件。

## 已完成的基本链路

考生身份 → 考试归属 → 多条 Conversation → 用户输入/引用 → Turn/Run →
模型原生 tool call → 策略/必要批准或提问 → 真实来源/沙箱适配 →
持久回执 → 同一 tool_call 的 ToolResult → 模型续调 → 历史、用量和事件查询。

| 能力 | 当前交付 |
| --- | --- |
| 模型与 Agent 核心 | 无状态模型调用、全局 prompt、原生工具 schema/解析、会话/Turn/步骤、快照和压缩 |
| 会话与范围 | PostgreSQL 归属、并发版本、幂等输入、运行租约、预算、中断和已知/未知用量 |
| 引用与动态阅读 | 文件/目录/选区/材料/同考试其他对话；真实查询阅读与用户附加引用分别记录 |
| 上下文 | 材料版本、当前窗口清单、主动/被动压缩、明确超窗后的有界补救、压缩后重读 |
| 工具交互 | chat_only/read_only/confirm_actions/sandbox_auto；读取和操作策略、批准/拒绝、提问与答复 |
| 命令 | 独立 runsc helper、输出/退出/截断、超时、查询/取消、按原操作核查且不自动重发 |
| 单文件修改 | 版本化 write_file/精确 apply_patch、修改前后材料及 diff、暂停归属、真实结果与当前版本分离 |
| 上游协议 | Cookie/来源头、会话/Run/操作/材料/上下文 HTTP、SSE 持久事件与对话游标续读 |

模型调用层保持无状态，不拥有沙箱或权限。
服务端协调器维护会话、访问范围、策略和运行状态；浏览器不提交模型响应、执行成功回执或整份会话覆盖。

## 装配入口

对外主要对象为 `RuntimeCoordinator`、`RunPolicy/RunInput`、`SourceReader`、
`MaterialStore/PostgresSessionStore` 和已有 `SandboxRuntime/WorkspaceReader`。
原子核心继续从 `app.agent` 使用；业务层不复制一套会话协议。

以下工厂示例用于已有 Linux/runsc 服务端，使用同一 PostgreSQL factory：

```python
from app.agent_integration.material_store import MaterialStore
from app.agent_integration.runtime import RuntimeCoordinator
from app.agent_integration.runtime_types import RunPolicy
from app.agent_integration.sources import SourceReader
from app.agent_integration.store import PostgresSessionStore
from app.db.lifecycle import SessionLifecycle
from app.db.session import get_session_factory
from app.exams.postgres import PostgresEntryStore
from app.llm.config import load_settings
from app.main import create_app
from app.sandbox.runtime import create_submission_sandbox
from app.workspace.service import WorkspaceReader

def build_app():
    factory = get_session_factory()
    sandbox = create_submission_sandbox()
    reader = SourceReader(
        MaterialStore(factory),
        workspace=WorkspaceReader(SessionLifecycle(factory), sandbox),
    )
    runtime = RuntimeCoordinator(
        reader, settings=load_settings(),
        policy=RunPolicy(mode="confirm_actions"),
    )
    return create_app(
        entry_store=PostgresEntryStore(factory),
        submission_sandbox=sandbox,
        agent_enabled=True,
        agent_store=PostgresSessionStore(factory),
        agent_reading=reader,
        agent_runtime=runtime,
    )
```

先执行 Alembic `upgrade head`（当前 `0010`），配置服务端模型与沙箱环境，
再按已有考试流程准备工作区、开考并使用原考生 Cookie 调用 Agent。
模型设置只进入服务端客户端，不注入候选命令容器。
默认 `create_app()` 的 Agent 开关关闭，默认 RunPolicy 为 read_only；执行模式由服务端显式启用。

题目说明、依赖文档和题目自带的历史通过 `SourceReader(text_sources=...)` 注册逻辑来源；
提供器实现 `resolve(scope_id, source_id) -> SourceSnapshot`，自行核查归属、固定版本和允许内容。
未注册来源明确返回不可用，任意宿主路径或外部 URL 不会自动成为阅读范围。
当前任务文件范围沿用发布清单，结构化修改仅开放两个已有可写文件。

## 上游调用顺序

统一前缀 `/api/v1/sessions/{session_id}/agent/conversations/{conversation_id}`；
使用原考生 Cookie 和 `X-VibeEval-Request: 1`。

1. 创建对话，按需提交 references；定位引用不自动全文读取。
2. `POST /turns` 提交 text、固定 request_key、reference_ids 和收窄后的 mode，获得 queued Run。
3. `POST /runs/{run_id}/advance` 推进，直到完成、等待交互/执行或明确停止。
4. 等待批准/提问时，读取 interactions 并答复，再 advance 同一 Run。
5. waiting_operation 时查询原 operation/输出或再次 advance；不重新提交原输入来恢复执行。
6. 从 history 取回答，operations 取命令/文件实际回执，context 取当前窗口预览；
   订阅 events/stream 接收已提交事件，并保存完整事件 id 以便续读。

advance 是显式调度入口；本版本没有常驻 Agent worker，订阅本身也不会推进运行。
原生输入、批准、取消和查询可由后续调用方组合，服务端继续负责权限与幂等。
结果 unknown 时保存原操作 ID 进行核查；文件字节恰好相等不等于已证明那次写入成功。

| 详细契约 | 内容 |
| --- | --- |
| [第三批 B](agent-integration-stage3b.md) | Run、模型尝试、输入绑定、批准/提问与上下文闭环 |
| [第四批 A](agent-integration-stage4a.md) | 命令、操作状态、输出、取消和未知结果 |
| [第四批 B](agent-integration-stage4b.md) | 文件提议、精确补丁、审批 diff、实际版本与暂停恢复 |
| [第五批 A](agent-integration-stage5a.md) | SSE 帧、游标、重连、心跳和连接预算 |

## 验证与后续边界

最后完整后端回归为 563 项：548 项通过，15 项真实沙箱用例按环境跳过；
真实隔离 PostgreSQL、模型 MockTransport 和实际 loopback HTTP 传输已验证。
行覆盖率 87.0%，Black、Ruff、依赖检查及密钥扫描通过。
真实模型 + Linux/gVisor 的生产联合验收仍待完成，推送本分支不表示已部署或已完成该验收。

供应商文字/工具参数增量流、会话分叉/显式重试、文件恢复检查点、交互 stdin/常驻后台任务，
以及 Skills/Hooks/MCP/子 Agent 等属于后续扩展。SSE 当前发送持久事件，完整回答仍从 history 取得。
本次按基本封装收口，组员可依据上述对象和 HTTP 契约继续接入。

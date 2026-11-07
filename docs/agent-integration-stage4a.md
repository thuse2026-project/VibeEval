# Agent 接入第四批 A：沙箱命令与持久执行回执

状态：2026-11-07，用户已确认，本地提交，未推送。
第三批 B 已确认并提交 `236967e`。本批只改后端；结构化文件修改/补丁留给第四批 B。

## 本批链路

原生 `run_command` → 服务端策略 → 必要批准 → 工作区版本复核 →
持久 operation → 独立 runsc helper → 真实输出/退出回执 → 原 ToolResult → 模型续调。

继续复用核心 `ToolRegistry/ToolCall/ToolResult`，不增加模型返回格式。
`operation_id` 同时是公开的 `execution_id`，绑定 Run、Turn、step、tool_call 与考试归属。
浏览器不能指定容器、镜像、宿主路径，也不能提交执行结果。

## 模式与工具

| mode | 行为 |
| --- | --- |
| chat_only | 不注册自主工具 |
| read_only | 查询/阅读、提问、压缩与读取已有命令输出 |
| confirm_actions | 增加 run_command，每次操作都需要批准 |
| sandbox_auto | 增加 run_command，沿用服务端 action_decision=allow/ask/deny |

服务端 `RunPolicy` 默认仍是 read_only；浏览器只能收窄模式。
action_decision=deny 在两种操作模式下都优先拒绝。
只有配置了执行后端且获准使用操作模式时，模型才会收到命令工具 schema。
策略、模型与 schema 指纹随 Run 固定。升级后旧活动 Run 若工具指纹变化，明确停止为
`TOOL_PLAN_CHANGED`，不会直接套用新工具计划续调。

内部装配示例，`reader/settings` 沿用已验证的数据库、阅读源与模型设置：

```python
from app.agent_integration.runtime import RuntimeCoordinator
from app.agent_integration.runtime_types import RunPolicy

runtime = RuntimeCoordinator(
    reader,
    settings=settings,
    policy=RunPolicy(mode="confirm_actions"),
)
# create_app(agent_runtime=runtime, ...)；自动发现 reader.workspace.sandbox 的执行接口。
# 如需自动执行，由服务端设置 mode="sandbox_auto", action_decision="allow"。
```

原生参数示例：

```json
{
  "argv": ["python", "-m", "unittest", "discover", "-s", "tests"],
  "cwd": ".",
  "timeout_seconds": 30,
  "max_output_bytes": 32768,
  "expected_versions": {"src/booking.py": 2}
}
```

`argv` 为显式参数数组，默认不经过 shell；需要 shell 语法时显式传入 `/bin/bash -lc`。
工作目录仅允许 `.`、`src`、`tests`。单次命令最多 120 秒，由服务端上限和 Run/考试截止继续收窄；
每个 Run 默认最多 16 项操作。stdout/stderr 各最多保存 32768 原始字节，继续排空但丢弃超出部分，
并记录实际观察到的字节数、截断与完整性。

## 批准、版本与工作区协调

批准沿用 `/runs/{run_id}/interactions/{interaction_id}/approval`，
绑定实际原生调用、参数摘要、策略、沙箱/题目绑定、operation ID 与当前六个发布文件的版本及摘要。
批准请求返回 arguments、expected_versions、operation_id、policy_decision 与 reason_code。
拒绝或批准到期不创建命令容器；重复批准不会创建新操作。

恢复时重新读取工作区版本；变化则返回 `SOURCE_VERSION_CHANGED`，不执行。
内部版本预检不伪造模型 ReadObservation。helper 启动子进程前再按文件摘要复核，
失败返回冲突。`expected_versions` 可进一步固定模型已观察的版本。

同一考试只有一个持有工作区的 Agent operation，数据库部分唯一索引加固；
另一条对话的命令和人工文件保存会被拒绝为忙，普通读取仍可进行。
现有人类 PTY 仍能修改允许写入的文件：版本检查不构成跨终端的文件系统事务，
不能承诺预检完成后文件绝对静止。

命令本身能修改当前任务允许 UID 1000 写入的文件；本批没有单独的文件 diff 批准、
结构化保存、补丁应用或恢复工具。后续版本观察仍根据实际文件摘要递增。
命令回执 `starting_versions` 表示执行前版本，不冒充执行后版本。

## gVisor 执行边界

每项命令使用独立命名 `vbe-op-<operation_id>` helper，与原考试容器共享已绑定的工作区数据卷。
仅复用现有本地不可变任务镜像，不拉取镜像、不连接网络，不向沙箱注入模型 Key 或宿主环境。

helper 使用 runsc、只读根文件系统、禁网、512 MiB 内存、64 PID、半核 CPU 和 64 MiB `/tmp`。
固定 Python `-I` supervisor 是 root PID 1，只持有 SETUID/SETGID/KILL；候选命令降为
UID/GID 1000，无附加组、无新权限，并设置文件尺寸和打开文件数限制。

supervisor 将原始输出前缀和进度原子写入独立 `/control` 卷，目录 root 0700、回执 root 0600；
候选命令不能读写该回执。Docker 守护进程的 helper 停止状态才是进程已停止的依据，
不会仅凭命令输出里的“成功”或某份 JSON 释放工作区。

独立 helper 不共享原终端的进程、环境变量或 `/tmp`；后台进程在本次 helper 结束时一并结束。
长前台任务可以等待并轮询，但不提供交互 stdin 或跨命令存活的后台服务。
取消/超时结束整项 helper，不停止原考试终端。

## 持久化与恢复

迁移 `0009` 新增 agent_operations，并将 waiting_operation 纳入活动 Run 唯一约束。
锁顺序保持考试 → 对话 → Run → operation；Docker I/O 不持有这些数据库锁。

| operation 阶段 | 恢复规则 |
| --- | --- |
| requested / waiting_approval | 尚未创建执行容器；等待策略或批准 |
| preparing | 创建同名的停止容器，可安全恢复创建，不启动候选代码 |
| prepared | 句柄已入库，允许一次派发认领 |
| dispatching / running | 派发意图已入库；仅查询相同容器，永不再次 start/restart |
| succeeded / failed / timed_out / cancelled / conflict / denied | 保存原生结果，重复 advance 复用回执 |
| unknown | 明确保留结果未知；无停止证据时继续持有工作区，可查询/取消 |

先提交 dispatching 再调用一次 start；RPC 超时、进程退出或数据库失败后不重放命令。
即便原 helper 仍是 created，也不能推断从未接受启动而重发；取消 created 会删除该确切容器，
使迟到的 start 无法再生效。这里提供“同一 operation 不自动重发”的保证，
不宣称 Docker 与 PostgreSQL 具备跨系统 exactly-once 事务。

命令未结束时 Run 为 waiting_operation，原生调用保持未闭合，不把“已排队”当执行结果送给模型。
再次 advance 查询回执，确认结束后补齐同一 tool_call_id，再调用模型。
只查询 operation 不会发模型请求，也不会启动命令。

网络不可用且无法取得回执时 Run 停止为 unknown，保留已记录输出并尝试取消；
只有容器已停止/确切句柄已不存在等证据才能释放工作区。
用户中断或恢复推进发现 Run 到期时，也会取消已有操作。
没有部署驻留 Agent worker；无人 advance 时 Run 状态可能滞后，helper 自身仍执行命令/考试截止上限。

交卷先暂停原考试容器，再停止命令 helper，确认停止后才导出工作区。
提交清理同时删除匹配归属的 helper 和控制卷，不恢复原容器。
若清理先于输出入库，后续保留未知/不完整结果，不伪造成功，也不重跑。

## 输出与上下文

执行结束后 stdout/stderr 分别成为不可变 material。原生结果包含退出码、状态、时间、
完整性、两个 material_id 与有限预览；序列化总长不超过 16000 字符。
非零退出属于真实程序结果，模型可以据此继续；基础设施失败、未知或版本冲突明确标错。
stdout 是候选环境产生的不可信数据，不作为可信评分报告。

`read_execution_output` 可按 execution_id、stream、offset、max_chars、expected_revision 读取。
同一考试的其他对话可读取，其他考试拒绝；不会合并两条会话的状态或消息历史。
实时前缀每次观察有 revision；指定旧 revision 时冲突，不拼接不同捕获版本。
可改读固定 material_id，取回曾经保存的不可变材料。

字节输出按 UTF-8 替换非法序列，NUL 显示为 `\0`，原始字节摘要和 base64 前缀仍在私有回执中保存。
HTTP 阅读 offset 是转换后文本的 Unicode 字符偏移；事件输出的 byte_offset 是原始字节偏移，不能混用。

命令结果预览与后续输出阅读都进入模型/压缩的材料清单；压缩后标为 compacted，
不声称完整正文仍在窗口。自动回传的命令回执不是自主阅读，不额外伪造 read 记录。

## HTTP 与事件

沿用考生 Cookie、`X-VibeEval-Request: 1`、Agent 开关与 no-store。
前缀为 `/api/v1/sessions/{session_id}/agent/conversations/{conversation_id}`。

| 方法 | 后缀 | 作用 |
| --- | --- | --- |
| GET | /operations?offset=0&limit=20 | 分页查看操作，含等待原因、参数、批准决策和退出状态 |
| GET | /operations/{operation_id} | 观察并保存最新回执；不启动、不重新派发 |
| POST | /operations/{operation_id}/cancel | 幂等取消原操作 |
| GET | /operations/{operation_id}/output | stream=stdout/stderr，offset、max_chars、expected_revision；读取已保存前缀 |

公开响应不含 Docker 句柄、内部容器/卷或模型凭证。
`operation.requested/preflight/prepared/dispatching/output/cancel_requested` 及终态事件进入现有事务事件表。
`operation.output` 携带 stream、byte_offset、data_b64；先持久化再返回，仍以 after_sequence 补取。
这是轮询驱动的输出增量记录；尚未提供 SSE/WebSocket 命令输出推送或模型流式。

## 验证

使用真实隔离 PostgreSQL、MockTransport 模型和 Docker SDK 替身验证后端闭环，不产生模型费用。
命令生命周期、批准/拒绝、陈旧版本、跨会话协调、输出版本、创建/启动丢回包、
取消、中断、到期、未知结果与工作区门禁均有测试。
另有 Linux/runsc 验收用例覆盖 UID、私有回执、真实输出截断、超时/脱离子进程、取消和启动前摘要冲突。
当前 Windows 环境未执行真实 gVisor 命令，不将模拟通过视为生产联合验收。

```powershell
# backend/；创建并清理自有临时 PostgreSQL，不改现有 .env 或开发数据库
..\.venv\Scripts\python.exe tests/manual/agent_database_probe.py --all-tests
```

Linux 验收须显式设置 `VIBEEVAL_RUN_GVISOR_TESTS=1`、本地 Unix Docker socket 和
预构建不可变 `VIBEEVAL_GVISOR_IMAGE_REF`；直接运行 `test_sandbox_execution*.py`。
通用临时数据库 probe 会禁用真实沙箱测试，不会自动降级为 runc。

2026-11-07 最终验证：完整后端 508 项，496 项通过，12 项真实沙箱用例跳过，
PostgreSQL 用例全部启用；新增 43 项中 39 项通过，4 项 Linux/runsc 用例待实机验收。
迁移升降级及数据库/模型一致性通过；全应用行覆盖率 87.5%，操作服务 90.0%。
Black、Ruff、依赖检查和密钥扫描通过；未修改前端，临时测试 PostgreSQL 已清理。
本批已获用户确认并本地提交，未推送。

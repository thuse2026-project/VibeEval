# Agent 接入第二批：引用、查询阅读与版本化材料

状态：2026-11-06，用户已确认，本批本地提交，未推送。第一批已确认并提交为 `add28b6`。

## 与上一分支的边界

`feat/llm-api` 的 `app.agent` 已负责全局 prompt、工具 schema、原生 `tool_calls` 解析、
`ToolResult` 配对、Conversation/Turn/ModelStep 状态转换及模型续调。
文件阅读、查询、命令和修改共用这套协议，不另定义模型 action JSON 或另一份 messages。

本批只在 `app.agent_integration` 注册实际可用的阅读工具，并处理来源归属、版本与范围、
处理器、策略入口、不可变材料、真实阅读记录和用户引用 HTTP。
没有修改 `app.agent` 的 prompt、解析器或状态协议。

```text
prepare_model_request(同一个 ToolRegistry)
→ call_model / accept_response（原核心）
→ ReadToolService.execute（校验已持久化的真实 pending call）
→ 策略 / 来源处理器 / 材料与记录事务
→ 原 ToolResult
→ supply_tool_results / save（原核心与存储）
→ 下一次 prepare_model_request / call_model
```

阅读服务返回结果后不会自行修改核心会话或调用模型。下一批编排器负责这一段推进，
避免阅读记录和核心 state_json 各自推进出两份消息历史。
阅读回执先存储，核心结果尚未回填时可以按原 step/call 取回同一回执。

## 本批已实现

### 用户引用

`InputReference` 支持六种输入：

| kind | 输入 | 行为 |
| --- | --- | --- |
| file | path、expected_version?、selection? | 保存定位，读取时才检查实际版本并取材 |
| directory | path，根目录为 `.` | 保存定位，读取时返回目录索引 |
| selection | path、content、base_version?、selection? | 只保存选定文本、原缓冲区摘要与范围；标明 unsaved |
| conversation | conversation_id、expected_revision? | 校验同一考试归属；读取时取得该版本的消息页 |
| material | material_id | 复用同一考试已存在的不可变材料 |
| source | source_id、expected_version? | 经服务端注册来源适配器校验；未装配时明确不可用 |

定位引用不读取工作区文件。引用创建、列表或材料预览不生成 Agent 阅读记录，
也不会自动加入模型窗口或绑定一个尚不存在的 Turn；用户消息与引用的绑定留给下一批。
用户提供的未保存缓冲区只落所选片段，未选正文不会作为材料保存。

行范围为 1-based、首尾行均包含；UTF-16 选区为 1-based 行列、末端不包含。
切开 emoji 代理对、越界或反向选择会失败，保留原始 CRLF。

### Agent 原生工具

| 工具 | 结果与约束 |
| --- | --- |
| list_directory | 当前授权工作区的直接子项，目录索引不算正文读取 |
| glob_files | 路径模式、分页和文件版本，不开放宿主目录 |
| search_content | 字面量搜索、逐行摘录、实际观察的逐文件版本；命中不算全文读取 |
| read_file | 文件全文快照 + 本次选区/分页片段；可检查 expected_version |
| list_conversations | 同一 ExamSession 内的对话元数据索引 |
| read_conversation | 保留的公开消息分页、最新摘要或指定 step/call 的工具结果 |
| read_material | 重新读取不可变材料，源已变化时仍能得到旧内容 |
| read_reference | 读取当前对话的引用；文件选区不会自动扩大为整文件 |
| read_source | 调用服务端提供的 TextSourceProvider；只有配置后才向模型声明 |

没有工作区处理器时不声明工作区工具；文件读和树查询复用现有 `WorkspaceReader`，
从绑定沙箱取数据并使用共享文件版本表。仍限当前题目的六文件发布清单。
依赖库、题目历史或运行输出可通过服务端 `TextSourceProvider.resolve(scope_id, source_id)` 接入；
本批提供这一端口，尚未自动接通这些实际数据源，不把任意 URL 或本地路径当成来源。

跨对话不合并两边状态或配置，消息包含稳定 turn/step/call 派生 ID，
不返回系统 prompt、模型 Key 或供应商 opaque continuation。
可固定 expected_revision；源发生变化返回 `SOURCE_VERSION_CHANGED`，
已捕获消息页可按 material_id 读取旧版本。压缩覆盖过的原始 Turn 仍可读取。
消息页最多五条；大消息页的文本仍按字符片段返回，后续先读完同一 material，
再根据 source_metadata.next_message 获取下一页。

工具结果仍是原 `ToolResult`，其 content 为 JSON 字符串，包含 material_id、来源版本、
源/材料/本次片段摘要、实际字符与 UTF-8 字节范围、text、has_more/next_offset 等。
字符 offset 按 Unicode codepoint 计数；不同于编辑器输入的 UTF-16 列。
text 默认 12000、最多 16000 字符，JSON 转义可能再缩短片段；以实际 next_offset 为准。
材料最多 8 MiB；目录/搜索页最多 100 项，搜索摘录最多 300 字符。
不完整文本明确标记 truncated/has_more，不静默当作整份正文。

### 权限、幂等与记录

- 内部 execute 只接受 scope、conversation、step、call ID，从数据库取真实原生调用，
  不能靠传入一份伪造 ToolCall 或浏览器成功回执绕过状态检查。
- `ReadDecision` 为 allow/ask/deny。ask 在读取前抛 `ReadingApprovalRequired`，不读文件、不伪造 ToolResult；
  deny 保存错误 ToolResult 和拒绝记录。持久批准请求、批准恢复与对外等待状态是下一批内容。
- 同一 conversation/step/tool_call 返回同一已保存回执；重新读取新版本要使用新工具调用。
  并发请求可能各自完成只读 I/O，但只产生一份逻辑回执和事件；这个模式不能照搬到命令/修改。
- 中断或交卷先发生时，不接受新的迟到阅读记录；新取材也受截止门禁约束。
  已保存材料可继续预览，不强行访问已冻结的活跃沙箱。
- `ReadObservation` 保存本次真正返回的片段，区分 query/read，关联核心 turn/step/call。
  完整材料保留用于后续读取，不表示已经把全文发送给模型。
- 记录与 `tool.read.completed` 事件同一事务提交；材料与引用使用包含考试归属的复合外键。

迁移 `0007` 新增 `agent_materials`、`agent_input_references`、`agent_read_observations`。
快照内容去重只发生在同一考试内，材料摘要读取时重新校验。
新记录与事件不增加核心 revision；核心状态仍只通过原 CAS save 推进。

## HTTP

前缀 `/api/v1/sessions/{session_id}/agent/conversations/{conversation_id}`，
复用原考生 Cookie、`X-VibeEval-Request: 1`、Agent 开关与同一 PostgreSQL。
保持 `Cache-Control: no-store`，本批不新增前端。

| 方法 | 后缀 | 输入/结果 |
| --- | --- | --- |
| POST | /references | InputReference，201 返回 reference_id、reference、material_id? |
| GET | /references | offset/limit，返回 items/has_more |
| GET | /references/{reference_id} | 单条当前对话引用 |
| GET | /materials/{material_id} | offset/max_chars，返回不可变材料片段及来源 |
| GET | /reads | offset/limit，返回实际阅读或拒绝记录及原 ToolResult |

这些端点不能接收模型响应或工具成功回执，也不会调度 Agent 工具。
不存在的来源 404，源版本变化 409，非法选区/范围 422，未装配来源 503；
归属、开关、截止等沿用第一批错误语义。
已配置沙箱时复用其文件读取；自定义来源由服务端通过 `create_app(agent_reading=SourceReader(...))` 注入。
内部服务同样必须使用服务端验证的 scope，不接受模型参数扩展归属。

## 验证与后续

`backend/tests/test_agent_integration_reading.py` 包含真实 PostgreSQL、真实 WorkspaceReader + 模拟沙箱 I/O、
HTTP 和两个模型请求的 httpx.MockTransport 链路；没有真实模型费用或真实 gVisor 执行。
临时数据库验证沿用第一批 probe，不改开发数据库或根 .env。

```powershell
# backend/
..\.venv\Scripts\python.exe tests/manual/agent_database_probe.py --all-tests
```

2026-11-06 验证：完整后端回归 408 项，400 项通过，8 项真实沙箱用例按配置跳过；
最终修正后另跑本分支 36 项，全部通过，其中 27 项使用隔离 PostgreSQL。
迁移升降级及模型一致性通过，Black、Ruff、依赖检查与密钥扫描通过。
临时数据库容器已清理，现有开发数据库未迁移。

本批不包含 Turn 自动循环、持久审批/提问、命令/补丁执行、窗口清单或主动/被动压缩调度。
这些仍按 [全流程计划](agent-integration-plan.md) 后续逐批实现；
材料可追溯与阅读日志不能替代“正文是否仍在当前模型窗口”的管理。

# Agent 接入第三批 B：持久只读运行与上下文闭环

状态：2026-11-07，用户已确认，本地提交，未推送。
第三批 A 已确认并提交 `925802e`。本批只改后端。

## 已接通的链路

考生身份 → 原始输入与引用绑定 → queued Run → 明确 advance →
窗口预算/必要压缩 → 持久模型请求 → 原生工具调用 →
策略/必要批准或提问 → 实际阅读 → ToolResult → 模型续调 → 完成或明确停止。

一次 advance 会在服务端自动推进多次模型/工具交换，直到完成、等待交互、失败、
预算耗尽、中断或到期。API 不让浏览器提交模型响应、工具成功回执或任意会话覆盖。
队列入库与调度入口分开；本批提供显式 HTTP advance 和 Python 协调器，
没有自动常驻后台 worker 或 SSE/模型增量流。调用方可轮询原有 events/runs/history；
queued 或可恢复的作业由调用方再次 advance，不能靠重提用户输入恢复。

本批支持 chat_only/read_only。真实命令、写文件和补丁属于第四批，不在这里声明为已实现。

## 核心与业务层的职责

- `app.agent` 继续拥有原生调用协议、会话状态与窗口准备。本批补充独立的 input_context，
  以及供应商明确拒绝超窗后释放未回答步骤的专用转换，不新建消息历史或模型返回协议。
- `RuntimeCoordinator` 负责有预算的只读循环、工具分派与压缩触发。
- `RunStore` 负责短事务、租约、请求/结果/交互和核心状态的原子落库。
- 真实取材沿用 `SourceReader/ReadToolService/MaterialStore`，不读 API 宿主文件，不给沙箱模型 Key。
- 窗口整理和压缩沿用第三批 A；全局 Agent prompt 仍为版本 1。

原有原子函数可继续使用；新 managed Run 不接管同 request_key 的旧非托管 Turn，
也不允许另一 managed Run 并行修改同一对话。

## 三张持久表与事务

迁移 `0008` 新增：

| 表 | 记录 |
| --- | --- |
| agent_runs | 原始输入摘要、Turn 关联、固定策略/模型/工具指纹、预算计数、状态、期限和 worker 租约 |
| agent_attempts | 每次普通模型或压缩调用的准备产物、窗口清单、派发意图、过滤后的响应、用量及采用状态 |
| agent_interactions | 与原 step/tool_call 和参数摘要绑定的批准或提问、答复、到期及解决状态 |

一个对话最多一个 queued/running/waiting 的 Run，一个 Run 最多一个 prepared/running 的供应商请求；
数据库部分唯一索引加固这两个约束。组合外键固定 run/conversation/exam 归属。
写操作统一按考试 → 对话 → Run 取得短锁；调用模型或读取沙箱时不持有这些锁。

模型准备与核心 start_step 同事务保存；响应、用量、核心采用和事件同事务提交。
摘要采用同时要求来源 revision/摘要一致，并且实际投影窗口缩小。
数据库提交失败时不把已发出的模型请求重新发一次来猜测结果。
窗口清单的 dispatched 表示已提交派发意图，不表示供应商一定收到或一定计费。

请求产物中包含实际消息/工具/参数，供服务端审计，不含 Key 或鉴权头。
数据库保留核心需要的 opaque continuation；对外 attempt 查询不返回请求正文、响应正文或这些内部续调数据。
公开回答和历史仍从已有 history 接口取得。

## 输入与引用

`POST /turns` 接收原始 text、request_key、reference_ids 和收窄后的 mode。
原文保存在 `TurnState.user_message`，引用材料由服务端生成 `input_context`，分别保存，
API 不接受客户端覆盖 input_context、系统 prompt、预算或已批准标记。

- reference_ids 最多 20 个，必须属于当前对话/考试；重复或越界 ID 拒绝。
- 文件/目录/其他会话是定位信息，不在提交时全读。
- 用户明确提供的未保存选区可作为输入预览：每份最多 2000 字符、总正文预算 8000 字符，
  标注 material_id、摘要与 has_more。没有进入输入的余量仍可按材料重新读取。
- 这类显式输入不伪造 ReadObservation；自主阅读仍走真实原生调用。
- request_key 绑定原始文本、引用 ID 和模式；相同重提返回原 Run，不新建 Turn 或重复计费。
- input_context 进入历史和摘要来源校验。旧无附件 Turn 的摘要算法保持兼容。

窗口清单新增 input_references，与真正通过工具阅读的 materials 分开。
当前输入预览、完整材料、定位引用和已压缩历史不会混为“已读全文”。

## 模型与预算

Run 固定运行策略、工具 schema 指纹、模型及 WindowPolicy；下一次派发前核对指纹。
客户端可选择 chat_only，不能把服务端 chat_only 扩大为 read_only，不能设置执行模式。
chat_only 不提供任何模型自主工具，仍可分析用户明确提供的输入片段。

默认上限为普通模型请求 8 次、压缩请求 2 次、逻辑工具调用 32 次、已知总 token 64000，
Run 墙钟期限 900 秒，租约 180 秒，交互等待上限 300 秒。实际读取仍受考试截止和交卷门禁约束。
这些是服务端可注入配置，不能由浏览器加大。

用量来自过滤后的供应商计数，包括未采用或截断摘要、失败请求中可用的计数和迟到结果。
核心 state.usage 仍只统计已接受响应/检查点；Run 的尝试账本另外覆盖未采用调用，两者不要相加重复计数。
缺失计数保留未知：known_total_tokens 只累计已知值，usage_complete=false 时 total_tokens=null。
未知不能当作免费调用，因此另外用调用次数、工具次数、墙钟期限和窗口硬预算限制运行。
预算达到上限后不会再读取下一文件或再调用模型。

## 主动、被动与超限压缩

三类触发统一写入 attempt.trigger：

1. 主动：空闲对话创建 compact Run；运行中提交 compact 意图；或模型原生调用 compact_context。
2. 被动：每次准备下一模型步骤前检查窗口软预算，选择第三批 A 的安全前缀压缩。
3. 超限补救：仅在供应商明确返回结构化 context_length_exceeded 时，允许一次压缩和重新准备。

模型 compact_context 的 ToolResult 只确认“已排程”，不伪造成功；实际采用由事件和检查点确认。
等待工具/批准/提问时的压缩请求延期到安全边界，不丢掉等待链。
模型最终回答期间收到的手动压缩意图也会在回答落库后处理。
没有可压缩旧材料时记录跳过；超窗又没有安全边界时明确 capacity_exceeded。
压缩失败、输出截断或没有缩小实际窗口时不替换原窗口，记录失败及已知用量并停止本次 Run。

普通网络错误、401/429、输出 length、仅在错误文字里出现“超窗”，都不触发自动重发。
当前适配识别兼容协议的 error.code，以及 OpenRouter 的 error.metadata.error_type；
不把 token_limit_exceeded 这类 token/额度预算错误当成上下文长度错误。
字段依据见 [OpenRouter 官方错误协议](https://openrouter.ai/docs/api_reference/errors-and-debugging)。

明确超窗拒绝后，核心只释放尚无回答的逻辑步骤，已派发请求及拒绝仍在 attempt 表中，
没有删除已接受消息或工具结果。补救次数固定最多一次，第二次拒绝停止。

## 批准与提问

read_only 默认直接读取授权来源，也可由服务端 RunPolicy 将读取设为 ask/deny。
ask 在实际来源读取前持久化 interaction，Run 进入 waiting_approval。
批准绑定 run/step/tool_call、参数摘要、策略摘要、考试归属和到期时间，不能挪给另一操作。
只读请求若没有 expected_version，批准语义是“读取执行时版本”；实际回执记录取得的版本，
不宣称批准期间沙箱文件不会变化。第四批修改型操作需要另加实际版本/执行绑定。

模型 request_user_input 生成 waiting_user_input，用户通过 answer 接口补充信息。
提问答复是该工具调用的 ToolResult，不是执行批准，也不创建另一条伪装的用户 Turn。
拒绝批准或交互到期返回明确工具错误；不会取材，也不会被包装成成功。
重复的相同答复幂等，不同答复冲突；没有相应归属不能答复。

## 重启、中断与未知结果

| 已持久状态 | 再次 advance |
| --- | --- |
| queued | 取得新租约后开始 |
| prepared，未派发 | 旧租约过期后可继续同一个准备请求 |
| running，租约未过期 | 返回忙，不启动第二个 worker |
| 已派发但无回执，租约过期 | 记 unknown，停止；不重发模型请求 |
| 已记录阅读，尚未 supply | 复用原阅读 ToolResult，再推进核心，不伪造新读取 |
| waiting | 等答复，或按 expires_at 处理到期 |
| terminal | 只返回原状态，不重跑 |

worker 的后续派发、结果供应和采用均检查租约。过期 worker 不能覆盖新状态。
迟到模型响应仍可记录过滤后的响应和计数，但不采用到已中断、已过期或来源变化的核心会话。
用户中断取消待答交互，停止续调；已经在途的请求不等于已在供应商取消。
只读 I/O 在并发恢复时可能发生多次物理读取，但同一工具调用只保存一份逻辑结果。
这个模式不能用于第四批命令和文件修改，后者需要独立、可查询的实际操作 ID。

## HTTP 契约

统一前缀 `/api/v1/sessions/{session_id}/agent/conversations/{conversation_id}`。
沿用考生 Cookie、`X-VibeEval-Request: 1`、Agent 开关、共享 PostgreSQL 和 no-store。

| 方法 | 后缀 | 输入/作用 |
| --- | --- | --- |
| POST | /turns | `{request_key,text,reference_ids?:[],mode?:"read_only"}`，202 返回 queued Run |
| POST | /compactions | `{request_key}`，202 创建独立手动压缩 Run |
| GET | /runs | offset/limit 分页 |
| GET | /runs/{run_id} | 状态、预算计数、期限和用量完整性 |
| POST | /runs/{run_id}/advance | 无输入，推进到完成、等待或停止 |
| POST | /runs/{run_id}/interrupt | 无输入，提交中断意图 |
| POST | /runs/{run_id}/compact | 无输入，记录运行中的手动压缩意图 |
| GET | /runs/{run_id}/attempts | offset/limit，过滤后的请求元数据、窗口清单与用量；不含模型消息正文 |
| GET | /runs/{run_id}/interactions | 当前 Run 的问题/批准与解决状态 |
| POST | /runs/{run_id}/interactions/{id}/approval | `{decision:"approve"|"reject"}` |
| POST | /runs/{run_id}/interactions/{id}/answer | `{answer:"..."}` |

典型调用顺序：POST turns → 保存 run_id → POST advance →
若 waiting 则 GET interactions 并提交答复 → 再 POST advance → GET history 取回答。
运行期间可 GET runs/events/context，不需要重新 POST 原输入。
advance 可能等待多次模型请求；客户端等待超时应查询原 Run，不将超时当作未执行并重提任务。

Run 状态包含 queued/running/waiting_approval/waiting_user_input/completed/failed/interrupted/
unknown/capacity_exceeded/budget_exhausted/expired；调用已成功接受后，执行失败通过 Run 状态和 error_code 表达。
身份和输入错误仍返回 404/403/422；运行忙或幂等键冲突 409；数据库/配置不可用 503。
events 使用已有单调 sequence 与 after_sequence；等待、派发、采用、跳过、失败、迟到和终止均有事务事件。

`create_app(agent_runtime=RuntimeCoordinator(...))` 可注入模型客户端与服务端策略；默认复用已配置的阅读源和模型设置。
内部调用方也必须使用服务端验证的 scope，不能直接把浏览器/模型给出的 scope 当作授权。

## 验证与剩余工作

测试使用真实临时 PostgreSQL、原生 httpx.MockTransport、模拟沙箱读取；没有真实模型费用。
覆盖多步读取、批准/拒绝、提问恢复、阈值与主动压缩、一次超窗补救、预算、中断、
重复输入、并发租约、未派发恢复、已派发未知、迟到计数、交卷竞争及事务回滚。

```powershell
# backend/
..\.venv\Scripts\python.exe tests/manual/agent_database_probe.py --all-tests
```

真实 gVisor 命令、修改、取消与执行恢复仍按第四批实现；流式输出、后台 worker 部署和联合故障验收后续推进。
本批只读端到端已由模拟模型与实际数据库验证，不等同于真实模型 + Linux/gVisor 的全功能验收。

2026-11-07 最终验证：新增 35 项测试全部通过（29 项使用真实隔离 PostgreSQL，6 项纯协议测试）。
完整后端回归 465 项，457 项通过，8 项真实沙箱用例按配置跳过；数据库用例已全部启用。
迁移升降级、模型/数据库一致性通过；全应用覆盖率 89.0%，新增运行模块合计 90.8%。
Black、Ruff、依赖检查和密钥扫描通过，前端未改动，临时 PostgreSQL 容器已清理。

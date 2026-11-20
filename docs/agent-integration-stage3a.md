# Agent 接入第三批 A：长 Turn 压缩与可检查的模型窗口

状态：2026-11-07，用户已确认，本地提交，未推送。
上一批已确认并提交为 `a892a67`。第三批按上下文原子能力、运行编排分别验收。

## 本小步的边界

本小步交付可独立调用的上下文投影、预算检查、压缩准备与采用，以及只读窗口查询。
已有压缩 API 调用仍由 `call_compaction` 完成；测试已串联真实 PostgreSQL、模拟摘要请求和模拟模型续调。
原生 tool call/result 格式与全局 Agent prompt 不变。

第三批 B 再把这些函数接到持久 Turn 编排器：保存每次请求窗口、压缩尝试和实际用量，
执行阈值自动维护、显式压缩请求、超窗补救、批准/提问、恢复与自动续调。
本小步的 `plan_compaction(trigger="threshold")` 是阈值规划原子函数，
不是已经运行的后台自动压缩或完整 Agent 循环；窗口查询也不会触发模型请求。

## 核心层：支持一个 Turn 很长的情况

`app.agent` 原来只支持空闲时压缩完整 Turn；现扩展为：

- 空闲或活动 Turn 为 ready 时，可以压缩已关闭的完整 Turn。
- 活动 Turn 为 ready 时，可以继续覆盖其中已经完成的旧工具交换。
- 完整保留当前用户请求，至少保留最新一组工具调用和结果。
- awaiting_model / awaiting_tools 时不准备或采用压缩；等待中的调用不会被摘要替换。

检查点使用稳定覆盖边界：

| 字段 | 含义 |
| --- | --- |
| through_turn_count | 完整覆盖的 Turn 数量 N |
| through_step_count | 后一个 Turn 内覆盖的工具交换数量 K，完整 Turn 边界为 0 |
| through_turn_id | K=0 时为第 N 个完整 Turn；K>0 时为其后部分覆盖的 Turn |
| through_step_id | K>0 时最后一个被覆盖步骤的稳定 ID |
| source_digest | 完整 Turn 前缀及部分 Turn 的身份、用户请求、已覆盖完整步骤的摘要 |

例如 N=0、K=3 表示当前第一轮的前三个工具交换进入摘要；当前用户请求和第四个及后续交换仍在窗口。
K 不能覆盖当前最新交换。部分 Turn 后来继续、结束或进入下一轮，检查点仍能验证：
摘要绑定已经覆盖的内容，不绑定后来可继续增长的尾部状态。

准备、一次 API 调用、采用仍分开。采用需要同一个 conversation/scope、revision、完整状态摘要，
检查点来源摘要也必须吻合；重复采用同一个结果幂等，迟到或并发结果不能覆盖新状态。
窗口压缩不删除 Turn、工具结果、ReadObservation 或 MaterialSnapshot。
原有空闲压缩调用和旧检查点仍可读取；新增字段有缺省值，原来的完整 Turn 来源摘要算法保留。
压缩 prompt 单独升级到版本 2，保留版本 1 的准备产物解析；会话全局 Agent prompt 仍为版本 1。

摘要请求保留当前任务、版本化材料定位、失败/拒绝/未知状态等要求，且不包括 opaque continuation。
超大历史按完整覆盖边界选择可容纳的前缀，不截断用户要求或拆开原生工具对。

## 集成层：当前窗口与阅读历史分开

`app.agent_integration.window` 在准备模型输入时投影材料，不修改原始 ToolResult：

1. 同一 material_id、相同返回摘要和区间的重复片段，只有完整副本仍在同一窗口时，
   才替换为明确的 duplicate_locator；原生 call/result 配对保留。
2. 已压缩掉的副本不算窗口内正文；之后重新读取时正文仍会进入窗口。
3. 较旧的大型阅读结果可变为带 material_id、原片段摘要和范围的显式预览。
   预览更新实际文本摘要、UTF-8/字符范围及 next_offset，原结果和完整材料仍可查。
4. 最近的工具交换保留。错误结果与命令 stdout 不按文件阅读材料去重或裁剪。
5. 文件版本不同或材料不同，不因文本碰巧相同而合并。

`inspect_window` 返回每次真实阅读在该投影中的状态：

| window_mode | 含义 |
| --- | --- |
| full_material | 本次捕获的材料正文完整保留；不等于整个源仓库或源对话都已读完 |
| fragment | 当前窗口只含阅读片段/选区 |
| preview | 原阅读正文现以显式材料预览提供 |
| locator | 同一窗口已有相同完整片段，此处保留定位信息 |
| compacted | 原阅读所在交换进入摘要覆盖区；不保证摘要保留每个原文细节 |
| not_included | 该阅读正文未进入本次窗口 |

清单关联原 turn/step/tool_call/material，提供来源版本、原片段摘要、实际可见摘要及消息位置。
源文件现在是否被终端再次修改，仍需重新读取才能确认，不由窗口清单推断。

## 预算与调用方接口

`WindowPolicy` 为服务端配置，不从模型参数或浏览器接受权限/预算覆盖。
默认配置上下文窗口为 32768 token、输出预留取本次 max_tokens、安全余量 1024，
输入软/硬上限 9000/16000；这些是本项目默认配置，不是供应商声明的模型规格。
最终有效输入上限还会减去输出与安全余量。调用方需按实际模型窗口配置。

输入估算包括系统 prompt、会话系统说明、工具 schema、消息和请求外壳。
当前使用 `utf8_bytes_div_3_heuristic_v1`，明确是估算，不是供应商精确 tokenizer。
同时检查字符硬上限（默认 60000，核心最多 64000）和消息数上限（默认 240，核心最多 256）。
单纯未达到字符上限不代表 token 一定足够；供应商超窗补救属于后续编排层。

| 函数 | 返回与责任 |
| --- | --- |
| core.project_model_input | 在分配步骤前检查待投影输入；不调用模型 |
| inspect_window | preview 清单、预算、请求摘要和原生历史是否已配对 |
| plan_compaction | manual/threshold 下选择安全 PreparedCompaction，或无可用边界时返回 None |
| core.call_compaction | 一次摘要 API 调用，不自动采用或重试 |
| adopt_window_compaction | 同时要求核心历史窗口与实际投影请求缩小，返回待 CAS 保存的新状态 |
| prepare_window_request | 超过有效硬预算时抛 ContextCapacityError；否则返回原 PreparedModelRequest 和 prepared 清单 |

规划最多检查 64 个候选边界，摘要输入默认最多 60000 字符。
历史特别长时可以只生成一个可容纳的旧前缀请求；调用方采用后重新检查预算，
总维护次数、费用、失败和网络重试由第三批 B 的持久编排负责约束。
不能缩到预算内时返回容量问题，不静默删除当前任务或工具结果。

`keep_recent_turns=1` 用于整轮压缩候选；压缩长活动 Turn 内部步骤时，
连续前缀边界会覆盖其之前的完整轮次，仍保留当前任务及近期完整工具交换。

## 只读 HTTP

新增：

```text
GET /api/v1/sessions/{session_id}/agent/conversations/{conversation_id}/context?offset=0&limit=20
```

复用考生 Cookie、`X-VibeEval-Request: 1`、Agent 开关、同一数据库与 no-store。
按 material occurrence 分页（limit 最大 100），提供 material_count/has_more。
输出 stage=preview、核心 revision、压缩代数与边界、预算和材料可见情况；
不返回工具正文、系统 prompt、供应商续调数据或 API Key，不调用模型、不修改会话。

preview 表示按当前服务端已装配阅读工具得到的窗口预览，不是“这份内容已经发给供应商”的证明。
prepared 清单也只表示请求已准备。真实请求发送/结果/窗口清单的持久关联在第三批 B 实现。
本批没有新增压缩 POST 调度接口或审批接口，也没有新数据库表或前端改动。

## 验证

新增的纯测试覆盖活动 Turn、旧格式、来源篡改、保护边界、陈旧摘要、重复片段、版本差异、
大型预览、超过 256 条消息的预检、请求预算以及实际投影变大时拒绝采用。
数据库测试覆盖检查点保存/重启、压缩后取回旧材料、只读 HTTP 和摘要后模型续调。
API 使用 httpx.MockTransport，没有真实模型费用；沙箱 I/O 使用第一批阅读测试的模拟处理器。

```powershell
# backend/
..\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_agent*.py' -q
..\.venv\Scripts\python.exe tests/manual/agent_database_probe.py --all-tests
```

后续按 [全流程计划](agent-integration-plan.md) 接入持久编排、批准/提问和实际运行窗口记录。

2026-11-07 验证结果：新增 22 项测试全部通过（18 项纯函数/模型窗口测试，4 项真实 PostgreSQL 测试）。
完整后端回归 430 项，422 项通过，8 项真实沙箱用例按配置跳过；数据库用例均已启用。
全应用覆盖率 88.6%，本小步核心压缩/窗口相关五模块合计 94.2%，新增 window 模块 95.0%。
Black、Ruff、依赖检查、语法检查、密钥扫描通过，数据库迁移升降级与模型一致性通过。
临时数据库容器已清理，开发数据库和前端未改动，没有真实模型调用。

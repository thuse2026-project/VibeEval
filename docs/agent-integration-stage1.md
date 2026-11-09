# Agent 接入第一批：归属、持久会话与事务事件

状态：2026-11-06 用户已确认，作为第一批接入检查点提交。
分支 feat/agent-integration，依赖已验收的 Agent 核心 8c25e65。
本批没有前端改动，也没有启动模型、工作区工具或自动压缩运行。

## 已实现的边界

- 新增 agent_conversations 和 agent_events，Alembic 迁移 0006。
- PostgresSessionStore 实现现有 create/load/save 协议，scope_id 映射为原 exam_session_id。
- 一个考试会话可有多条 Agent 对话；使用原考生 Cookie 和 require_candidate_session 校验归属。
- 状态、revision 和递增事件在同一事务保存；同一结果重放不重复写事件。
- save 持有短事务行锁并检查 expected_revision，拒绝并发覆盖、跳版本和会话配置变化。
- 普通 load 保留健康活动 Turn，不调用会中断运行的 restore_snapshot。
- HTTP 仅返回公开对话信息与历史，不返回系统配置、完整内部快照或 continuation_json。

会话创建时锁定原 ExamSession：考试已截止、正在交卷或已交卷时拒绝新建。
既有记录仍可查询；持久层允许记录旧运行的最终状态，后续运行入口另行检查截止门禁。
数据库 JSON 快照中的会话 ID、归属和 revision 必须与索引列一致，数据库约束也校验这些关联。

## HTTP 契约

所有路径均以 /api/v1/sessions/{session_id}/agent 为前缀；这里 session_id 是考试会话 ID。
沿用原浏览器 Cookie，并要求 X-VibeEval-Request: 1；不新增用户身份或独立登录。

| 方法和路径 | 输入/返回 |
| --- | --- |
| POST /conversations | 仅接受可选 title，返回 201 和公开对话描述 |
| GET /conversations | offset 默认 0，limit 默认 20、最多 100；返回 items、offset、has_more |
| GET /conversations/{conversation_id} | 返回公开描述、当前状态、活动 turn_id、revision 和事件位置 |
| GET /conversations/{conversation_id}/history | after_turn 默认 0、limit 默认 20、最多 100；返回当前 revision、turns、next_turn 和 has_more |
| GET /conversations/{conversation_id}/events | after_sequence 默认 0、limit 默认 100、最多 200；返回事件、next_sequence、has_more |

after_turn 是已跳过的轮次数，将 next_turn 传给下一次 after_turn 即可继续。
事件 sequence 从 1 开始，after_sequence 为不包含该序号的后续查询；创建事件对应核心 revision 0。
当前是事务事件的分页 HTTP 查询，SSE、运行中输出和批准事件在后续批次接入。
历史查询是上游读取记录的 API，不代表 Agent 已经阅读该内容；Agent 阅读观察记录在下一批单独建立。

公开描述包含 conversation_id、exam_session_id、title、revision、status、active_turn_id、
turn_count、context_generation、event_sequence、created_at、updated_at。时间沿用 Unix 秒。
没有轮次时 status 为 idle，有轮次时为最后一轮状态。
历史保留工具请求和实际结果，剔除适配器内部续调字段。查询不会恢复或重新执行旧操作。
本批 history 按 Turn 分页；后续跨对话工具和长工具结果将提供材料/消息级的版本化范围读取。

POST 不接受 messages、scope_id、system_prompt、模型响应或整份 state。
客户端不能绕过核心函数改写会话；新的用户输入和模型运行端口在后续批次提供。
404 统一表示不存在或无权访问，403 表示来源头不合法或 Agent 接口未启用，
409 表示考试关闭/版本冲突，422 表示输入不合法，503 表示持久层或记录不可用。
新路由的业务错误放在 detail.code/detail.message，数据库异常沿用原脱敏响应。

## 启用与服务注入

默认关闭新增接口。开发时需显式设置进程环境 VIBEEVAL_AGENT_ENABLED=1，
并将目标开发数据库迁移到 0006。此开关不自动读取根目录 .env，也不替代后续场次策略。
主应用工厂可传 agent_enabled=True 和服务端 agent_store；未注入时使用入场服务的共享 PostgreSQL factory。
显式 SQLite 入场测试模式不会回落为 Agent 持久库，缺少真实 Agent 存储时返回 503。

本批基础接口只管理记录，启用它不等于模型或命令工具已经接通。

## 本批固定的上下文数据责任

以下为后续阅读/上下文模块的契约，不是本批已经运行的接口：

| 对象 | 需要固定的字段与关联 |
| --- | --- |
| InputReference | reference_id、考试/对话/轮次、来源类型与 ID、用户选择范围、期望版本、可选显式文本材料；不代表发生阅读 |
| MaterialSnapshot | material_id、来源版本、来源内容摘要、实际保存区间、材料内容摘要、不可变正文/存储指针、完整性 |
| ReadObservation | observation_id、考试/对话/turn/step/tool_call、请求范围、实际返回范围、material_id、读取时间；记录真实阅读 |
| ContextManifest | 模型调用 ID、会话 revision、上下文代数、材料的全文/片段/摘要/定位/未包含状态、预算及估计方式 |
| ContextCheckpoint | 检查点/父检查点 ID、覆盖的 turn/step 边界与来源摘要、当前任务保护项、摘要、材料关系、触发来源和用量 |

用户引用和工具阅读分别存关联，材料可以复用；不得用引用表替代真实阅读日志。
不同文件版本分别记录，目录/搜索不标记为全文已读；跨会话阅读固定源 revision。
窗口清单只描述实际发送给模型的内容；原始历史保留在核心快照和关联记录中。
主动压缩、阈值自动压缩和明确超窗后的有界补救，共用检查点与用量契约。
覆盖活动 Turn 内完成交换的压缩边界尚需扩展核心；本批不把旧的空闲压缩接口当成该能力已经完成。

详细目标见 [上下文管理设计](agent-context-management-plan.md) 和 [全流程计划](agent-integration-plan.md)。

## 可重复验证

从 backend 运行：

```powershell
$env:PYTHONUTF8 = '1'
..\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_agent_integration*.py' -v
..\.venv\Scripts\python.exe tests/manual/agent_database_probe.py
..\.venv\Scripts\python.exe tests/manual/agent_database_probe.py --all-tests
```

第一条默认只运行不需要数据库的检查，数据库用例必须显式启用。
probe 使用本地已有 postgres:16-alpine 镜像，新建仅绑定回环随机端口的临时容器，
生成临时密码，并在该实例内创建独立测试数据库；结束后清理自己创建的容器和匿名卷。
不改根目录 .env，不迁移当前开发数据库，不请求真实模型，也不执行 gVisor 候选人命令。

2026-11-06，新增 15 项测试全部通过，其中 10 项实际使用独立 PostgreSQL，
覆盖新实例读取、多对话、归属隔离、并发 CAS、事务回滚、事件分页、内部字段过滤和 HTTP 门禁。
完整后端回归 387 项，379 项通过，8 项真实沙箱用例按配置跳过；数据库用例均已启用。
迁移升降级及模型/数据库结构一致性检查通过。
新 Agent 接入模块行覆盖率 95.5%，完整 app 行覆盖率 87.2%。
Black、Ruff、Python 编译、差异空白和密钥扫描通过。

两次验证使用的独立 PostgreSQL 容器及匿名卷均已清理；原开发 PostgreSQL/Redis 保持运行。
本批未修改前端，未调用真实模型或实际执行 gVisor 命令；经用户确认提交到本地分支，未推送、未触发远程 CI。

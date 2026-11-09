# VibeEval 考试流程后端封装交接

本分支交付考试各个基本流程的后端支持。题目由出题同学提供，前端由前端同学实现；调用方注入冻结考试配置、题目提供器及工作区和 Agent 适配器，即可使用准备、开始、环节切换、阶段提交、恢复和整场结束的统一接口。

开发分支为 `feat/multi-stage-exam`，基线包含最新一期主线 `341d070` 和 Agent 后端 `241c05e`。本次不编写正式题目、题库、前端页面或评分系统，评分方通过提交事件和服务端答卷读取接口接入。

## 已交付的流程

| 流程 | 后端行为 |
| --- | --- |
| 关联考试 | 可信服务端调用 `attach`，关联已入场会话，固定配置版本，拒绝替换已有配置 |
| 环境准备 | 调用资源适配器准备当前阶段；成功前不开计时、不开放材料 |
| 正式开始 | 适配器启用当前环节后，按服务端时间设置独立截止时间 |
| 探索转答题 | 先撤销旧授权并关闭 Agent，固定公开题目后启用新环节；同阶段保留笔记，按能力决定是否允许继续编辑 |
| 作答保存 | 保存任意 JSON 答案，由题目提供器校验；草稿、笔记和提交说明分别使用版本号避免覆盖 |
| 阶段提交 | 先关闭操作，再封存文本或获取固定代码产物；每阶段仅一份有效答卷和回执 |
| 阶段过渡 | 当前答卷安全固定后准备下一阶段；准备成功后启动过渡计时，支持提前开始 |
| 到期推进 | `run_due` 供后台调度；请求也补查截止，离线不重置时间 |
| 失败恢复 | 持久化当前外部操作标识和重试时间；关闭或封存失败保持撤权，不开放下一阶段 |
| 整场结束 | 所有阶段提交后标记 `completed`，与评分及成绩发布解耦 |
| 下游交接 | 持久事件提供游标；可信服务端通过 `export_submission` 读取固定答卷及代码产物引用 |

四阶段和第一阶段双环节由配置表达，不在代码中写死题目数量、缺陷数量、仓库名称、考试时长或评分分值。配置支持有界的多阶段、多环节顺序流程；原方案中的 20＋15、20、50、40 分钟和 60 秒过渡可由出题或考务配置方填入。

## 公共入口

- [ExamFlowService](../backend/app/exam_flow/service.py)：业务协调器与所有可信服务端操作。
- [类型和扩展协议](../backend/app/exam_flow/types.py)：考试配置、题目、阶段权限、资源适配器与代码产物。
- [PostgresFlowStore](../backend/app/exam_flow/store.py)：共享 PostgreSQL 持久化，复用入场行锁串行处理同一考试。
- [HTTP 路由](../backend/app/exam_flow/router.py)：供前端同学接入的薄层 API。
- [API 响应类型](../backend/app/exam_flow/api_types.py)：当前状态、阶段进度、事件、公开题目和错误的响应契约。
- [装配示例](../backend/examples/exam_flow.py)：装配应用、读取外部考试配置、后台定期推进。
- [阶段资源适配](stage-resources-handoff.md)：已实现的阶段 Agent、文件、终端、独立沙箱和实际撤权机制。

`ExamDefinition` 包含考试标识、版本、名称、过渡秒数及阶段列表。每个 `StageDefinition` 提供固定题目版本、资源引用、文字或代码提交类型及环节列表；每个 `PhaseDefinition` 提供名称、时长、能力集合和可选的题目提供器引用。

## 题目方接入

实现 `QuestionProvider` 的两个方法：

1. `questions(context, reference)` 返回当前环节的公开题目列表。每项为 `PublicQuestion(id, title, body, answer_schema)`。
2. `validate_answer(context, question, value)` 根据传入的固定题目校验答案结构和限额，非法答案抛出 `ValueError`。校验时不要重新查找动态题目并替换传入的题面或规则。

题目数量、题型、文本限额、缺陷报告字段均由提供器定义。答案支持 JSON 字符串、对象或数组，封装层限制单次答案的紧凑 UTF-8 JSON 为 64 KiB；文字计数与业务结构由提供器按自己的公开契约校验。非法 Unicode、NUL 和非有限数值在持久化前返回明确错误，不让它们变成数据库或响应序列化故障。私有评分要点、隐藏测试、参考答案不进入此提供器。

环节正式开放前，协调器会取得并持久化公开题目副本。后续查询、答案校验和提交使用这份副本，提供器后来的变更不会替换考生正在作答的题面；固定题目也随答卷封存，纳入内容摘要。提供器失败时环节保持未开放，不启动作答时钟。升级前已经开始、尚无题目副本的环节在首次读取或保存时补固定一次，截止时间保持不变；已提交的旧答卷保持原正文与摘要。

题目获取可能发生在正式开表前，此时 `context.deadline_at` 为 `null`。提供器应通过服务端的冻结材料来源读取题目，不依赖候选人已经获得活动环节权限；答案校验则在当前环节有效期间执行。

`question_ref` 只配置在允许开放题目的环节。探索环节可以没有题目引用，答题环节使用独立引用。未来环节、已结束环节不能通过候选人接口读取题目或草稿。

## 工作区和 Agent 方接入

已提供具体实现 `StageResources` 和 `StageSandboxRuntime`，通过 `build_stage_app` 装配。自定义后端仍可按 `ResourceGateway` 的四个方法接入：

| 方法 | 适配器职责 |
| --- | --- |
| `prepare(context, stage_definition, operation_id)` | 根据资源引用建立独立阶段工作区，准备只读材料或沙箱；不提前发布材料 |
| `activate_phase(context, phase_definition, operation_id)` | 按能力配置当前环节，关联 Agent 和工作区；实际访问仍须等待协调器发布有效授权 |
| `close_phase(context, operation_id)` | 撤销该环节旧授权，取消 Agent 调用，阻止新操作并排空已接受写入 |
| `seal(context, stage_definition, operation_id)` | 固定所有写入；文字阶段返回 `None`，代码阶段返回 `Artifact(workspace_id, reference, sha256)` |

`context` 明确包含入场会话、考试、阶段、环节、工作区、题目版本、权限版本和截止时间。每个阶段分配不同的 `workspace_id`，同一阶段内部环节共用工作区。适配器应按这些标识持久化资源和 Agent 绑定，不能沿用整场会话的跨对话可见性。

每个文件、Agent 工具、历史、材料、上下文和终端操作，都应调用 `flow.authorize(session_id, phase_id, capability, permission_epoch=...)` 校验当前权限。关闭环节会先持久化递增权限版本，再调用适配器撤权。适配器还必须处理校验后与实际执行之间的竞争，并主动停止或冻结已开始的操作。

四个适配方法必须有明确超时，并对同一 `operation_id` 幂等，包括服务重启后的重试。多个工作进程可能调用同一操作；不能重复创建沙箱、重复启用 Agent 或让迟到的旧关闭请求影响新环节。协调器只会提交仍对应当前操作的返回结果。

代码产物必须属于当前 `workspace_id`，并在所有进程和写入者停止改变内容后返回。关闭 WebSocket 不代表冻结沙箱。冻结失败时适配器保持写入屏障，协调器保留 `sealing` 和同一操作标识重试；成功回执不依赖后续容器清理。

`StageResources` 已复用 Agent 原子能力和沙箱操作，增加独立阶段工作区、文件版本和环节 Agent scope。v2 文件、PTY 和 Agent 路由执行当前环节权限，旧 v1 沙箱不会被伪装成阶段工作区。注册可信镜像和清单后，可按 [阶段资源交接](stage-resources-handoff.md) 使用具体适配器；真实 runsc 联合验收按用户要求暂缓。

## HTTP 接口

现有考生 Cookie 的 Path 仅覆盖 `/api/v1/sessions`。同一浏览器先调用 `POST /api/v1/sessions/{session_id}/exam-access`，后端核验原入场身份并设置独立命名、Path 为 `/api/v2` 的 HttpOnly Cookie。前端无需读取身份秘密。所有接口继续要求 `X-VibeEval-Request: 1`，私有响应禁止缓存。

| 接口 | 用途 |
| --- | --- |
| `GET /api/v2/attempts/current` | 恢复当前状态、服务端时间、阶段进度、能力和版本 |
| `POST /api/v2/attempts/{id}/prepare` | 准备第一阶段环境 |
| `POST /api/v2/attempts/{id}/start` | 开始第一阶段 |
| `GET /api/v2/stages/{id}` | 当前阶段完整状态或其他阶段的进度元数据 |
| `POST /api/v2/stages/{id}/start` | 过渡期提前开始当前阶段 |
| `POST /api/v2/phases/{id}/finish` | 结束当前环节，推进下一环节或封存本阶段 |
| `GET /api/v2/phases/{id}/questions` | 当前环节公开题目 |
| `GET /api/v2/phases/{id}/answers` | 恢复当前环节草稿 |
| `PUT /api/v2/phases/{id}/answers/{item_id}` | 按版本保存答案 |
| `GET /api/v2/phases/{id}/notes` 与 `PUT .../notes` | 按当前能力读取或修改同阶段笔记 |
| `GET /api/v2/stages/{id}/summary` 与 `PUT .../summary` | 恢复或保存当前阶段的实现或修复说明，用于到期提交 |
| `POST /api/v2/stages/{id}/submit` | 主动提交最后一个环节所在阶段 |
| `GET /api/v2/stages/{id}/submission` | 查询不包含答卷正文的提交回执 |
| `GET /api/v2/attempts/{id}/events?after=0&limit=100` | 按持久游标读取状态和提交事件 |

状态命令携带 `expected_state_version` 和 `request_key`；保存携带 `expected_revision` 和 `value`，笔记或说明使用 `text`。提交还可附带 `summary`。同一请求键改变操作或正文返回 `IDEMPOTENCY_CONFLICT`；已提交阶段的重试返回原回执。

`server_now` 和所有 `*_at` 时间戳使用 UTC Unix 秒，`capabilities` 为能力名称数组。草稿更新也会推进整场状态版本；主动提交前先等待保存完成，再获取最新状态版本。版本冲突后重新查询状态，不覆盖更新的草稿。

考试流程 v2 成功响应均有明确的 Pydantic 模型，可从应用的 `/openapi.json` 获取结构生成接入类型。`GET /stages/{id}` 对当前阶段返回 `AttemptView`，对其他阶段仅返回 `StageProgress`；响应不会包含内部题目副本、草稿、资源引用或完整流程记录。实现说明只允许在当前阶段的最终环节读取，阶段关闭后不再开放正文。

常用错误码包括 `STATE_VERSION_CONFLICT`、`REVISION_CONFLICT`、`PHASE_CLOSED`、`CAPABILITY_DENIED`、`SUBMISSION_IN_PROGRESS`、`EXAM_MODE_MISMATCH`。外部资源错误通过状态中的 `error_code`、`failure_count` 和 `retry_at` 反映，不暴露适配器异常原文；没有待重试错误时 `retry_at` 为 `null`。

## 持久化和服务端装配

执行 Alembic `upgrade head` 到 `0012`。`0011` 的 `exam_flow_attempts` 将冻结配置、各阶段环节、草稿、回执、事件和待重试操作作为有版本的 JSONB 聚合保存，整份记录限制 32 MiB。同一入场会话只有一条记录，在入场行锁和聚合行锁下串行更新；多份阶段答卷保存在该聚合各自的阶段中，不向原单阶段提交表重复插入。`0012` 增加阶段工作区、文件版本和 Agent scope 三张表，迁移已有单阶段 Agent 归属，详见 [阶段资源交接](stage-resources-handoff.md)。

`exam_sessions.exam_mode` 默认 `single`。可信方只能把尚未进入旧考试的会话关联到多阶段模式，关联与模式更新同事务提交。旧 HTTP 路径和旧 `SessionLifecycle` 都拒绝操作多阶段会话，避免旧请求或不同应用装配绕过新流程。旧单阶段记录及其提交唯一约束保留。

服务端装配示例：

```python
from examples.exam_flow import attach_definition, build_app, maintain

# my_questions 和 my_resources 由对应模块实现协议。
app = build_app(my_questions, my_resources, factory=shared_factory)
flow = app.state.exam_flow

# 可信考务接入在入场成功后关联该考生的冻结考试配置。
attach_definition(flow, admitted_session_id, "exam-definition.json")

# 在宿主的后台维护进程中调用，stop_event 由宿主管理。
maintain(flow, stop_event)
```

也可将 `run_due()` 接入已有调度器。必须持续运行后台维护，不能只依赖浏览器请求推进；启动服务本身不会私自创建后台线程。适配器错误按持久重试时间继续处理，进程级故障由宿主监督恢复。

下一阶段已经准备好、过渡期也已结束时，调度延迟不会重新发放完整作答时间：自动开始沿用已持久化的过渡截止时间，已经到期的环节立即执行关闭与提交。下一阶段的过渡仍需等待其环境实际准备好。题目获取或资源激活出现明确失败时，作答时钟保持停止，恢复成功后才开始；探索结束后需要撤权再切换的答题环节也以实际切换成功为起点。

评分方读取 `stage.submitted` 事件并保存自己的游标，再以可信服务端身份调用 `export_submission(session_id, stage_id)`。返回固定文字、笔记、说明、代码产物引用及内容摘要；评分不修改原答卷，也不阻塞下一阶段。这里不实现评分规则、评分执行、考官页面或成绩发布。

迁移回退在仍有多阶段考试记录时会明确拒绝，以免静默丢失考试与答卷；需先由维护方完成归档和清理。

## 验证边界

专项测试使用外部题目及资源协议的显式测试替身验证四阶段流转，真实 PostgreSQL 验证迁移、并发保存、并发提交、失败重试和恢复。HTTP 测试经过真实入场与 Cookie 路径交换，覆盖所有权、来源头、旧路径绕过、草稿冲突、提交和事件查询。

本分支的资源协议测试不等于真实模型和 gVisor 的阶段联合验收。具体适配器已实现，真实环境仍需验证模型请求取消、沙箱进程停止、同路径跨阶段隔离及资源清理；本轮按用户要求暂缓服务器验收。前端同学可直接按上述状态、能力、版本和回执契约接入，无需本分支实现页面。

最新完整后端回归为 619 项：604 项通过，15 项真实沙箱用例按环境跳过，整体覆盖率为 87.3%。其中阶段资源专项 18 项全部通过，包含 SSE 逐条撤权和非样例清单的原生 Agent 读取检查。Black、Ruff 通过，回归包含真实 PostgreSQL 迁移一致性、降级升级、并发与恢复验证。测试使用独立 PostgreSQL 容器，未改动正式数据库或前端。

本轮完成具体阶段资源适配器、v2 文件与终端、环节级 Agent 接口，以及关闭环节后的模型请求取消、进程停止和冻结导出。部署这部分实现需要执行 `0012` 数据库迁移；可信题目与资源注册、宿主维护调度和 Linux/runsc 环境仍由服务端装配提供。

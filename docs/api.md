# Release 1.0 接口草案

本页从原始需求文档附录 E.3 提取，保留原始示例供组内讨论，不表示接口已实现或已确认。

一期范围与分工见 [原始需求文档](requirements/VibeEval-AICoding%20平台.md) 附录 E。确认后的联调契约统一更新本页，原始文档保留讨论背景。

## 待确认项

- 考生 WebSocket 鉴权和考试状态流转（HTTP 入场及恢复已约定如下）。
- 工作区文件、PTY 与正式工作台的整体联调；沙箱提交适配器已接入，联合验收记录见下文。
- 会话状态、开始计时与截止后的处理方式。
- 公开测试的执行入口，以及结果如何保存并关联提交。
- 生产截止冻结精度与定期处理部署（到期扫描、恢复和清理命令已实现，见提交定期处理说明）。
- 文件版本冲突、越界路径、只读文件和冻结后操作的错误约定。
- 终端退出、断开及错误消息的约定。

## 模型 API（新增）

模型 API 新增独立考官联调接口：`GET /api/v1/admin/llm/config`、
`GET /api/v1/admin/llm/models`、`POST /api/v1/admin/llm/chat`。
沿用下述考官 Cookie 和 POST 来源头；请求、响应、限额及错误码见
[模型 API 契约](llm-api.md#http-契约)。内部原子函数与后续批次见 [Agent 原子函数](agent-primitives.md)。
本批尚未接入候选人工作台。

Agent 接入分支另增考生归属下的会话创建、列表、历史和事务事件查询，默认关闭；
契约、开关及数据库迁移见 [Agent 接入第一批](agent-integration-stage1.md)。
这些记录接口不直接执行模型或沙箱命令。

第二批另增同一对话下的 `POST/GET /references`、`GET /references/{reference_id}`、
`GET /materials/{material_id}`、`GET /reads`，用于引用输入、材料预览和实际阅读查询。
内部阅读服务复用上一分支原生 tool call/result；没有开放浏览器伪造工具回执的接口。
版本、选区、工具和后续边界见 [Agent 接入第二批](agent-integration-stage2.md)。

第三批 A 新增同一对话下的 `GET /context?offset=0&limit=20`，只读返回当前窗口预览、
预算估算、压缩边界与材料可见情况，不返回正文、系统 prompt 或 Key，不触发模型。
stage=preview 不表示已经向供应商发送；契约与运行编排边界见 [第三批 A](agent-integration-stage3a.md)。

第三批 B 接通 `POST /turns`、`POST /compactions` 及 `/runs/{run_id}` 下的 advance/interrupt/compact、
attempts/interactions 和批准/答复接口。提交返回 queued Run，调用 advance 在后端运行只读循环；
等待交互时答复后再 advance。请求、预算、错误和恢复语义见 [持久只读运行契约](agent-integration-stage3b.md)。
浏览器不提交模型/工具成功回执；本批尚不提供命令或文件修改。

第四批 A 增加原生 `run_command/read_execution_output`、`waiting_operation` 和
`GET /operations`、`GET /operations/{id}`、`POST /operations/{id}/cancel`、
`GET /operations/{id}/output`。批准继续使用已有 interactions 接口；输出增量通过现有事件查询。
默认服务端仍为 read_only；confirm_actions/sandbox_auto 必须由服务端启用并允许客户端收窄。
版本绑定、执行回执、输出偏移及不重发规则见 [沙箱命令契约](agent-integration-stage4a.md)。
结构化文件修改/补丁见第四批 B；本分支不改前端。

第四批 B 新增原生 `write_file/apply_patch`，绑定基础版本、修改前后材料、审批 diff 与实际回执，
操作详情增加 kind/change/file_result。另有 `GET /operations/{id}/change?part=before|after|diff` 分页预览。
复用现有批准、advance 和取消；仅开放任务的两个可写文件，按单文件原子替换。
字段与暂停/恢复、交卷门禁及效果未知语义见 [版本化修改契约](agent-integration-stage4b.md)。

第五批 A 增加 `GET /events/stream`，支持 after_sequence、follow 与绑定对话的 Last-Event-ID 续读。
使用现有已提交事件，不驱动模型或沙箱；保留 Cookie 和来源头，提供心跳、预算和安全结束帧。
游标、帧格式、错误与客户端示例见 [事件订阅契约](agent-integration-stage5a.md)。
模型文字和工具参数的供应商增量流由后续第五批 B 接入。

## 考官登录（本轮实现）

- 账号由本地初始化命令预配置，无注册接口。
- `POST /api/v1/admin/login`：JSON `{ "username": "examiner", "password": "..." }`，成功返回 `{ "username": "examiner", "name": "陈智恒", "expires_at": 1793336400, "server_time": 1793325600 }`（时间为 Unix 秒，仅作示例）。同时写入 `vibeeval_admin` HttpOnly、SameSite=Strict Cookie，路径 `/api/v1/admin`，有效期固定 10800 秒。
- `GET /api/v1/admin/me`：验证 Cookie，返回上述身份及原始到期时间；不会延长有效期。后续考官接口复用后端 `current_session` 鉴权依赖。
- `POST /api/v1/admin/logout`：撤销当前会话并清除 Cookie，返回 204；重复退出同样返回 204。
- 两个 POST 需带 `X-VibeEval-Request: 1`；前端同源调用，不开放跨域 CORS。正式部署使用 HTTPS 并启用 Secure Cookie。
- 错误：401（凭证错误或未登录/过期）、403（缺少请求来源校验头）、422（请求字段不合法）、429（同一客户端地址 60 秒内已失败 5 次）、503（未配置账号）；错误正文为 FastAPI 的 `detail` 字段。客户端展示友好提示，不区分未知账号与密码错误。
- 当前考官会话仅存在单进程内存，重启后失效。此约定取代下方原始草案中的管理端 Bearer JWT 设想；总览和答卷详情均复用此鉴权，真实答卷由共享 `SubmissionService` 提供。

## 考务总览（本轮实现）

`GET /api/v1/admin/overview` 使用考官登录 Cookie 鉴权；未登录/过期返回 401，候选人 Cookie 不具备访问权限。当前所有预配置考官均可查看全部场次，无场次分配权限模型。

| 查询参数 | 默认值 | 含义 |
| --- | --- | --- |
| `source` | `live` | `live` 真实记录或 `demo` 演示数据，两者不混合 |
| `exam_id` | 不传 | 按场次精确筛选，不传表示全部场次 |
| `status` | `all` | `all`、`invited`、`admitted`、`in_progress`、`submitted`、`invitation_expired`、`recovery_expired` |
| `page` / `page_size` | `1` / `10` | 页码至少 1，单页 1～100 条；超出末页返回末页，无数据返回第 1 页 |

响应：`source`、`items`、筛选后记录数 `total`、实际 `page` / `page_size`、全部场次选项 `exams`、统计 `summary`、读取时间 `updated_at`，及真实提交服务接入标志 `submission_service_connected`（默认真实来源为 true，演示来源和显式 SQLite 回归模式为 false）。非法查询参数返回 422，所有响应禁止缓存。

每条记录字段：`candidate_id`、`candidate_name`、`exam_id`、`exam_title`、`status`、`session_id`（未入场为 null）、`admitted_at`、`started_at`、`submission_id`、`submitted_at`。时间为 Unix 秒，尚未发生的时间为 null；不返回入场或恢复凭证及哈希。

`summary` 包含 `total`、`not_submitted`、`submitted`，计算所选场次的全部状态和全部页，不受状态筛选影响；按 `(exam_id, candidate_id)` 计人次。列表按最近提交/入场时间倒序，未入场放最后，同时间以场次和考生编号稳定排序。前端改变来源、场次或状态时重置页码；改变来源还重置其他筛选。

真实记录当前只读取已预配置凭证和入场数据：`invited` 尚未入场，`admitted` 已入场未开始；未入场凭证过期为 `invitation_expired`，入场恢复期结束为 `recovery_expired`。入场不等于开始或交卷，未提交记录的 `submission_id`、`submitted_at` 必须为 null。

**统一接入点**：`backend/app/admin/submissions.py` 的 `get_submission_reader`（具体见下节）同时供列表与详情使用。总览按 `(exam_id, candidate_id)` 将已提交记录与入场记录合并，真实提交优先；同一考生同一场次不得重复，重复提交编号也会拒绝并返回 502。允许展示仅存在于提交库的历史记录。`submission_service_connected` 根据是否注入 reader 自动生成，不需要手动修改布尔值；入场不推断开始或交卷。

`demo` 为 12 条虚构记录，两个场次覆盖四种进度状态；界面统一标记“演示数据”。演示身份和时间固定，列表与详情保持一致，不写入真实存储。未交卷记录没有“查看答卷”入口。

## 答卷详情与提交服务只读契约（已实现）

`GET /api/v1/admin/submissions/{submission_id}?source=live|demo`，默认 `live`。使用考官 Cookie，未登录/过期 401。不会回退到另一数据来源：真实 reader 未接入返回 503，已接入但无此提交返回 404；演示来源中找不到记录也返回 404。非法 ID/来源返回 422；接口仅支持 GET，禁止缓存。

响应保留附录 E 的 `submission_id`、`submitted_at`、`public_test_output` 与 `files[{path, content}]`，补充定位与身份信息：`source`、`session_id`、`snapshot_id`、`candidate_id`、`candidate_name`、`exam_id`、`exam_title`、`admitted_at`、`started_at`。

所有时间为 Unix 秒。未提供的入场/开始时间为 null；公开测试输出 null 表示尚未提供，空字符串表示已提供但输出为空，两者都不等于通过。界面按原始纯文本展示代码及输出，不解释 HTML，不自动推断分数。

**持久化成员只需提供一个 reader**（不在管理端实现数据库、交卷或快照写入）：

```python
class SubmissionReader:
    def list_submissions(self):
        # 返回已持久化提交的摘要列表
        ...

    def get_submission(self, submission_id):
        # 返回固定快照详情，查无记录返回 None
        ...
```

摘要字段与 `SubmissionSummary` 模型一致：提交编号、会话编号、考生编号/姓名、场次编号/标题、提交时间，以及可为 null 的入场/开始时间。详情在摘要上增加 `snapshot_id`、`files`、`public_test_output`，与 `SubmissionDetail` 模型一致。方法可返回模型或同字段字典；没有公开测试结果时显式返回 null。返回值不包含密钥、内部磁盘路径或可变工作区引用。

在服务启动时，将 `get_submission_reader` 通过 FastAPI 依赖注入接到持久化成员的实现；也可在此函数内接现有服务。例如已有 `submission_reader` 实例时：

```python
from app.admin.submissions import get_submission_reader

app.dependency_overrides[get_submission_reader] = lambda: submission_reader
```

默认应用已注入队友的 `SubmissionService`，列表和详情读取同库提交与持久化快照；显式 SQLite 回归模式仍不接 reader。演示来源不会查询真实存储。服务异常返回不含内部异常信息的 503；字段缺失、重复文件路径、错误提交身份或非法路径返回 502。

提交编号为 1～128 位字母、数字、下划线或连字符；其他身份文本长度沿用现有考务字段限制。快照 `path` 为唯一的相对 POSIX 路径，不允许绝对路径、盘符、空路径段或 `..`。`content` 是 UTF-8 解码后的文本，空文本和空文件列表均允许，管理端不根据路径去打开服务器文件。

前端详情地址为 `/admin/submissions/{id}?source=...&return_to=...`。来源显式保留；返回目标仅允许同源 `/admin` 的已知筛选参数，防止外部跳转。未登录直接打开详情，登录后继续该详情；返回按钮、刷新、浏览器前进/后退保留列表筛选和页码。

演示快照是单独保存的固定 JSON 文件，内容来自原始会议室样题；每次请求不重读候选人目录、当前工作区或维护者参考答案。演示测试输出明确标为虚构，不代表真实考试执行结果。真正快照冻结、测试执行和持久化归赵晨一/陈弘毅的模块，本次未新增这些写入逻辑。

## 考生入场与恢复（已实现）

本分支默认使用共享 PostgreSQL 的 Invitation/ExamSession，CLI 与 HTTP 配置一致，不双写 SQLite。current/status 返回共享生命周期真实状态、真实 started_at/deadline_at，并增加可为 null 的 task_version_id。初次入场仍为 admitted/null；环境准备和开始操作由沙箱接入方调用共享生命周期。状态集合见 sandbox-integration.md，考官总览也接受相同状态筛选。数据库故障返回 503，不回落本地库。启动、旧 Cookie 与回归验证边界见 [入场交接](entry-postgres-handoff.md)。

以下规则更新原始 E.3 的入场与状态草案，题目、沙箱及开始计时不在本轮范围。

1. 首先请求 `GET /api/v1/sessions/current`，携带 `X-VibeEval-Request: 1` 并允许同源 Cookie。未入场返回 401；没有有效浏览器凭证时，同时初始化 `vibeeval_candidate` HttpOnly、SameSite=Strict Cookie，路径 `/api/v1/sessions`，最长 30 天。已有 Cookie 不轮换、不续期。来源校验失败返回 403 且不设置 Cookie。
2. `POST /api/v1/sessions/verify-token`：JSON `{ "token": "tok_..." }`，同样携带校验头和上述 Cookie。首次兑换与消费在事务内完成；必须先初始化浏览器 Cookie，以支持兑换响应丢失后的安全重试。
3. 成功返回 200：`{ "session_id": "ses_...", "status": "admitted", "candidate": { "id": "student-001", "name": "张同学" }, "exam_info": { "id": "exam-001", "title": "平台入场联调", "duration_minutes": 60 }, "admitted_at": 1793325600, "started_at": null, "deadline_at": null }`。会话编号仅用于定位，不能充当凭证；不得据 `admitted` 宣称沙箱已就绪。
4. 同一浏览器后续请求 `GET /current` 返回同一会话；再次兑换同一凭证也返回原会话，包括邀请凭证已过期的情况。首次入场期限只约束首次兑换，不参与原会话恢复判定。
5. `GET /api/v1/sessions/{session_id}/status` 需携带恢复 Cookie 和校验头，只能读取自己的会话；未授权或编号不符统一返回 404。后续文件接口可复用 `require_candidate_session` 依赖。候选人恢复 Cookie 与考官登录 Cookie 独立。
6. 401：无效凭证/尚未入场；403：缺少请求来源头；409：已被其他浏览器使用，或当前浏览器试图切换会话；410：未使用凭证已过期；422：字段不合法；428：未初始化浏览器 Cookie；429：客户端地址 60 秒内出现 10 次无效凭证尝试。错误正文使用 `detail`，并禁止缓存。

恢复 Cookie 丢失或 30 天窗口结束后需要联系考官，已消费凭证不会重新启用；服务端也会检查入场后 30 天的恢复上限（到期返回 410），本轮没有重开/换设备接口。前端不要将入场凭证放在 URL、localStorage 或日志中。多标签页初始化与兑换使用同源 Web Locks 串行处理；持久化入场记录及两类凭证的哈希，后端重启不会重置。

### 本轮开源参考

- [Assessment Platform 的入场页面](https://github.com/PrahladWebDev/assessment-platform/blob/main/assessment-frontend/src/views/TokenEntry.vue)：参考凭证输入与考生免注册入场流程。
- [候选人鉴权](https://github.com/PrahladWebDev/assessment-platform/blob/main/assessment-backend/src/middleware/candidateAuth.js)及[候选人模型](https://github.com/PrahladWebDev/assessment-platform/blob/main/assessment-backend/src/models/Candidate.js)：参考将候选人、考核和作答状态关联；本项目另外实现原子首次消费和独立浏览器恢复凭证。
- [PrairieLearn 工作区说明](https://docs.prairielearn.com/workspaces/)：参考环境准备与正式作答的区分，本轮保留未开始计时状态。

仅参考相关环节与职责划分，代码独立实现，未引入其技术栈、监考、评分或完整题库。

## 主动交卷 HTTP（已实现）

`POST /api/v1/sessions/{session_id}/submit` 在 `backend/app/submissions/router.py` 实现，复用入场模块的 `require_candidate_session`。请求必须来自原浏览器，携带 `vibeeval_candidate` Cookie、`X-VibeEval-Request: 1` 和 `Idempotency-Key`。幂等键为 1～128 位字母、数字、下划线或连字符，可用 UUID；不放入 URL。不需要 JSON 请求体，前端传入的文件、身份、容器 ID 或测试结果不会作为答卷来源。

接口先检查浏览器与会话归属，再从共享数据库读取已提交回执。没有回执时，由协调器设置 `submitting`、调用服务端沙箱适配器冻结导出并保存最终答卷。成功响应沿用附录 E 的 **202**，但本实现同步等待快照和数据库事务提交；`status: "frozen"` 表示答卷已经保存，不表示仅加入后台队列。所有响应带 `Cache-Control: no-store`。

```json
{
  "submission_id": "sub_99a8b7c6d5e4",
  "session_id": "ses_01j7c390a8e1b",
  "snapshot_id": "snp_33214abc",
  "submitted_at": 1730004800,
  "status": "frozen"
}
```

同一会话更换幂等键也只返回首份最终答卷。网络断开或响应丢失时，可用原浏览器再次 POST 恢复回执；不会重新导出或覆盖文件。后端重启后即使尚未配置沙箱适配器，已有的固定回执仍可恢复。`GET /sessions/{session_id}/status` 继续返回共享会话状态、开始及截止时间；回执仍通过 POST 获取。`frozen` 是回执状态，数据库最终状态为 `submitted`。

| HTTP 状态 | 条件与代码 | 客户端处理 |
| --- | --- | --- |
| 403 | 缺少/错误的 `X-VibeEval-Request` | 检查同源请求配置 |
| 404 | 浏览器不属于该会话或会话不存在 | 回原浏览器恢复；不靠 session_id 获取权限 |
| 409 | `SESSION_BUSY`，正在保存、交卷或清理 | 响应带 `Retry-After: 1`，按提示延迟并有界重试 |
| 409 | `SUBMISSION_NOT_READY`，如尚未开考 | 查询当前状态，不强行重新建环境或开始计时 |
| 410 | 入场恢复窗口已结束 | 联系考官；原凭证不重新启用 |
| 422 | 缺少或非法 `Idempotency-Key` | 修正请求头；不执行冻结或状态变更 |
| 503 | `SUBMISSION_UNAVAILABLE`，未配置完整适配器，或显式 SQLite 回归模式 | 当前会话状态不变；等待服务接入后重试 |
| 503 | `SANDBOX_FREEZE_EXPORT_FAILED`、`SANDBOX_EXPORT_MISMATCH`、`SNAPSHOT_PERSIST_FAILED` | 保留原会话，查询状态并重试/联系维护者；不能把失败视作成功或恢复编辑 |
| 503 | 共享数据库不可用 | 稍后查询或重试，不能推断是否已提交 |

交卷领域错误使用 `{"detail": {"code": "...", "message": "..."}}`；入场鉴权、参数校验和通用数据库异常沿用各自原有 `detail` 格式，客户端需兼容字符串、校验列表和对象。返回值及日志不暴露 SDK 异常、数据库凭据或磁盘路径。

成功入库后会尝试清理容器。清理失败仍返回同一份 202 回执，清理待办继续保存在共享台账，不要求考生重复提交。冻结或持久化失败时保持写入门禁，尽力记录 `submission_failed`；数据库暂时不可用时也可能仍为 `submitting`，后续重试使用同一容器和原截止时间。

实际沙箱在应用构建时注入 `create_app(submission_sandbox=adapter)`，已提供成员 C 的显式入口 `app.sandbox.http_app:create_app`，使用带超时的 `SandboxRuntime`。默认 `app.main:app` 未配置真实适配器，对没有最终答卷的交卷请求明确返回 503，不会调用空冻结或生成演示答卷。普通 CI 用 Docker 替身，真实 runsc 联合测试需显式开启；环境和实际验证记录见 [联合验收说明](sandbox-submission-validation.md)。候选人工作台和文件/PTY 接口仍待后续接入，定期处理命令须由部署方显式启动。

## 提交持久化内部契约

共享会话、容器映射、跨进程行锁、状态集合及提交失败后的恢复规则见 [沙箱接入约定](sandbox-integration.md)，当前迁移为 `0005`，新增工作区文件版本表。`SessionView.task_version_id` 与 `sandbox_image_ref` 分别提供题目发布版本和镜像引用；提交台账会冻结保存两者，现有管理端 reader 字段保持不变。已有 HTTP 草案仍待对齐这些内部状态和鉴权方式。

以下为当前分支已实现的 Python 内部服务，HTTP 交卷路由见上一节，真实适配器通过显式工厂接入；下方 E.3 保留原始 HTTP 草案。

`backend/app/submissions/coordinator.py` 的 `SubmissionCoordinator(sandbox_adapter)` 是交卷编排入口，提供 `submit(session_id, idempotency_key)` 和 `cleanup(session_id)`。调用方先验证会话归属，再由协调器提交 `submitting` 门禁、调用注入适配器的 `freeze_and_export(view)`、核验导出的会话与容器身份、持久化答卷、事务提交后尝试 `delete_sandbox(view)`。适配器返回 `FrozenWorkspace(session_id, sandbox_id, files, public_test_output=None)`；具体冻结、超时、保留文件要求见沙箱接入约定。已接入 `SandboxRuntime`；普通 CI 保留替身测试，真实验收另行开启。

`submit()` 返回下述固定回执，重复请求直接返回第一份已提交答卷。冻结、导出或持久化失败抛出带安全 `code` 的 `SubmissionAttemptFailed`，保留原环境及禁止写入状态供重试；并发锁冲突透传 `SessionBusy`。提交已成功时，清理失败不撤销回执，继续出现在 `lifecycle.list_cleanup_candidates()`；后续单独 `cleanup()` 失败可抛出 `SandboxCleanupFailed`。已实现独立的 `SubmissionMaintenance.run_once()` 和 CLI 持续模式，按宿主扫描到期与未完成提交、重试清理，不依赖浏览器在线；共享重试间隔、启动方式及延迟边界见 [提交定期处理](submission-maintenance.md)。这些内部处理不新增候选人 HTTP 清理接口，也不会在 API 导入时自动启动。

`backend/app/submissions/service.py` 中的 `SubmissionService` 提供：

```python
persist_frozen_submission(session_id, idempotency_key, files, *, public_test_output=None)
list_submissions()
get_submission(submission_id)
get_session_receipt(session_id)
```

- `files` 是 `SnapshotFile(path, content)` 的可迭代对象；路径是相对 POSIX 路径，内容为 UTF-8 文本。公开测试输出由调用方提供，服务不执行命令。
- 写入前置条件：调用方已校验归属、完成真正的沙箱写入冻结和进程协调；会话已开始，数据库状态为 `submitting`。仅修改状态字段不能替代冻结沙箱。
- 成功写入后会话状态为 `submitted`，返回 `{submission_id, session_id, snapshot_id, submitted_at, status: "frozen"}`。这里 `frozen` 是提交回执状态。响应只在数据库事务成功提交后交给调用方。
- 同一会话只有一个最终提交。并发请求串行处理，重复请求使用原幂等键或新键均返回首份答卷，不重新消费 `files`。幂等键接受 1～128 位字母、数字、下划线或连字符，前端可使用 UUID。
- `list_submissions()` 返回已持久化摘要：`submission_id`、`session_id`、`candidate_id`、`candidate_name`、`exam_id`、`exam_title`、`admitted_at`、`started_at`、`submitted_at`。
- `get_submission(id)` 查无记录返回 `None`，否则在摘要上增加 `snapshot_id`、`files[{path, content}]` 与 `public_test_output`。身份与时间取自提交当时保存的数据，文件及测试输出仅从固定快照读取。`public_test_output` 为 `None` 和空字符串时保留区别。
- `get_session_receipt(session_id)` 返回该会话已提交的固定回执，尚未提交时为 `None`，用于重试和不确定提交结果的恢复。
- 持有生命周期锁时，使用 `locked.persist_frozen_submission(service, key, files, ...)` 复用当前事务，不能另起普通提交事务。块内返回值须等待锁上下文成功退出后才可响应；清理必须另开锁上下文，通过 `require_cleanup()` 后进行，禁止在尚未提交的事务内删容器。
- 返回字段对齐 `SubmissionReader`，已通过 `create_app` 注入管理端路由，写入和调阅共用同一数据库工厂及快照存储配置；返回值不包含幂等键、凭据哈希、内部磁盘路径或内容存储摘要。
- 底层服务未知会话抛出 `SubmissionNotFound`；未开始或状态不正确抛出 `InvalidSubmissionState`；快照输入不合法抛出 `SnapshotValidationError`；文件丢失、损坏或存储不可用抛出 `SnapshotStorageError`。候选人 HTTP 经协调器调用，使用上一节的领域错误与恢复语义。

数据库错误会回滚提交、快照元数据和会话更新。文件系统中可能保留未引用对象，查询不会将其显示为成功提交；当前未实现垃圾回收。存储位置与大小限制见根 README。

---
### E\.3 核心联调接口契约 \(v1\.0\-draft\，原始参考)



- **通信基准**：

    - **HTTP Base URL**：`http://localhost:8000/api/v1`

    - **WebSocket Base URL**：`ws://``localhost:8000/api/v1`

    - **通用请求头**：`Content-Type: application/json`



#### 1\. 凭证接入换取会话

- **接口定义**：`POST /sessions/verify-token`

- **协同方**：刘婧瑶（前端调用） ↔ 陈智恒（后端实现）

- **请求体 \(JSON\)**：

```JSON
{
  "token": "tok_vibe_9f82a1c0d4"
}
```

- **响应体 \(200 OK\)**：

```JSON
{
  "session_id": "ses_01j7c390a8e1b",
  "status": "provisioning",
  "exam_info": {
    "title": "Python工程缺陷修复考核",
    "duration_minutes": 60
  }
}
```



#### 2\. 轮询沙箱就绪状态

- **接口定义**：`GET /sessions/{session_id}/status`

- **协同方**：刘婧瑶（前端调用） ↔ 陈弘毅（后端实现）

- **响应体 \(200 OK\)**：

```JSON
{
  "session_id": "ses_01j7c390a8e1b",
  "status": "ready",
  "time_remaining_seconds": 3600
}
```



#### 3\. 拉取工作区文件树

- **接口定义**：`GET /sessions/{session_id}/files`

- **协同方**：刘婧瑶（前端渲染） ↔ 陈弘毅（沙箱读取）

- **响应体 \(200 OK\)**：

```JSON
{
  "files": [
    { "path": "README.md", "is_dir": false, "version": 1, "readonly": true },
    { "path": "src/main.py", "is_dir": false, "version": 1, "readonly": false },
    { "path": "tests/test_public.py", "is_dir": false, "version": 1, "readonly": true }
  ]
}
```



#### 4\. 读取与保存文件内容

- **读取接口**：`GET /sessions/{session_id}/file?path=src/main.py`

    - **响应体 \(200 OK\)**：

```JSON
{
  "path": "src/main.py",
  "version": 1,
  "content": "def fix_me():\n    return False\n"
}
```



- **持久化保存接口（5秒防抖）**：`PUT /sessions/{session_id}/file`

    - **请求体 \(JSON\)**：

```JSON
{
  "path": "src/main.py",
  "expected_version": 1,
  "content": "def fix_me():\n    return True\n"
}
```

- **响应体 \(200 OK\)**：

```JSON
{
  "path": "src/main.py",
  "new_version": 2,
  "saved_at": 1730001230
}
```



#### 5\. 伪终端长连接通信 \(PTY\)

- **接口定义**：`WSS /sessions/{session_id}/terminal`

- **协同方**：刘婧瑶（前端 xterm） ↔ 陈弘毅（后端 PTY 网关）

- **文本帧协议格式 \(JSON\)**：

    - **客户端发送按键/命令**：

```JSON
{ "type": "input", "data": "pytest\r" }
```

- **服务端实时字符输出**：

```JSON
{ "type": "output", "data": "==== 2 passed in 0.12s ====\r\n" }
```

- **客户端窗口尺寸自适应**：

```JSON
{ "type": "resize", "cols": 120, "rows": 32 }
```



#### 6\. 主动交卷与快照冻结

- **接口定义**：`POST /sessions/{session_id}/submit`

- **请求头**：`Idempotency-Key: {client_uuid}`

- **协同方**：刘婧瑶（前端调用） ↔ 赵晨一（后端落盘入库）

- **响应体 \(202 Accepted\)**：

```JSON
{
  "submission_id": "sub_99a8b7c6d5e4",
  "session_id": "ses_01j7c390a8e1b",
  "status": "frozen",
  "submitted_at": 1730004800,
  "snapshot_id": "snp_33214abc"
}
```



#### 7\. 考官后台调阅提交数据

- **接口定义**：`GET /admin/submissions/{submission_id}`

- **请求头**：`Authorization: Bearer {jwt_token}`

- **协同方**：陈智恒（前端页面） ↔ 赵晨一（持久化查询）

- **响应体 \(200 OK\)**：

```JSON
{
  "submission_id": "sub_99a8b7c6d5e4",
  "submitted_at": 1730004800,
  "public_test_output": "2 passed, 0 failed in 0.08s",
  "files": [
    {
      "path": "src/main.py",
      "content": "def fix_me():\n    return True\n"
    }
  ]
}
```



---

## 沙箱准备与开始接口（成员 C 开发中）

显式 app.sandbox.http_app:create_app 新增 POST /api/v1/sessions/{session_id}/prepare 和 POST /api/v1/sessions/{session_id}/start。复用 require_candidate_session、Cookie与 X-VibeEval-Request: 1；默认 app.main 未挂载。请求无需body，镜像、宿主和题目版本由后端 SANDBOX_IMAGE_REF / SANDBOX_BACKEND_REF / SANDBOX_TASK_VERSION 指定，禁止浏览器选择镜像。

成功返回 session_id/status/started_at/deadline_at；不返回容器ID。prepare 同步执行有界沙箱调用，耗时可能达数十秒；不是后台排队202。前端应禁用重复按钮，超时后先查询已有status。409表示状态冲突或会话忙，503表示配置或准备失败。start只在共享生命周期允许时开始，重复开始不刷新截止时间。

健康检查只检查Python3.12、非root身份、六文件可读/可写权限与父目录保护，不运行考生代码；两项预期失败测试不作为健康失败。磁盘配额和完整前端联合验收尚待完成。

## 工作区读取与 PTY（成员 C）

显式沙箱入口已挂载 WS `/api/v1/sessions/{session_id}/terminal`：原浏览器 Cookie 鉴权，精确 Origin 允许名单，只有 `in_progress` 且未到截止时间才能连接、输入或 resize。协议沿用上述 input/output/resize；Ctrl+C 原样发送 `\u0003`。完整配置、限制、关闭码、断线语义和真实验证命令见 [终端与文件交接](workspace-terminal-handoff.md)。

GET `/files` 和 GET `/file?path=...` 已接入 `LockedSession.observe_file_revision` 与持久版本表，先执行 Alembic `upgrade head` 到 `0005`。首次观察返回版本 1，相同内容保持版本，观察到变化时递增，记录与读取使用同一会话事务，成功提交后才返回浏览器；内部契约见 [工作区文件版本](workspace-file-revisions.md)。响应结构沿用附录 E；会话忙返回 409 与 `Retry-After: 1`，未开考/已结束返回 409，非法路径返回 422，归属不符返回 404。仍保留方法缺失时的 503 `FILE_REVISIONS_UNAVAILABLE` 兜底，数据库或读取故障返回 503。

PUT `/file` 已提供，沿用 `path/expected_version/content` 请求和 `path/new_version/saved_at` 响应。版本必须为正整数，内容限 UTF-8 编码后 1 MiB。相同内容保存保持版本；旧版本或暂停前发生的终端修改返回 409 `FILE_VERSION_CONFLICT`，detail 包含 `current_version`，不会覆盖当前内容。前端保留本地编辑并重新读取比较，不得自动套用新版本强行重发。

保存先取得共享会话锁，暂停考生进程后再次核对摘要，由受限临时 runsc 容器执行固定脚本原子替换；确认写入容器已移除后才恢复原进程。只读文件返回 403；非法参数返回 422；会话忙为 409 `SESSION_BUSY`，可按 `Retry-After: 1` 重试；状态不允许为 409 `SESSION_NOT_WRITABLE`。保存异常返回 503 `FILE_SAVE_UNCERTAIN`，需重新读取核对，原环境可能保持暂停；显式确认后可重试，不能自动覆盖。文件替换与数据库提交不是同一事务，下一次读取会根据实际内容恢复版本。

交卷前前端应 flush 并等待保存成功。冻结会先停止遗留写入容器，再导出稳定文件；固定 staging 文件不属于答卷。API 与维护进程需同时升级适配器，不能混用新保存模块和旧冻结模块。保存不会延长考试截止时间。详细恢复语义见 [工作区交接](workspace-terminal-handoff.md)。

本期固定六文件，只有 `src/booking.py`、预建的 `tests/test_candidate.py` 可编辑；草案中 `src/main.py` 的可写示例已被陈智恒确认的会议室题目权限取代。只读文件不能由终端删除或替换。

## 考官快照评测（Release 1 补充）

- `GET /api/v1/admin/submissions/{submission_id}/evaluation`：考官 Cookie 鉴权，返回相对实际发布版本的文件变化/差异、`report` 与最近尝试 `last_attempt`、结果满分 60、过程满分 40。GET 不启动环境。只支持已提交快照和明确配置的题目版本；演示数据不评分。
- `POST` 同一路径：额外要求 `X-VibeEval-Request: 1`。考官主动触发一次独立 gVisor 评测，成功返回持久化报告；单 worker 同时只执行一轮，忙时 409。调用方需允许约 120 秒等待，超时后可 GET 查看是否已落盘，不能用网络超时推断零分。
- `status=completed` 表示评测执行完成，`passed/total` 为可信公开行为测试通过数，`score=passed/8*60`，`tests` 为 8 项逐项状态和分值，`output` 为环境外可信测试控制器生成的记录。候选代码失败可为 0 分；`infrastructure_failed` 的 `score=null`，不可作为零分。
- 报告包含 `run_id/submission_id/snapshot_id/task_version/image_ref/rule_version/baseline_sha256/evaluated_at`。每轮原子追加文件，不改冻结快照；失败重试后仍展示此前同版本的成功结果，同时提示最近失败。过程分与总分为 null。新写考生测试不增加分母，公开测试通过不等同完整能力结论。
- 仅当前版本 `python-debug-01:f898a77` 有评分规则，未知题目版本返回 409；答卷缺失 404，环境或存储不可用 503。完整报告文件与快照同卷备份；本轮不新增数据库迁移。

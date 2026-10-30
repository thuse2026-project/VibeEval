# Release 1.0 接口草案

本页从原始需求文档附录 E.3 提取，保留原始示例供组内讨论，不表示接口已实现或已确认。

一期范围与分工见 [原始需求文档](requirements/VibeEval-AICoding%20平台.md) 附录 E。确认后的联调契约统一更新本页，原始文档保留讨论背景。

## 待确认项

- 考生 WebSocket 鉴权和考试状态流转（HTTP 入场及恢复已约定如下）。
- 真实提交服务接入与答卷详情接口（考官总览查询已实现）。
- 会话状态、开始计时与截止后的处理方式。
- 公开测试的执行入口，以及结果如何保存并关联提交。
- 提交是同步完成还是异步处理，以及冻结失败、重复提交和状态查询的语义。
- 文件版本冲突、越界路径、只读文件和冻结后操作的错误约定。
- 终端退出、断开及错误消息的约定。

## 考官登录（本轮实现）

- 账号由本地初始化命令预配置，无注册接口。
- `POST /api/v1/admin/login`：JSON `{ "username": "examiner", "password": "..." }`，成功返回 `{ "username": "examiner", "name": "陈智恒", "expires_at": 1793336400, "server_time": 1793325600 }`（时间为 Unix 秒，仅作示例）。同时写入 `vibeeval_admin` HttpOnly、SameSite=Strict Cookie，路径 `/api/v1/admin`，有效期固定 10800 秒。
- `GET /api/v1/admin/me`：验证 Cookie，返回上述身份及原始到期时间；不会延长有效期。后续考官接口复用后端 `current_session` 鉴权依赖。
- `POST /api/v1/admin/logout`：撤销当前会话并清除 Cookie，返回 204；重复退出同样返回 204。
- 两个 POST 需带 `X-VibeEval-Request: 1`；前端同源调用，不开放跨域 CORS。正式部署使用 HTTPS 并启用 Secure Cookie。
- 错误：401（凭证错误或未登录/过期）、403（缺少请求来源校验头）、422（请求字段不合法）、429（同一客户端地址 60 秒内已失败 5 次）、503（未配置账号）；错误正文为 FastAPI 的 `detail` 字段。客户端展示友好提示，不区分未知账号与密码错误。
- 当前会话仅存在单进程内存，重启后失效。此约定取代下方原始草案中的管理端 Bearer JWT 设想；考务总览查询已实现，真实提交服务接入与答卷详情尚待实现。

## 考务总览（本轮实现）

`GET /api/v1/admin/overview` 使用考官登录 Cookie 鉴权；未登录/过期返回 401，候选人 Cookie 不具备访问权限。当前所有预配置考官均可查看全部场次，无场次分配权限模型。

| 查询参数 | 默认值 | 含义 |
| --- | --- | --- |
| `source` | `live` | `live` 真实记录或 `demo` 显式联调示例，两者不混合 |
| `exam_id` | 不传 | 按场次精确筛选，不传表示全部场次 |
| `status` | `all` | `all`、`invited`、`admitted`、`in_progress`、`submitted`、`invitation_expired`、`recovery_expired` |
| `page` / `page_size` | `1` / `10` | 页码至少 1，单页 1～100 条；超出末页返回末页，无数据返回第 1 页 |

响应：`source`、`items`、筛选后记录数 `total`、实际 `page` / `page_size`、全部场次选项 `exams`、统计 `summary`、读取时间 `updated_at`，及真实提交服务接入标志 `submission_service_connected`（当前 false）。非法查询参数返回 422，所有响应禁止缓存。

每条记录字段：`candidate_id`、`candidate_name`、`exam_id`、`exam_title`、`status`、`session_id`（未入场为 null）、`admitted_at`、`started_at`、`submission_id`、`submitted_at`。时间为 Unix 秒，尚未发生的时间为 null；不返回入场或恢复凭证及哈希。

`summary` 包含 `total`、`not_submitted`、`submitted`，计算所选场次的全部状态和全部页，不受状态筛选影响；按 `(exam_id, candidate_id)` 计人次。列表按最近提交/入场时间倒序，未入场放最后，同时间以场次和考生编号稳定排序。前端改变来源、场次或状态时重置页码；改变来源还重置其他筛选。

真实记录当前只读取已预配置凭证和入场数据：`invited` 尚未入场，`admitted` 已入场未开始；未入场凭证过期为 `invitation_expired`，入场恢复期结束为 `recovery_expired`。入场不等于开始或交卷，未提交记录的 `submission_id`、`submitted_at` 必须为 null。

**持久化成员接入点**：`backend/app/admin/overview.py` 的 `read_live_records(store)` 返回上述最小记录列表。接入提交服务时按考生与场次关联并避免重复行，可补充仅存在于提交库中的历史记录；只有存在真实开始记录才返回 `in_progress` / `started_at`，只有真实持久化提交才返回 `submitted` / `submission_id` / `submitted_at`。同时更新 `submission_service_connected` 为 true。不读取活动沙箱来推断已提交答卷，不返回代码内容。查询筛选与统计可随后迁移至 PostgreSQL，目前课程规模采用本地列表处理。

`demo` 为只读生成的 12 条虚构记录，两个场次覆盖四种进度状态；明确标记联调示例，不改变真实数据，不代表提交服务已完成。本轮不实现交卷、快照、评分或详情接口。

## 考生入场与恢复（已实现）

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

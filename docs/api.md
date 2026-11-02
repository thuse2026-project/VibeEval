# Release 1.0 接口草案

本页从原始需求文档附录 E.3 提取，保留原始示例供组内讨论，不表示接口已实现或已确认。

一期范围与分工见 [原始需求文档](requirements/VibeEval-AICoding%20平台.md) 附录 E。确认后的联调契约统一更新本页，原始文档保留讨论背景。

## 待确认项

- 考生会话访问凭证及 HTTP / WebSocket 鉴权方式；不能只凭 session_id 访问。
- 考官登录、场次提交列表的接口。
- 会话状态、开始计时与截止后的处理方式。
- 公开测试的执行入口，以及结果如何保存并关联提交。
- 提交是同步完成还是异步处理，以及冻结失败、重复提交和状态查询的语义。
- 文件版本冲突、越界路径、只读文件和冻结后操作的错误约定。
- 终端退出、断开及错误消息的约定。

## 提交持久化内部契约

共享会话、容器映射、跨进程行锁、状态集合及提交失败后的恢复规则见 [沙箱接入约定](sandbox-integration.md)，对应迁移 `0004`。`SessionView.task_version_id` 与 `sandbox_image_ref` 分别提供题目发布版本和镜像引用；提交台账会冻结保存两者，现有管理端 reader 字段保持不变。已有 HTTP 草案仍待对齐这些内部状态和鉴权方式。

以下为当前分支已实现的 Python 内部服务，HTTP 交卷和沙箱冻结尚未接入，下方 E.3 仍是 HTTP 草案。

`backend/app/submissions/coordinator.py` 的 `SubmissionCoordinator(sandbox_adapter)` 是交卷编排入口，提供 `submit(session_id, idempotency_key)` 和 `cleanup(session_id)`。调用方先验证会话归属，再由协调器提交 `submitting` 门禁、调用注入适配器的 `freeze_and_export(view)`、核验导出的会话与容器身份、持久化答卷、事务提交后尝试 `delete_sandbox(view)`。适配器返回 `FrozenWorkspace(session_id, sandbox_id, files, public_test_output=None)`；具体冻结、超时、保留文件要求见沙箱接入约定。当前只以替身验证该接口，尚无真实沙箱适配器或 HTTP 路由。

`submit()` 返回下述固定回执，重复请求直接返回第一份已提交答卷。冻结、导出或持久化失败抛出带安全 `code` 的 `SubmissionAttemptFailed`，保留原环境及禁止写入状态供重试；并发锁冲突透传 `SessionBusy`。提交已成功时，清理失败不撤销回执，继续出现在 `lifecycle.list_cleanup_candidates()`；后续单独 `cleanup()` 失败可抛出 `SandboxCleanupFailed`。数据库异常后的回执恢复、错误代码和无自动 worker 的边界见沙箱接入约定；这些内部异常暂不规定 HTTP 状态码。

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
- 返回字段对齐队友 `feat/admin-submission-details` 分支的 `SubmissionReader`，尚未注入管理端路由；返回值不包含幂等键、凭据哈希、内部磁盘路径或内容存储摘要。
- 未知会话抛出 `SubmissionNotFound`；未开始或状态不正确抛出 `InvalidSubmissionState`；快照输入不合法抛出 `SnapshotValidationError`；文件丢失、损坏或存储不可用抛出 `SnapshotStorageError`。HTTP 状态映射和冻结失败后的会话恢复策略在路由接入时对齐。

数据库错误会回滚提交、快照元数据和会话更新。文件系统中可能保留未引用对象，查询不会将其显示为成功提交；当前未实现垃圾回收。存储位置与大小限制见根 README。

---
### E\.3 核心联调接口契约 \(v1\.0\-draft\)



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

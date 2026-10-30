# Release 1.0 接口草案

本页从原始需求文档附录 E.3 提取，保留原始示例供组内讨论，不表示接口已实现或已确认。

一期范围与分工见 [原始需求文档](requirements/VibeEval-AICoding%20平台.md) 附录 E。确认后的联调契约统一更新本页，原始文档保留讨论背景。

## 待确认项

- 考生会话访问凭证及 HTTP / WebSocket 鉴权方式；不能只凭 session_id 访问。
- 场次提交列表的接口（考官登录约定见下节）。
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
- 当前会话仅存在单进程内存，重启后失效。此约定取代下方原始草案中的管理端 Bearer JWT 设想；后续提交查询接口尚未实现。

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

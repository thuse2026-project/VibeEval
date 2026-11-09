# Agent 接入第五批 A：持久事件订阅与断线续接

状态：2026-11-08，用户已确认，随基本 Agent 封装提交。
第四批 B 已确认并提交 `a5496a5`。本批只改后端。

## 范围

新增 SSE 传输层，重放并订阅现有 PostgreSQL agent_events：
运行、工具、批准/提问、命令输出、文件变更和压缩事件沿用原有记录。
不新增会话状态机、模型返回协议或工具执行路径，不需要新迁移或依赖。
现有 JSON `GET /events` 保持原有响应结构。

本批提供持久事件流；模型文字/工具参数的供应商增量流留到第五批 B。
完成事件到达后，完整回答仍从 history 读取；批准细节、命令/文件回执分别从
interactions/operations 读取。没有把事件通知当作完整模型回答。

订阅不会调用模型、推进 Run、查询沙箱或重新执行工具。
运行仍由已有 advance/执行器观察入口驱动，输出先入库，再成为可订阅事件。
浏览器断开只关闭订阅，不中断 Run；中断仍使用独立的 interrupt/cancel 接口。
本批不开发前端，也不部署常驻 worker。

## HTTP 契约

前缀 `/api/v1/sessions/{session_id}/agent/conversations/{conversation_id}`：

| 方法 | 后缀 | 作用 |
| --- | --- | --- |
| GET | /events/stream | 有界重放并跟随已提交事件 |

参数：after_sequence 默认为 0，取值 0 至 PostgreSQL bigint 上限；follow 默认 true。
follow=false 只读取连接开始时的已提交 head，分页期间的新事件留给下一次订阅。
follow=true 先追平历史，再轮询新提交。

沿用考生 Cookie、Agent 开关、考试/对话归属、`X-VibeEval-Request: 1` 和 no-store。
每批发送前重新检查开关与 Cookie 归属；开关关闭或权限失效时结束订阅。
首次鉴权、非法游标或数据库失败通过普通 HTTP 403/404/409/422/503 返回，
不会先发 200 再把这类初始错误藏进流中。

成功返回 `Content-Type: text/event-stream; charset=utf-8`、`Cache-Control: no-store`、
`X-Accel-Buffering: no`。实际代理部署和长连接行为仍须联合验收。

原生浏览器 EventSource 的初始化参数不提供自定义请求头；本接口保留来源校验头，
调用方使用 fetch 流式读取或可设置请求头的 SSE/HTTP 客户端。
协议依据 [WHATWG Server-sent events](https://html.spec.whatwg.org/multipage/server-sent-events.html)。

## 业务帧与游标

每个实际业务事件使用固定 `event: agent.event`，data 是一行 JSON：

```text
id: v1.<base64url-conversation-id>.<sequence>
event: agent.event
data: {"version":1,"conversation_id":"...","sequence":1,"kind":"conversation.created","data":{"revision":0,"turn_id":null,"status":"idle"},"created_at":1794000000}

```

data 的 sequence/kind/data/created_at 与 JSON events 接口相同，另有 version/conversation_id。
命令输出仍是原 operation.output 的 stream、byte_offset、data_b64；传输层不改写输出或退出状态。
客户端根据业务 kind 分派，并按事件 ID 去重，避免重复追加重放的输出片段。

游标同时绑定协议版本、对话 ID 与事件序号，不使用 Turn revision、消息 ID 或材料 ID。
客户端在完整处理业务帧后保存整个 id 字符串，并在重连时提交 `Last-Event-ID`。
该头优先于 URL 中原有的 after_sequence，避免初始 URL 阻断标准重连。
其他对话的游标返回 409；非法、非规范编码或溢出返回 422；超前于已提交 head 返回 409。
事件记录有缺口时明确返回 AGENT_EVENT_GAP，调用方重新核对快照/历史，不能静默跳过。

传输可以重放；网络中断附近可能重复交付。订阅和重连均不会触发任何业务重执行。
控制帧、心跳没有 id，不把尚未发送的记录标记为已经收到。
当前事件日志没有自动裁剪；将来引入保留策略时仍须显式处理游标失效。

## 控制帧与连接边界

| 帧 | 含义 |
| --- | --- |
| retry: 1000 | 建议的重连间隔，毫秒 |
| stream.ready | 本次连接的 after_sequence、已提交 head 和跟随模式 |
| : keep-alive | 空闲心跳，不产生持久事件或改变业务游标 |
| stream.end | 重放完成、连接时间窗结束或业务数据预算达到上限 |
| stream.closed | 已追平已结束/正在交卷/到期考试的历史，停止继续跟随 |
| stream.error | 建连后出现的来源故障、权限变化或记录异常；不返回原始异常正文 |

结束/错误控制帧提供 code、retryable、next_sequence 和 cursor，均指向最后实际发送的业务记录。
客户端根据 retryable 决定重连或停止；不会把控制帧中的 cursor 当作一个新的业务事件 ID。
重放完成和考试关闭不建议继续重连；连接时间窗/字节数/事件数达到上限可继续按游标读取。

默认服务端限制：每批 50 条，最多 500 条业务事件、业务帧总量 1 MiB、单帧 128 KiB；
正常跟随时间窗 25 秒，轮询间隔 0.5 秒，空闲心跳间隔 10 秒。
这些是服务器 EventStreamPolicy，不能由浏览器扩大；控制帧和心跳是额外的有限传输开销。
单条记录超出帧限制时明确报错并保留原位置，可通过已有 JSON events/关联记录接口核查。

数据库查询完成后立即关闭读事务；等待、网络发送和心跳期间不持有数据库连接或行锁。
同步鉴权/查询在线程执行，不阻塞异步流的事件循环。
连接时间窗是正常轮询/发送边界，不承诺基础设施不可用时的数据库连接硬超时。
数据源故障会以安全的错误代码结束；取消任务原样传播，不转换成工具失败。

## 数据库一致性

沿用原会话事件的事务序号；SSE 不建立旁路内存队列，也不制造额外业务事件。
每批先读取归属和已提交 head，再只取 sequence <= head 的连续记录。
在两次查询之间提交的新记录留在下一批，避免把新事件与旧 head 混为一次快照。
未提交或回滚的记录不可见；重启应用后仍可用同一游标从 PostgreSQL 续读。

考试到期或开始交卷后，允许读完已保存历史并发送 stream.closed；不会恢复沙箱。
若关闭前的积压超过本次连接预算，先返回可续读的 stream.end，后续连接继续排空。
作用域与 Cookie 始终独立验证，游标不能作为访问令牌。

## 可运行的后端调用示例

在已安装项目依赖的 Python 环境中，以当前考生自己的 Cookie 和 ID 调用。
Cookie 通过环境变量提供，不写进源码或命令参数；以下只做一次有限重放：

```python
import os
import httpx

base = "http://127.0.0.1:8000"
exam = os.environ["VIBEEVAL_EXAM_SESSION_ID"]
conversation = os.environ["VIBEEVAL_CONVERSATION_ID"]
url = f"{base}/api/v1/sessions/{exam}/agent/conversations/{conversation}/events/stream"
headers = {"X-VibeEval-Request": "1"}
if os.environ.get("VIBEEVAL_LAST_EVENT_ID"):
    headers["Last-Event-ID"] = os.environ["VIBEEVAL_LAST_EVENT_ID"]
with httpx.stream(
    "GET", url, headers=headers,
    cookies={"vibeeval_candidate": os.environ["VIBEEVAL_CANDIDATE_COOKIE"]},
    params={"follow": "false"}, timeout=35, follow_redirects=False,
) as response:
    response.raise_for_status()
    for line in response.iter_lines():
        print(line)
```

实际消费方应解析完整的空行分隔帧，处理 agent.event 后再保存 id；
上例打印协议以便联调，不自动推进 Run 或写回业务状态。

## 验证

新增纯协议测试覆盖游标作用域/规范编码、Unicode/换行安全、快照重放、新事件跟随、
心跳、时长/事件数/字节预算、断线与取消、权限失效、错误脱敏和缺口。
真实隔离 PostgreSQL 测试覆盖提交/回滚可见性、并发 head、跨应用续读、
鉴权和初始 HTTP 错误、交卷结束；真实 loopback HTTP 验证连接中后续提交的增量到达及关闭开关。
这批测试不调用模型或候选命令，不修改前端。

```powershell
# backend/；只创建并清理自有临时 PostgreSQL
..\.venv\Scripts\python.exe tests/manual/agent_database_probe.py --all-tests
```

2026-11-08 最终验证：完整后端 563 项，548 项通过，15 项真实沙箱用例按配置跳过；
数据库用例全部启用，迁移升降级与模型一致性通过。
新增 24 项全部通过（14 项纯协议测试，10 项真实 PostgreSQL 测试，其中含实际 HTTP 增量传输）。
全应用行覆盖率 87.0%，event_stream 模块 94.6%。
Black、Ruff、依赖检查和密钥扫描通过；临时数据库、HTTP 监听和连接已清理。
前端未改动；本批已获用户确认，随基本 Agent 封装提交。

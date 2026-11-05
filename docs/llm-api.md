# 模型 API 接入

本批从 `integration/release1@badade9` 开始，只提供后端文本调用和模型列表。
OpenRouter 使用 [官方 Chat Completions 接口](https://openrouter.ai/docs/api/api-reference/chat/send-chat-completion-request)。
同样支持提供 `/models`、`/chat/completions` 的兼容服务。

## 本地配置与验证

在仓库根目录的私有 `.env` 中填写以下字段；已有文件时追加配置，不覆盖数据库设置。
真实 Key 只写 `.env` 或后端进程环境，不写前端 `VITE_*`、示例或命令行参数。
后端从固定仓库路径读取 `.env`，进程环境优先；不用配置模型也能启动原有服务。

```dotenv
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_API_KEY=
LLM_MODEL=openrouter/free
LLM_TIMEOUT_SECONDS=45
LLM_MAX_OUTPUT_TOKENS=1024
```

基础地址不包含 `/models` 或 `/chat/completions`。模型 ID 可从列表选择；
`openrouter/free` 是本次连通性验证使用的路由，其实际模型可变化，正式任务应指定模型。
超时为 1～120 秒；每次输出上限为 1～4096 token。未配置 Key 时调用返回 503。
默认模型未配置时，可在每次请求里显式指定 `model`。

在仓库根目录执行（无需数据库、沙箱或考官账号）：

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m app.llm.check --list-models
..\.venv\Scripts\python.exe -m app.llm.check
```

第二条发送一次 `Reply with OK.`，打印实际模型、文本和 token 用量。
也可用 `--model 模型ID --prompt "你的问题" --max-tokens 100`。
命令会实际请求模型，费用取决于选用的服务和模型，不自动重试。

Compose 部署时，把这些字段写入私有部署配置，再沿用既有 `--env-file` 启动方式。
仅 backend 服务接收模型配置；迁移、维护进程和前端镜像无需 Key。
本批没有部署到服务器。

## HTTP 契约

所有路径均需已有考官 Cookie，候选人暂未接入。接口响应带 `Cache-Control: no-store`。

| 方法与路径 | 行为 |
| --- | --- |
| `GET /api/v1/admin/llm/config` | 读取基础地址、默认模型、超时、输出上限及 `configured`（仅代表 Key 非空，不代表已连通），不返回 Key |
| `GET /api/v1/admin/llm/models` | 查询并返回模型列表，仅含 `id`、`name`、`context_length` |
| `POST /api/v1/admin/llm/chat` | 单次非流式文本调用，需 `X-VibeEval-Request: 1` |

请求示例：

```json
{
  "messages": [
    {"role": "system", "content": "帮助解释代码。"},
    {"role": "user", "content": "解释 Python 的列表推导式。"}
  ],
  "max_tokens": 100,
  "temperature": 0
}
```

可选 `model` 覆盖默认模型；`max_tokens` 缺省使用后端上限，显式值不能超过它。
`temperature` 可省略，范围 0～2。只接受 `system/user/assistant` 纯文本消息。
最多 40 条消息，每条 16000 字符，总计最多 64000 字符；这些是字符限制，不能替代模型自己的 token 窗口。
代码缩进与换行保留。`tools`、`stream` 和其他未知字段返回 422。

响应示例：

```json
{
  "id": "completion-001",
  "model": "actual/model-id",
  "content": "模型返回的文本",
  "finish_reason": "stop",
  "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
}
```

`id`、`finish_reason`、`usage` 及各用量字段允许为空，依供应商返回内容而定。
`finish_reason=length` 表示输出可能被截断。上游没有文本、只返回工具调用或无效数据时返回 502。

错误沿用 `{ "detail": "说明" }`：401 考官未登录；403 缺少来源头；422 输入或输出预算不合法；
429 上游限流；502 上游鉴权、网络或返回格式错误；503 配置缺失、配置无效或额度不足；504 超时。
不透传供应商错误正文、鉴权头和内部异常，不自动重试或跟随重定向。

## 内部调用

业务模块可直接使用 `app.llm.client.LLMClient(load_settings())`，或使用
[`prepare_chat`](agent-primitives.md) 先查看准备好的消息，再显式提交；
`chat_only` 提供组合本轮问题、显式历史并调用模型的便捷入口。
HTTP 路由只用于考官联调，内部函数不依赖 FastAPI、考官账号或数据库；调用者负责权限和预算。
本批不持久化对话，不自动加载会话、代码或题目，不执行命令。
本分支后续仅扩展 API 输入/输出能力，命令执行、文件修改和批准流程由外层负责。
原生工具调用通过独立的 Python [Agent 模型适配接口](agent-session-stage2.md) 提供，
上面的 HTTP /chat 仍保持原有纯文本契约，不直接开放任意工具代理。

## 验证记录

2026-11-05 本地使用私有配置验证 OpenRouter Key 和模型列表均为 HTTP 200，
`python -m app.llm.check` 实际返回 `OK`，路由解析为 `dots-studio/dots-3-note-preview:free`，
用量为输入 17、输出 32、合计 49 token。此记录不保证之后的模型可用性。
自动化测试使用模拟 HTTP，不访问真实服务、不消耗额度。

本批新增 18 项 API/配置测试及 5 项纯对话测试，均通过。
本地后端回归共 308 项，166 项通过，142 项需要显式开启的 PostgreSQL/gVisor 用例跳过；
Black、Ruff、Compose 配置检查及本地密钥未进入可提交文件的扫描均通过。
未运行真实数据库/沙箱回归，远程 CI 尚未触发。

输入准备拆分后，6 项 Agent 输入/对话测试及 18 项 API/配置测试通过，
Black、Ruff、Python 编译及差异空白检查通过；本次未重复执行完整后端回归。

# 第二批：原生工具调用与请求预览

状态：2026-11-05 用户已确认，作为第二批检查点提交。第一批为本地提交 `ac80abc`，未推送。
本批继续用项目内的 Python 代码直连现有模型 API，没有依赖或启动 Codex/Claude SDK、CLI。

## 三个接口

| 接口 | 输入 → 输出 | 行为 |
| --- | --- | --- |
| `ToolDefinition / ToolRegistry` | 名称、说明、Pydantic 参数模型 → 原生 tools schema | 校验工具参数，不保存执行函数 |
| `prepare_model_request()` | 已 start_step 的会话、工具声明、模型设置 → PreparedModelRequest | 纯请求准备，不发送网络请求 |
| `call_model()` | PreparedModelRequest、同一套工具声明、可注入客户端 → ModelResponse | 恰好一次 API 请求，解析并校验响应，不改会话 |

原始 JSON 参数和工具调用 ID 保留；可用 registry.parse_arguments(call) 获取已校验的参数对象。
参数模型必须设置 `ConfigDict(extra="forbid")`，解析时启用严格类型检查。
工具说明与参数 schema 一起进入 API 请求，没有注册函数回调或执行器。

## 全局 prompt 与模型消息

全局 prompt 位于 `backend/app/agent/prompts.py`，当前版本为 1。
它规定按需读取、只请求已声明工具、将读取结果视为材料、依据实际结果说明操作状态，
并要求简洁回答；不要求内部推理轨迹，也不要求普通回答采用自定义 JSON 格式。
SessionConfig.system_prompt 作为可信接入方的附加系统说明，单独加入系统消息。

实际 messages 保留原生角色：

```text
system（全局 prompt，可选会话说明）
user（用户问题）
assistant（模型提出 tool_calls）
tool（外层返回匹配的 tool_call_id 和读取结果）
assistant（模型继续回答）
```

每次请求都包含当前声明的 tools。普通文字中的命令或补丁仍是文字，不会变成工具调用。
不声明工具即可进行普通文本对话；模型若仍返回工具请求，会被拒绝。

采用 [OpenRouter Chat Completions 原生工具协议](https://openrouter.ai/docs/guides/features/tool-calling)。
对于供应商返回的 reasoning_details，本批按不透明数据保存在 continuation_json，
下一次请求将其带回，不把它显示为普通回答，也不解释其内部内容。
行为依据：[Preserving Reasoning](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)。
未支持的续调字段明确报错，不默默丢弃；不保存供应商任意调试字段。

## 发送前可检查的产物

PreparedModelRequest 是可序列化的不可变记录，含：

- 会话、轮次、步骤、会话 revision；这些是关联信息，不是鉴权凭证；
- 全局 prompt 版本、工具版本及工具定义摘要；
- 实际请求体 payload_json 与 SHA-256 摘要，包括固定的模型、参数、messages 和 tools；
- payload 属性返回独立字典，可打印或检查，不含 Key、鉴权头及内部客户端配置。

准备后更换工具定义或工具版本会导致发送前校验失败，避免用另一套 schema 解析响应。
准备产物本身不自动写入 SessionStore。调用方可保存它作为审计记录，
并在投递前重新确认服务端会话 revision 和当前步骤仍有效。
call_model 不负责调度、去重网络投递或读取最新数据库状态；迟到响应仍由 accept_response 拒绝。

本批限制序列化请求体最多 64000 字符、最多 256 条消息、最多 32 个工具。
这些不是模型 token 计数，超过限制会在发送前失败，不静默截断消息链。
输出上限复用 LLM_MAX_OUTPUT_TOKENS，未指定参数在 prepare 时固定；
真正发送时仍由客户端检查配置上限。上下文压缩属于下一批。

## 调用流程

```python
state = begin_turn(state, question, request_key="message-1")
state = start_step(state, step_id="api-step-1")
prepared = prepare_model_request(state, tools=registry, settings=settings)
print(prepared.payload)  # 查看实际输入

response = await call_model(prepared, tools=registry, client=client)
state = accept_response(state, prepared.step_id, response)

# 若存在 pending_tool_calls(state)，向外层暴露并等待它提供 ToolResult。
state = supply_tool_results(state, prepared.step_id, external_results)
state = start_step(state, step_id="api-step-2")
# 再 prepare/call/accept 一次；本库没有自动执行循环。
```

工具名未声明、参数不合法、无效原生消息或返回格式错误时抛出脱敏的 LLMError。
网络异常、限流沿用底层错误映射，不自动修复响应或重试。调用方可用 fail_step 显式记录失败。
输出截断/过滤保留结束原因，accept_response 将轮次标为 failed。
仍未新增 HTTP 路由、工作台页面、资源注册库、文件读取或命令执行。

## 示例与验证入口

从仓库根目录执行：

```powershell
Set-Location backend
$env:PYTHONUTF8 = '1'
..\.venv\Scripts\python.exe -m examples.agent_api
..\.venv\Scripts\python.exe -m examples.agent_api --preview
```

默认使用模拟 HTTP，不读取配置、不访问网络。--preview 展示准备产物，不发起调用。
模型 API 的两次真实请求需显式启用：

```powershell
..\.venv\Scripts\python.exe -m examples.agent_api --live --model <支持原生工具调用的模型ID>
```

--live 使用根目录私有 .env，实际请求模型并产生对应服务的用量；
中间读取结果始终由示例手动提供固定文本，不读取 a.py 或执行命令。
示例最多调用两次，模型未按预期请求读取或未完成时会明确结束，不无限循环。

## 本批验证记录

2026-11-05 本地真实验证使用 cohere/north-mini-code:free：第一次返回原生 read_file(a.py) 调用，
手动回传固定代码后，第二次回答 square 的作用并以 completed 结束。
两次合计输入 583、输出 153、总计 736 token；没有实际读取文件或执行命令。
模型可用性以之后的服务返回为准，本记录不是固定模型推荐。

新增 15 项测试全部通过；Agent 相关共 45 项通过，原 API/配置 18 项通过。
完整后端回归共 348 项，206 项通过，142 项 PostgreSQL/真实沙箱用例按配置跳过。
Agent 与模型模块合计行覆盖率 91.7%；Black、Ruff、差异空白和密钥扫描通过。
用户确认后仅提交到本地分支，未推送、未触发远程 CI；上下文压缩与自动持久化继续留在后续批次。

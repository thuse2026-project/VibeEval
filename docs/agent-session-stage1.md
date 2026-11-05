# 第一批：本地会话状态与工具交互记录

状态：2026-11-05 用户已确认，作为第一批检查点提交。继续按 [已确认范围](agent-session-design.md) 分批推进。

本批将 Codex 的会话/轮次状态划分、工具调用中断后的消息配对规则，以及 Claude Agent SDK 的工具消息字段，
改写为项目内的 Python 类型和函数。没有依赖或启动 Codex/Claude SDK、CLI，也没有改动现有直接模型 API。
逐模块来源、固定版本和许可证见 [UPSTREAM.md](../backend/app/agent/UPSTREAM.md)。

## 状态与接口

`ConversationState` 保存配置、版本及多个 `TurnState`；每轮保存用户输入和多个 `ModelStep`。
每个步骤保留模型响应、调用 ID、原始 JSON 参数、外部工具结果及用量。
所有对象不可变，每次有效状态变化返回新快照并递增 revision；重复输入/结果返回原状态。

| 函数 | 作用 |
| --- | --- |
| `create_conversation(scope_id, config=...)` | 创建隔离的会话；scope 必须由可信接入方提供 |
| `begin_turn(state, text, request_key=...)` | 追加用户轮次；同一会话忙碌时拒绝新轮次 |
| `start_step(state, step_id=...)` | 记录待调用模型步骤，本函数不发送请求 |
| `accept_response(state, step_id, response)` | 接收一个模型响应，进入等待工具、完成或失败状态 |
| `pending_tool_calls(state)` | 返回当前尚未收到结果的工具请求 |
| `supply_tool_results(state, step_id, results)` | 按调用 ID 接受部分/全部结果；全部到齐后才允许下一模型步骤 |
| `fail_step(state, step_id, error_code=...)` | 显式记录网络等调用失败，不自动重试 |
| `interrupt_turn(state, turn_id)` | 中断当前轮次，不自动取消或重启外部命令 |
| `render_history(state)` | 生成基础 user/assistant/tool 角色投影，保留调用与结果配对 |
| `restore_snapshot(json, scope_id=...)` | 校验并恢复快照；未结束轮次默认中断，不复活在途操作 |

状态路径：ready → awaiting_model → awaiting_tools → ready → awaiting_model → completed。
助手说明文字若同时带工具调用，不算整轮结束。截断/内容过滤进入 failed。
failed/interrupted/completed 均结束当前轮次，下一次用户输入可开启新轮。

## 存储边界

`SessionStore` 定义 create/load/save。内存实现 `MemorySessionStore` 按 scope 和会话 ID 隔离，
save 使用 expected_revision 比较并保存，拒绝并发覆盖和跳版本写入。
它是进程内实现，进程退出后不会自动持久化；上层可保存 `state.model_dump_json()` 并在恢复时调用 restore_snapshot，
或实现同一协议接入共享存储。本批没有数据库迁移，也没有新的 HTTP 接口。

状态转换返回新 revision 才代表新进展；重放 start_step 的同一 ID 不表示应该再次发起网络请求。
网络投递和请求调度尚未接入，本批不承诺跨进程的 exactly-once 外部调用。

## 历史、结果与恢复

- 工具结果保留真实回传顺序；模型消息投影按原工具请求顺序排列，分批返回不会乱配。
- 相同 ID 和相同结果幂等；同一 ID 的不同结果冲突；未知 ID、旧步骤的迟到响应被拒绝。
- 工具失败由 is_error 标记，在模型投影中明确表达错误，而非伪造成功。
- 中断步骤的缺失结果仅在投影里补为 aborted/unknown，原始记录不增加伪造的成功结果。
- 总用量从已接受响应计算，不会因重放重复累加；缺失用量保持未知，不按零计算。
- 供应商续调元数据可保存在 continuation_json，基础展示投影不展开它；下一批适配器负责传回供应商。

render_history 只是基础角色投影，不含全局 prompt、tools 定义或完整模型参数，
不能将它误当作已经准备完毕的 API 请求。单次网络调用、原生工具响应解析和完整请求构建在下一批。
会话保留工具读取记录；没有另建引用范围账本、资源注册库或执行器。

## 本地验证

在仓库根目录执行：

```powershell
Set-Location backend
$env:PYTHONUTF8 = '1'
..\.venv\Scripts\python.exe -m examples.agent_session
..\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_agent*.py' -v
```

示例手动模拟两次模型响应及一次读取结果，不请求模型、不读取文件。
它展示一个用户轮次内的“模型请求读取 → 外部回传 → 模型回答”以及最终原生消息结构。

2026-11-05 本地验证：新增 24 项状态/存储测试全部通过；Agent 相关共 30 项通过。
完整后端回归共 333 项，191 项通过，142 项需显式启用的 PostgreSQL/真实沙箱用例跳过。
Agent 模块行覆盖率 93.2%；Black、Ruff、Python 编译、差异空白及密钥扫描均通过。
示例已运行，本批未调用真实模型或沙箱，未触发远程 CI；用户确认后仅提交到本地分支，未推送。

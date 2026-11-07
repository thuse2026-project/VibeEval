# 第三批：显式压缩与会话恢复

状态：2026-11-05 用户已确认，作为第三批检查点提交。前两批已分别提交为 ac80abc、abd1a1e，均未推送。
本批继续参考固定版本的 Codex 源码，改写为项目内 Python 实现，来源见 backend/app/agent/UPSTREAM.md。

后续接入分支在 [第三批 A](agent-integration-stage3a.md) 扩展了 ready Turn 内的完整工具交换边界、
窗口投影和预算入口；下文保留原批次验收时的空闲压缩范围，最新调用约束以扩展说明为准。

## 会话保留什么，模型收到什么

ConversationState.turns 保存完整用户消息、助手响应、工具调用与工具结果。
新增 compactions 保存已采用的压缩检查点；不删除或改写被压缩的原始 Turn。

- `render_history(state)`：完整历史投影，供外层展示或读取原会话。
- `render_context(state)`：最后一次摘要前缀，加上覆盖范围之后的完整 Turn；未压缩时等同完整历史。
- `context_info(state)`：检查点代数、覆盖/保留轮数、历史与窗口的消息数和序列化字符数。
- `prepare_model_request(state, ...)`：自动使用当前窗口，继续携带全局 prompt、可信会话说明、工具 schema。

摘要作为标明历史材料的 user 消息加入模型窗口，不提升为 system 指令。
最近轮次的原生工具配对与供应商续调数据保持原样；被压缩部分的续调数据保留在原历史，
不发送给摘要模型，也不混进摘要。需要旧正文时，由外层读取接口返回原记录。

## 三步压缩接口

| 接口 | 返回值 | 作用 |
| --- | --- | --- |
| `prepare_compaction(state, through_turn=N, ...)` | PreparedCompaction | 选择前 N 个已结束轮次，生成可检查且不含 Key 的请求；不调用模型 |
| `await call_compaction(prepared, client=...)` | CompactionResult | 一次摘要模型请求，返回摘要、模型、结束原因、用量；不修改会话 |
| `apply_compaction(state, result)` | 新 ConversationState | 检查归属、源版本和摘要，显式采用检查点；不调用模型 |

through_turn 是从开头计数的轮数，不是从零开始的下标。
本批只允许会话空闲时准备和采用压缩；completed、failed、interrupted 均属于已结束轮次。
有活动 Turn、模型调用或待回传工具时均拒绝，避免切开调用/结果链。

PreparedCompaction 记录源会话 ID、scope、revision、完整源快照摘要、覆盖边界和原始轮次摘要，
以及 prompt 版本、模型参数、实际 payload_json 和 SHA-256。
它是可信调用方的检查与关联产物，不是鉴权凭证；外层仍须验证访问范围。

```python
from app.agent import (
    prepare_compaction, call_compaction, apply_compaction,
    context_info, restore_snapshot,
)

# state 是已结束当前 Turn 的服务端会话；settings/client 由外层注入。
prepared = prepare_compaction(state, through_turn=2, settings=settings, max_tokens=512)
print(prepared.payload)             # 发送前查看材料和参数
result = await call_compaction(prepared, client=client)
print(result.response.message.content)  # 采用前查看摘要

updated = apply_compaction(state, result)
store.save(updated, expected_revision=state.revision)
print(context_info(updated))

snapshot_json = updated.model_dump_json()
restored = restore_snapshot(snapshot_json, scope_id=trusted_scope)
# begin_turn -> start_step -> prepare_model_request -> call_model -> accept_response
# 后续请求由 restored 自动生成摘要 + 新近历史，不需前端重新拼 messages。
```

采用后 revision 增加 1。摘要生成期间会话发生任何变化，采用旧结果会报 SessionConflict；
重复采用同一结果幂等，不重复累计用量；同一请求 ID 的不同结果会被拒绝。
使用共享存储时须按 expected_revision 保存；跨进程竞争仍由外层存储的 CAS 负责。

## 连续压缩、失败与用量

后续压缩输入为“上次摘要 + 新覆盖的完整轮次”，不会反复发送全部旧正文。
允许保持相同边界再次精简已有摘要，不允许覆盖边界向后退。
检查点记录父 ID 与代数，快照恢复时校验顺序、边界、原始轮次摘要及已结束状态。
已有不含 compactions 的版本 1 快照仍可读取；正在运行的尾部 Turn 继续按第一批规则中断，
不会自动重新调用模型或执行工具。

采用前要求新窗口的序列化字符数严格减少；否则保留旧窗口并返回 SessionError。
字符数是便于检查的尺寸指标，不是精确 token 数；不保证摘要无信息损失，也不承诺固定压缩率。
不引入自动触发阈值、模型 tokenizer、隐式裁剪或重试。
单次准备产物沿用 64000 字符上限；材料过大时外层可选择更小的轮次前缀分批压缩。
摘要限制 16000 字符，输出 token 上限复用服务端配置。

网络失败、格式错误、空摘要、工具输出、过滤或截断均不采用摘要；失败返回脱敏错误。
`state.compaction_usage` 汇总已采用检查点的实际用量；`state.usage` 汇总已接受响应和已采用压缩。
缺失计数继续为 None。未采用的结果在 CompactionResult 中仍带用量；失败请求或未采用调用的账单，
需外层另行记录，state.usage 不是供应商总账单。

## 可运行示例

从 backend 目录执行：

```powershell
$env:PYTHONUTF8 = '1'
..\.venv\Scripts\python.exe -m examples.agent_compaction
..\.venv\Scripts\python.exe -m examples.agent_compaction --preview
..\.venv\Scripts\python.exe -m examples.agent_compaction --live --model <模型ID>
```

默认模拟 HTTP，不读配置、不访问网络。--preview 只打印请求准备产物。
--live 使用私有 .env，最多发送两次真实 API 请求：摘要一次、快照恢复后的问答一次。
历史为示例内固定材料，检查恢复后保留原历史，继续问答能结合旧摘要与最近缩进规则。
本批没有新增 HTTP 路由、业务数据库绑定、文件读取工具或执行器。

## 验证记录

2026-11-05 使用 cohere/north-mini-code:free 完成真实摘要与恢复后问答，共两次 API 调用。
固定示例的模型窗口从 4518 字符降至 442 字符，完整历史一致；恢复后正确回答了 Python、
src/calc.py、square 的作用，以及未压缩最近轮次中的四空格规则。
摘要请求输入 2736、输出 418、合计 3154 token；恢复后问答输入 363、输出 149、合计 512 token。
这是一次实际运行记录，不代表固定压缩率或未来模型可用性。

默认模拟示例也通过，窗口从 4518 字符降至 319 字符，未调用外网。
新增 14 项压缩测试通过，Agent 相关共 59 项通过。
完整后端回归 362 项，220 项通过，142 项数据库/真实沙箱用例按配置跳过。
Agent 与模型模块合计行覆盖率 92.3%；Black、Ruff、Python 编译、请求预览校验、
差异空白与密钥扫描通过。本批经用户确认提交到本地分支，未推送，也未执行远程 CI。

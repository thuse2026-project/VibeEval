# Agent API 原子能力：分批交付

本分支 `feat/llm-api` 专注模型 API 的输入准备、调用和结果暴露。
每批完成后等待用户检查，确认后再提交并继续下一批。

最新完整范围和源码调研依据见 [Agent API 会话边界与实现范围](agent-session-design.md)。
用户视图和模型消息序列从同一会话记录派生，读取通过原生工具请求/结果衔接。
文本调用之外，第一批 [本地会话状态](agent-session-stage1.md) 已确认提交；
第二批 [原生模型调用](agent-session-stage2.md) 已确认提交，提供工具声明、全局 prompt、请求预览和单次调用；
第三批 [上下文压缩与恢复](agent-session-stage3.md) 已确认提交，提供显式压缩、检查点和快照续调；
第四批 [组员接入契约与示例](agent-session-stage4.md) 已确认提交，作为组员接入的首个入口。
自定义引用仓库和单次 JSON 协议已移至本地草稿备份。

## 分支边界

本库接收调用方提供的历史、摘要和代码材料，将准备好的输入和模型输出作为返回值暴露。
返回命令、补丁或工具请求时，由外层决定是否批准、执行、写入文件，以及是否将结果交回模型。
本分支不实现执行器、文件写入、批准流程、工作台接入或自主工具循环。
完整原始供应商响应不直接透传，仍需过滤密钥、内部调试字段并统一错误。

保留四项设计原则，并按 API 库的职责收窄：

1. 会话记录与当前模型窗口分开维护，读取过程保存在工具请求/结果中。
2. 读取参数和返回内容按具体工具 schema 定义，先不设计独立的统一引用仓库或范围账本。
3. 纯对话不提供工具；有工具时返回原生工具请求，由外层处理并回传结果。
4. 会话由小函数推进，可序列化并恢复；实际存储和工具处理采用注入接口。

## 当前实现：准备输入与纯文本调用

| Python API | 输入与输出 | 行为 |
| --- | --- | --- |
| `app.agent.prepare_chat()` | 问题、显式历史、系统提示和可选模型参数 → `ChatInput` | 纯输入整理，可检查并序列化；不加载配置、不访问网络 |
| `LLMClient.chat()` | `ChatInput` → `ChatResult` | 发送一次非流式模型请求，返回文本、实际模型、结束原因和用量 |
| `app.agent.chat_only()` | 同 `prepare_chat()`，可注入客户端 → `ChatResult` | 组合前两步的便捷入口，进行一次文本调用 |

先查看准备结果，再显式发送：

```python
from app.agent import prepare_chat
from app.llm.client import LLMClient, Message
from app.llm.config import load_settings

request = prepare_chat(
    "结合上面的讨论，解释这个函数。",
    history=[
        Message(role="user", content="def square(x):\n    return x * x\n"),
        Message(role="assistant", content="这是一个返回平方值的函数。"),
    ],
    system_prompt="帮助理解代码，回答简洁。",
    max_tokens=200,
)
print(request.model_dump_json(indent=2))

# 由外层决定何时发送；在异步业务函数中调用。
result = await LLMClient(load_settings()).chat(request)
print(result.content)
```

准备结果包括消息和显式模型参数，不含鉴权信息。
未指定的 `model/max_tokens/temperature` 保持为空，模型及输出上限在发送时按配置解析，
`temperature` 未指定则不发送；因此它是逻辑输入预览，不是带鉴权头的完整 HTTP 请求。
历史先复制，顺序为“可选系统提示 → 历史 → 本轮问题”，代码缩进和换行保留。
当前历史需已由调用方选择并授权；具体来源通过会话工具接口读取，不应把不可信材料直接提升为系统提示。
模型返回的命令、JSON、补丁都只作为文本返回。参数限制见 [模型 API](llm-api.md)。

## 会话接口（完整契约见各批说明）

| 能力 | 暴露的结果 |
| --- | --- |
| 会话与轮次状态 | 历史、当前轮次、待回传调用和明确的结束状态；见第一批 |
| 模型请求准备 | 当前窗口、tools、模型参数和版本信息，允许发送前查看；见第二批 |
| 原生工具请求与结果 | 调用 ID、名称、参数及配对结果，不执行工具；见第二批 |
| 上下文压缩与恢复 | 压缩后的窗口、原始历史、可恢复检查点及用量；见第三批 |

每批只实现已确认的一小部分。上下文压缩本身也是模型调用，需要计入用量；
完整历史用于保存与查询，实际模型输入由显式选择的工作上下文决定。

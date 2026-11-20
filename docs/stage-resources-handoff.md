# 阶段资源适配交接

已实现阶段级 Agent、文件、终端和沙箱接入。`StageResources` 实现考试流程的 `ResourceGateway`，复用 Agent 原生运行协议、runsc 命令 helper、原子文件保存和内容寻址快照。题目和镜像由题目方提供，前端可调用阶段路由。

## 资源归属

| 对象 | 归属与隔离方式 |
| --- | --- |
| `StageWorkspace` | 每阶段一个工作区，绑定考生、attempt、stage、镜像、发布清单、Docker 名称和容器 ID |
| `StageFileRevision` | 使用 `workspace_id + path`，同路径在第三、四阶段有独立版本 |
| `AgentScope` | 每环节一个作用域，隔离对话、材料、引用、运行和执行回执 |

第一阶段探索和答题可以共用阶段文件卷，切换时移除旧容器及其进程，再建立新的进程环境。第三、四阶段使用不同卷，不继承文件、终端进程或对话。

迁移 `0012` 将现有 Agent 表的归属外键接到 `agent_scopes`。原字段名 `exam_session_id` 为兼容旧协议保留：旧数据的值仍是入场会话 ID，新阶段数据的值是环节 ID。旧记录自动回填，新入场记录由数据库触发器建立 legacy scope，不改写旧 Agent 快照。

## 可信资源注册

服务端将 `StageDefinition.resource_ref` 映射到 `ResourceSpec`，包含：

- `task_version`，必须与考试冻结版本一致。
- `image_ref`，本机已有的不可变镜像 ID 或仓库 digest，不接受可变 tag，不自动拉取镜像。
- `files`，允许读取和提交的 UTF-8 文件清单。
- `writable`，清单中可修改的已有文件。
- `agent_policy`，服务端 Agent 策略，默认需要批准操作。

候选人接口不能指定镜像、Docker 地址、宿主路径或更换清单。文件及父目录禁止符号链接，父目录由 root 持有且不可由考生写入；仅开放清单内已有文件，不支持任意创建、删除或重命名文件。

文件读取、目录查询、引用和命令的 `expected_versions` 都使用本阶段注册清单，不依赖旧样例文件名。单文件上限 1 MiB，默认快照源文件总量上限 10 MiB，Docker 导出归档上限 16 MiB。

只读阶段使用只读卷。此适配器的任意 shell 终端要求同时具备 `use_terminal` 和 `write_repository`，不能以执行任意 shell 来承诺只读。镜像需要 Python、sleep，启用终端时还需 bash；依赖应预装，候选容器不联网。

## 准备和实际撤权

准备时先提交资源计划，再分配固定名称的卷和 runsc 容器。重试只认领符合镜像、标签、挂载和隔离设置的同一资源。容器使用 UID 1000、只读根文件系统、禁网、移除 capabilities、禁止提权和资源限制，不回落到 runc。

关闭顺序如下：

1. 考试协调器先提交关闭状态和新权限版本，拒绝后续操作。
2. 资源层关闭环节 Agent scope，暂停原工作区。
3. 停止并移除绑定的命令 helper 和文件 writer。
4. 强制移除原容器，结束包括 PTY 和后台子进程在内的旧进程环境；文件卷保留。
5. 取消未结束的 Agent Run，释放已确认停止的工作区占用。

长模型调用期间每 250 毫秒检查共享数据库授权；撤权后取消客户端请求，保留未知用量或未确认结果，不自动重发。文件操作、终端输入与尺寸调整、Agent 接口和工具调用都检查当前环节及截止时间。终端输出和阶段 SSE 每条事件也检查权限，避免撤权后继续发送缓冲材料。

## 冻结和快照

代码阶段在旧容器、writer 和命令 helper 停止后封存。导出使用单独的 runsc 容器只读挂载原卷，只执行固定平台操作，收集发布清单中的普通 UTF-8 文件，忽略构建输出。

结果写入现有 `SnapshotStore`，校验摘要并持久化产物元数据后返回 `Artifact`。重试返回原产物，失败保持关闭，不恢复编辑或进入下一阶段。快照与后续工作区变化分离。

命名文件卷默认保留用于恢复和备份，其归档清理由宿主运维策略处理。本模块移除运行容器和导出容器，不在失败恢复期间删除答卷卷。

## HTTP 和 WebSocket

先按 [考试流程接口](multi-stage-exam-plan.md) 兑换 v2 Cookie，HTTP 继续要求 `X-VibeEval-Request: 1`。

| 路径 | 用途 |
| --- | --- |
| `GET /api/v2/phases/{phase_id}/files` | 文件树、只读标记和持久版本 |
| `GET /api/v2/phases/{phase_id}/file?path=...` | 读取清单内文件和版本 |
| `PUT /api/v2/phases/{phase_id}/file` | 保存 `path`、`expected_version`、`content`；冲突返回当前版本 |
| `WS /api/v2/phases/{phase_id}/terminal` | 当前阶段 Docker exec PTY |
| `/api/v2/phases/{phase_id}/agent/...` | 原生对话、引用、运行、批准、材料、上下文和事件接口 |

Agent 子路径与 [基本 Agent 交接](agent-integration-handoff.md) 相同，替换前缀即可。为复用原协议，Agent 路由在 OpenAPI 中的参数仍名为 `session_id`，这里传入环节 ID，不能传入场会话 ID。v1 Agent 仍拒绝多阶段会话。

WebSocket 使用 v2 Cookie 校验归属，使用 `SANDBOX_TERMINAL_ORIGINS` 精确校验 Origin。消息沿用 `input` 和 `resize`，响应为 `output`。环节关闭后关闭连接，不重放旧输入。

## 服务端装配

```python
from examples.exam_flow import build_stage_app, attach_definition, maintain
from app.stage_resources import ResourceSpec

specs = {
    "registered-task": ResourceSpec(
        task_version="task-v1",
        image_ref=verified_local_image_id,
        files=published_paths,
        writable=published_writable_paths,
    )
}
app = build_stage_app(
    question_provider, specs,
    factory=shared_postgres_factory,
    settings=server_llm_settings,
)
flow = app.state.exam_flow
attach_definition(flow, admitted_session_id, "exam-definition.json")
maintain(flow, stop_event)  # 在宿主监督的维护进程中运行
```

先执行 Alembic `upgrade head` 至 `0012`，配置 `SANDBOX_BACKEND_REF` 和本地 Unix Docker socket。运行环境需为已注册 runsc 的 Linux。模型密钥仅进入服务端设置，不注入候选容器。需要 Agent 的环节在配置不完整时不会假装激活成功。

## 验证范围

专项使用真实 PostgreSQL、原生 Agent 协议及 HTTP／WebSocket，替换模型网络传输和 Docker I/O。覆盖工作区隔离、文件版本、跨阶段对话隔离、原生单文件修改、活动命令和模型请求取消、持续终端撤权、导出重试与快照校验。Docker 边界测试验证归属、挂载、停止顺序和文件校验。

完整后端回归 619 项中 604 项通过、15 项真实沙箱用例按环境跳过，整体覆盖率 87.3%；其中阶段资源专项 18 项全部通过。Black、Ruff 检查通过。

按用户要求，本轮暂不进行服务器真实 gVisor 验收。这些结果不能替代 Linux/runsc 上的进程停止、PTY、卷挂载与冻结导出联合验收；不宣称已部署或完成真实模型与 gVisor 联测。

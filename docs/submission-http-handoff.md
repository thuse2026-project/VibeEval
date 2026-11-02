# 交卷 HTTP 接入与沙箱交接

本阶段基于陈智恒 `feat/entry-postgres`（`83075d9`）接入提交路由，复用共享 PostgreSQL、入场 Cookie、会话归属检查和既有交卷协调器。不修改共享模型或迁移，仍使用 `0004`。默认管理端和提交写入使用相同的数据库工厂及快照存储配置。

## 候选人调用

接口为 `POST /api/v1/sessions/{session_id}/submit`。原浏览器携带 HttpOnly Cookie，并设置 `X-VibeEval-Request: 1`、`Idempotency-Key: <UUID>`；不需要 JSON 请求体。前端应先完成交卷二次确认，检查保存状态，提交期间禁止重复操作；409 `SESSION_BUSY` 时按 `Retry-After: 1` 延迟并有界重试。

202 响应包含 `submission_id`、`session_id`、`snapshot_id`、`submitted_at`、`status: "frozen"`，表示快照与数据库最终答卷均已保存。相同会话重试始终返回第一份已提交答卷，包括响应丢失后更换幂等键的重试。客户端可通过现有 status 路由读取状态，再次 POST 获取原回执。所有响应禁止缓存，完整状态码及错误体见 [API 约定](api.md#主动交卷-http已实现)。

本阶段没有改候选人工作台、环境准备或开始考试界面，也没有新增开始考试 HTTP 接口。适配器接入后，仍需由沙箱与工作台按既有生命周期完成准备、绑定和确认开考，再调用交卷。入场状态 `admitted` 不能直接交卷。

## 需要陈弘毅提供的内容

请提供可拉取的代码分支或 PR 链接，包含现有创建、恢复、状态、工作区与 PTY 实现，以及启动/配置/测试说明。提交侧需要在该模块上实现 `SubmissionSandbox` 的两个方法：

```python
from app.db.lifecycle import SessionView
from app.submissions.coordinator import FrozenWorkspace, SubmissionSandbox
from app.submissions.storage import SnapshotFile

# 适配器实现上述协议；以下是方法签名，不是可运行的空适配器。
# freeze_and_export(view: SessionView) -> FrozenWorkspace
# delete_sandbox(view: SessionView) -> None
```

- `freeze_and_export`：按已记录的沙箱 ID/宿主重连，核验归属和镜像，禁止文件/PTY 新写入，等待在途保存，暂停全部进程后导出稳定的相对 POSIX 路径与 UTF-8 文本。返回 `FrozenWorkspace(view.session_id, view.sandbox_id, files, public_test_output)`，其中 `files` 是 `SnapshotFile(path, content)` 可迭代对象。没有已有公开测试输出时传 `None`；冻结后不另行启动测试。导出在持久化完成前须保持可读且稳定。
- `delete_sandbox`：提交侧确认答卷入库后才调用；仅删除原容器，保留考生文件。已不存在的容器视为成功。删除失败抛异常，不以 `False` 或错误字符串表示失败。
- 两个方法均同步、有界超时、可重试；异常时保留环境及文件，不自动恢复编辑。会话状态、快照/提交入库与清理重试由提交侧负责，不另建会话表。方法在共享会话行锁内调用，不能另起事务修改或重新锁同一行。
- 运行说明需包含 Docker/gVisor 配置要求、不可变题目镜像引用、环境变量示例、启动及最小验证命令。凭据和真实考生文件不入库。

详细创建去重、版本、写入门禁和事务要求见 [沙箱接入约定](sandbox-integration.md)。目前这些要求是接入契约，尚未对陈弘毅的真实实现验证。

## 应用装配

已接入成员 C 的 `SandboxRuntime`，可使用 `python -m uvicorn app.sandbox.http_app:create_app --factory` 显式装配。环境、镜像构建与普通/真实测试的区别见 [联合验收说明](sandbox-submission-validation.md)。下述注入接口继续支持可信后端自行构造适配器；文件 HTTP、PTY 和正式工作台仍需后续接入。

`create_app` 新增两个只用于可信后端代码的关键字参数：

```python
from app.main import create_app

# sandbox_adapter 由沙箱模块真实实现并从可信部署配置初始化。
app = create_app(submission_sandbox=sandbox_adapter)
```

默认 `uvicorn app.main:app` 不装配沙箱：未提交会话收到安全的 503，原状态保持不变，不用测试替身或空操作模拟成功。已有最终答卷的合法请求仍可获得原回执。实际适配器交付后，由应用装配入口构造适配器并使用上面的调用方式，不向浏览器开放适配器配置。

可选 `snapshot_store=SnapshotStore(root=...)` 控制持久存储位置和限制；交卷和默认管理端 reader 共用该配置，不能各自指向不同目录。测试使用独立临时目录；生产须使用后端独占的持久存储。数据库工厂统一从已鉴权的 `app.state.entry.factory` 获取，显式 SQLite 旧回归模式不能执行真实交卷。

## 验证边界

从 `backend` 目录运行：

```powershell
$env:PYTHONUTF8 = "1"
$env:VIBEEVAL_RUN_DB_TESTS = "1"
..\.venv\Scripts\python.exe -m unittest discover -s tests -p test_submission_http.py -v
```

测试会创建并清理随机 PostgreSQL 数据库，覆盖真实 Token 入场与 Cookie、会话归属、幂等回执、并发锁、冻结/快照/最终事务失败、清理失败及考官调阅。沙箱替身仅存在测试文件内；这些测试不代表完成真实 gVisor 暂停、文件权限、候选人交互或整个平台端到端验收。

本阶段新增 20 项 HTTP 测试；完整回归在 Windows / Python 3.13 与 Linux 容器 / Python 3.12 均为 181 项全部通过，覆盖率分别为 96.3% 和 96.5%。Black、Ruff 通过，Linux 的 Alembic 升级、当前版本及模型一致性检查通过（`0004`，无新增迁移）。这些是本地验证，GitHub CI 以提交推送后的实际结果为准。

真实适配器已通过显式工厂装配，联合验证记录见新说明；到期提交、进程中断恢复及清理重试现有独立命令，见 [提交定期处理](submission-maintenance.md)。生产宿主部署和前端提交交互仍待后续阶段。没有新增候选人可调用的清理、删除、任意文件导出或快照写入 HTTP 接口。

# 沙箱与共享数据库接入约定

本说明对应数据库迁移 `0003`。共享数据库层和提交存储已实现；Docker + gVisor 生命周期代码由陈弘毅提供，当前尚未接入其真实代码，也未完成沙箱冻结或 HTTP 交卷的端到端联调。以下 Python 接口均为可信后端内部接口，调用前仍须校验浏览器凭据和会话归属，`session_id` 不充当凭证。

## 1. 会话、沙箱和截止时间

共用 `backend/app/db/models.py` 中的 `ExamSession`，数据库表为 `exam_sessions`。不再另建一套会话模型。对接入口是 `backend/app/db/lifecycle.py` 的 `SessionLifecycle`：

| 字段 | 含义 |
| --- | --- |
| `sandbox_id` | 已绑定的真实容器/沙箱 ID；绑定后不自动替换，清理后仍保留历史 ID |
| `sandbox_resource_key` | 提前持久化的固定资源名/去重标识；创建或恢复时必须使用 |
| `sandbox_image_ref` | 固定的不可变镜像引用，支持本地 `sha256:<64位摘要>` 或 `仓库@sha256:<64位摘要>` |
| `sandbox_backend_ref` | 稳定的沙箱引擎/宿主配置标识，如 `docker-gvisor:exam-node-01`；不存连接密码 |
| `started_at`、`deadline_at` | 服务端开始/截止时间，UTC Unix 秒；就绪未开考时都为 `None` |
| `sandbox_deleted_at` | 提交确认成功后，沙箱清理完成的时间；保留映射和镜像版本 |
| `lifecycle_error_code`、`lifecycle_error_at` | 安全的错误代码和发生时间，不存原始异常、凭据或内部路径 |

只读查询：`lifecycle.get_session(session_id)` 返回不可变 `SessionView`，未知会话返回 `None`。后端重启后仍从 PostgreSQL 读取这些字段。

`prepare_sandbox(session_id, image_ref, backend_ref=...)` 在创建容器前提交固定计划，返回的 `sandbox_resource_key`、镜像和宿主之后不变。仅有浮动 tag 时，先由沙箱模块解析成不可变镜像引用；直接传 `python:latest` 等 tag 会被拒绝。本地 Docker 可使用已解析的镜像 ID；使用仓库 digest 时，需由适配器按相同标识验证实际镜像。

环境健康检查通过后调用 `locked.mark_ready()`。考生确认开始时调用 `lifecycle.start_exam(session_id)`，它使用邀请记录的 `duration_minutes` 一次性设置开始/截止时间。重复调用和重连不会重新计时。时间到了之后禁止写入；自动交卷调度还需由提交侧后续接入，沙箱侧不要仅按截止时间先行删除容器或工作区。

## 2. 同一会话的并发控制与创建恢复

统一使用 PostgreSQL `SELECT ... FOR UPDATE NOWAIT` 行锁，入口为 `with lifecycle.lock_session(session_id) as locked:`。锁覆盖整个上下文事务，适用于不同后端进程；提交、回滚或数据库连接结束时释放。竞争者收到 `SessionBusy`，应轮询当前状态或有界重试，不在未拿到锁时调用 Docker。底层锁语义见 [PostgreSQL 文档](https://www.postgresql.org/docs/16/explicit-locking.html#LOCKING-ROWS)。

创建/连接/暂停/删除等生命周期操作均应在拿到锁后重新读取 `locked.view`，并在同一上下文内执行。不要把数据库方法分别锁住，却把真正的容器创建放在锁外。锁期间应给沙箱操作设置超时，不等待前端确认或用户输入；也不要手工提交、回滚内部事务或把 `locked` 缓存在上下文之外。

数据库事务不能使外部 Docker 创建天然具有 exactly-once 语义。为覆盖“容器创建成功、数据库绑定尚未提交就崩溃”，沙箱侧还必须：

1. 在固定宿主上使用 `sandbox_resource_key` 作为原子唯一资源名，并关联会话归属标签；多个后端进程指向同一个已记录的引擎/宿主。
2. `sandbox_id` 已存在时按 ID 重连并核验归属和镜像，找不到时明确报错，不能退回创建空环境。
3. `sandbox_id` 为空时，先按固定资源名/标签查找并核验已有容器；只有确实没有时才创建。遇到名称冲突再次查找，不能换随机名称绕过去。
4. 将核验后的容器 ID 传给 `locked.bind_sandbox(id, image_ref=...)`；不同 ID、镜像或宿主的覆盖会被拒绝。

调用结构示意（`adapter.*` 代表沙箱侧待对齐的方法，不是本仓库已实现的 Docker API）：

```python
from app.db.lifecycle import SessionLifecycle

lifecycle = SessionLifecycle()
plan = lifecycle.prepare_sandbox(session_id, resolved_image_ref,
                                 backend_ref="docker-gvisor:exam-node-01")

with lifecycle.lock_session(session_id) as locked:
    view = locked.view
    if view.sandbox_id is not None:
        sandbox = adapter.connect_existing(view.sandbox_id)  # 失联时报错，不创建空环境
    else:
        sandbox = adapter.find_by_resource_key(view.sandbox_resource_key)
        if sandbox is None:
            sandbox = adapter.create_unique(
                resource_key=view.sandbox_resource_key,
                image_ref=view.sandbox_image_ref,
                session_id=view.session_id,
            )
    adapter.verify_identity_and_image(sandbox, view)
    locked.bind_sandbox(sandbox.id, image_ref=view.sandbox_image_ref)
    if view.status == "provisioning":
        locked.mark_ready()
# 成功退出上下文后，绑定才已提交。
```

文件保存和终端输入使用 `with lifecycle.write_guard(session_id) as view:`，在上下文内执行本次写入或输入转发。只有已开始、未到期、未提交且绑定有效的会话可写。开始交卷与进行中的保存竞争同一行锁：有保存正在执行时，`begin_submission` 返回 `SessionBusy`，提交侧待其结束后重试；一旦交卷状态提交，新写入会被拒绝。长时间运行的后台进程仍须由沙箱侧暂停整个环境，数据库写入门禁不能替代暂停。

## 3. 状态名称

统一枚举在 `backend/app/db/states.py` 的 `SessionStatus`，数据库也限制为以下集合：

| 状态 | 含义 | 考生写入 |
| --- | --- | --- |
| `admitted` | 已入场，尚未准备环境 | 禁止 |
| `provisioning` | 创建或恢复环境中 | 禁止 |
| `ready` | 环境就绪，等待考生确认开始 | 禁止 |
| `in_progress` | 正在作答；还需检查服务端截止时间 | 未到期时允许 |
| `provision_failed` | 准备/连接失败，保留原映射与已有文件 | 禁止 |
| `submitting` | 正在交卷，等待冻结、导出或持久化 | 禁止 |
| `submission_failed` | 交卷失败，保留暂停环境供重试 | 禁止 |
| `submitted` | 最终答卷已持久化；沙箱可能仍待清理 | 禁止 |

准备失败用 `locked.mark_provision_failed("SANDBOX_CREATE_FAILED")` 记录。重试相同计划后，恢复已有 ID 或固定资源名，不能换镜像、换宿主或重新给一份空题目。已开考会话恢复健康后回到 `in_progress`，保留原截止时间；首次准备就绪则进入 `ready`。

## 4. 交卷顺序与事务边界

由赵晨一的提交侧协调重试与后续调度，沙箱侧提供禁止写入、真正暂停和稳定导出能力。顺序如下：

1. 提交入口先校验考生归属，并通过 `SubmissionService.get_session_receipt(session_id)` 检查是否已有已提交回执；有则直接返回。
2. 调用 `lifecycle.begin_submission(session_id)`，成功返回意味着 `submitting` 已在数据库提交，新写入从此被拒绝。进行中的保存未结束时会拿不到锁，提交侧有界重试。
3. 重新进入 `lock_session`，再次核对状态。沙箱侧关闭写入入口、等待在途保存，暂停整个考生环境（包括后台进程），再导出稳定工作区。暂停和导出必须支持重试；不能用删除容器代替冻结。
4. 在这个锁上下文里调用 `locked.persist_frozen_submission(submissions, key, files, public_test_output=...)`。它复用当前数据库事务，避免另外一个连接等待自己持有的行锁。快照元数据、最终提交和会话 `submitted` 状态在同一事务中提交。
5. 成功退出锁上下文后才能向调用方回成功。块内拿到的 `receipt` 尚不能当作已提交回执，更不能在块内删除沙箱。
6. 再开一个锁上下文，调用 `locked.require_cleanup()`，核对已提交记录和原容器 ID后才通知沙箱侧删除容器；按双方约定保留考生文件，不删除数据库答卷和快照。成功后调用 `locked.mark_sandbox_deleted(original_id)`。

不要在持有 `lock_session` 时调用普通 `SubmissionService.persist_frozen_submission()`，它会另开事务锁同一会话；必须使用 `locked.persist_frozen_submission()`。底层 `persist_in_transaction()` 供内部复用，事务所有者负责提交后才响应。

## 5. 失败、重试与清理归属

- 暂停/导出/快照/数据库失败：沙箱侧保留环境和文件，不自动恢复编辑或删除容器。错误处理应在失败的锁上下文退出、事务回滚之后执行。
- 若提交结果不确定，先查询 `get_session_receipt`；已存在回执就按成功处理。没有时另开锁调用 `mark_submission_failed("SNAPSHOT_WRITE_FAILED")` 等安全错误代码，保持禁止编辑，不因重试重置截止时间。
- 由提交侧发起交卷重试，重新 `begin_submission`，复用同一暂停环境、容器 ID、固定资源标识及首份最终答卷语义；沙箱侧的暂停/导出须幂等。当前尚未新增自动重试 worker 或真实 HTTP 提交入口。
- 快照已落盘而数据库失败时，可能保留未引用文件；它不会被当作成功答卷，也不会触发沙箱清理。该类文件的垃圾回收仍未实现。
- 只有确认数据库提交成功后，提交侧才发起清理通知；本阶段提供待清理台账 `list_cleanup_candidates(limit=...)`，尚未启动后台清理 worker。沙箱侧执行删除，保留工作区文件；容器已不存在时应按幂等成功处理。
- 清理失败调用 `locked.mark_cleanup_failed("SANDBOX_CLEANUP_FAILED")`，会话仍是 `submitted`，答卷成功不撤销。待清理记录继续可查；后续清理必须再次拿锁、核对原 ID。清理后保留历史映射，不创建新空环境。

## 6. 仍需沙箱侧提供的接入信息

需要陈弘毅提供代码分支或方法签名，确认固定资源名/标签查找、不可变镜像解析、归属验证、真正的 pause 与稳定工作区导出接口。已有创建、状态、停止、删除能力不会自动等于已经完成冻结导出。上述适配器调用仍为接口示意，当前测试使用替身；真实 Docker + gVisor 联调、截止自动交卷和后台清理在后续阶段完成。

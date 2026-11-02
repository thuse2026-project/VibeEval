# 沙箱与共享数据库接入约定

本说明对应数据库迁移 `0004`。共享数据库层、提交存储、`SubmissionCoordinator` 交卷协调器及候选人交卷 HTTP 路由已实现；Docker + gVisor 生命周期代码由陈弘毅提供，当前尚未接入其真实代码，未完成真实沙箱端到端联调。以下 Python 接口均为可信后端内部接口，调用前仍须校验浏览器凭据和会话归属，`session_id` 不充当凭证。HTTP 鉴权、错误码和应用注入见 [交卷 HTTP 接入](submission-http-handoff.md)。

## 1. 会话、沙箱和截止时间

共用 `backend/app/db/models.py` 中的 `ExamSession`，数据库表为 `exam_sessions`。不再另建一套会话模型。对接入口是 `backend/app/db/lifecycle.py` 的 `SessionLifecycle`：

| 字段 | 含义 |
| --- | --- |
| `sandbox_id` | 已绑定的真实容器/沙箱 ID；绑定后不自动替换，清理后仍保留历史 ID |
| `task_version_id` | 逻辑题目发布版本，如 `python-debug-01:v1`；与运行环境的镜像版本分开记录 |
| `sandbox_resource_key` | 提前持久化的固定资源名/去重标识；创建或恢复时必须使用 |
| `sandbox_image_ref` | 固定的不可变镜像引用，支持本地 `sha256:<64位摘要>` 或 `仓库@sha256:<64位摘要>` |
| `sandbox_backend_ref` | 稳定的沙箱引擎/宿主配置标识，如 `docker-gvisor:exam-node-01`；不存连接密码 |
| `started_at`、`deadline_at` | 服务端开始/截止时间，UTC Unix 秒；就绪未开考时都为 `None` |
| `sandbox_deleted_at` | 提交确认成功后，沙箱清理完成的时间；保留映射和镜像版本 |
| `lifecycle_error_code`、`lifecycle_error_at` | 安全的错误代码和发生时间，不存原始异常、凭据或内部路径 |

只读查询：`lifecycle.get_session(session_id)` 返回不可变 `SessionView`，未知会话返回 `None`。后端重启后仍从 PostgreSQL 读取这些字段。

新接入在 `prepare_sandbox(..., task_version_id="python-debug-01:v1")` 中传入题目配置里的确切发布版本，也可在拿锁后调用 `locked.pin_task_version(...)`。发布版本 ID 由题目维护方定义并保持不可变，本阶段保存引用，不新增题库管理平台。版本一旦记录便不能更换；未知旧记录保留 `None`，仅允许在开考前登记经确认的实际版本，禁止开考后把“当前版本”猜作历史版本。旧调用可不传此参数以兼容既有接口，新场次应显式传入。提交时 `Submission` 同时保存题目版本和镜像引用副本，方便后续追溯；现有管理端 `SubmissionReader` 返回字段保持不变。

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
                                 backend_ref="docker-gvisor:exam-node-01",
                                 task_version_id="python-debug-01:v1")

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

统一入口在 `backend/app/submissions/coordinator.py`，由调用方注入实现 `SubmissionSandbox` 协议的真实适配器，没有默认的空冻结实现：

```python
from app.submissions.coordinator import SubmissionCoordinator

# sandbox_adapter 由沙箱模块实现；调用前须已校验浏览器凭据和会话归属。
coordinator = SubmissionCoordinator(sandbox_adapter)
receipt = coordinator.submit(session_id, idempotency_key)

# 后续运维/调度可重试清理；重复 submit 仅返回原回执，不重做导出或清理。
for view in coordinator.lifecycle.list_cleanup_candidates():
    coordinator.cleanup(view.session_id)
```

可显式注入 `lifecycle=...` 和 `submissions=...`，两者必须共享同一个 SQLAlchemy `sessionmaker` 实例；仅注入其中一个时，协调器用其工厂和时钟构建另一个。幂等键在更改状态或调用沙箱前验证。协调器执行以下顺序，底层方法仍供内部使用：

由赵晨一的提交侧协调重试与后续调度，沙箱侧提供禁止写入、真正暂停和稳定导出能力。顺序如下：

1. 提交入口先校验考生归属，并通过 `SubmissionService.get_session_receipt(session_id)` 检查是否已有已提交回执；有则直接返回。
2. 调用 `lifecycle.begin_submission(session_id)`，成功返回意味着 `submitting` 已在数据库提交，新写入从此被拒绝。进行中的保存未结束时会拿不到锁，提交侧有界重试。
3. 重新进入 `lock_session`，再次核对状态。沙箱侧关闭写入入口、等待在途保存，暂停整个考生环境（包括后台进程），再导出稳定工作区。暂停和导出必须支持重试；不能用删除容器代替冻结。
4. 在这个锁上下文里调用 `locked.persist_frozen_submission(submissions, key, files, public_test_output=...)`。它复用当前数据库事务，避免另外一个连接等待自己持有的行锁。快照元数据、最终提交和会话 `submitted` 状态在同一事务中提交。
5. 成功退出锁上下文后才能向调用方回成功。块内拿到的 `receipt` 尚不能当作已提交回执，更不能在块内删除沙箱。
6. 再开一个锁上下文，调用 `locked.require_cleanup()`，核对已提交记录和原容器 ID后才通知沙箱侧删除容器；按双方约定保留考生文件，不删除数据库答卷和快照。成功后调用 `locked.mark_sandbox_deleted(original_id)`。

不要在持有 `lock_session` 时调用普通 `SubmissionService.persist_frozen_submission()`，它会另开事务锁同一会话；必须使用 `locked.persist_frozen_submission()`。底层 `persist_in_transaction()` 供内部复用，事务所有者负责提交后才响应。

适配器需要提供两个同步、有界超时、可重复调用的方法；这两个方法在会话行锁内执行，不能另起事务修改同一会话，也不能在内部再次进入 `write_guard` 取得同一行锁：

| 方法 | 实现要求 |
| --- | --- |
| `freeze_and_export(view: SessionView) -> FrozenWorkspace` | 核验原容器归属、宿主和镜像；关闭文件/PTY 新写入，等待已接受保存，暂停整个环境，再导出稳定数据。重试连接原暂停环境。失败时保留环境和文件，不自动恢复编辑 |
| `delete_sandbox(view: SessionView) -> None` | 只删除 `view.sandbox_id` 对应的原容器，保留考生文件；容器已不存在视为成功。若删除成功但数据库清理标记未提交，再次删除仍须成功 |

`FrozenWorkspace(session_id, sandbox_id, files, public_test_output=None)` 是协调器模块里的不可变数据类。会话 ID 和容器 ID 必须与当前锁内视图完全一致，否则拒绝入库。`files` 使用 `SnapshotFile` 可迭代对象，惰性导出的数据源须在整个持久化期间保持可读且稳定，读到一半失败也不能发布最终答卷。公开测试输出为已取得的结果；暂停后不再启动测试进程。适配器自身负责 SDK 超时和连接配置，协调器不内置 Docker 客户端或后台线程超时取消。

## 5. 失败、重试与清理归属

- 暂停/导出/快照/数据库失败：沙箱侧保留环境和文件，不自动恢复编辑或删除容器。错误处理应在失败的锁上下文退出、事务回滚之后执行。
- 若提交结果不确定，先查询 `get_session_receipt`；已存在回执就按成功处理。没有时另开锁调用 `mark_submission_failed("SNAPSHOT_WRITE_FAILED")` 等安全错误代码，保持禁止编辑，不因重试重置截止时间。
- 由提交侧发起交卷重试，重新 `begin_submission`，复用同一暂停环境、容器 ID、固定资源标识及首份最终答卷语义；沙箱侧的暂停/导出须幂等。HTTP 提交入口已接协调器，当前尚未新增自动重试 worker。
- 快照已落盘而数据库失败时，可能保留未引用文件；它不会被当作成功答卷，也不会触发沙箱清理。该类文件的垃圾回收仍未实现。
- 只有确认数据库提交成功后，提交侧才发起清理通知；本阶段提供待清理台账 `list_cleanup_candidates(limit=...)`，尚未启动后台清理 worker。沙箱侧执行删除，保留工作区文件；容器已不存在时应按幂等成功处理。
- 清理失败调用 `locked.mark_cleanup_failed("SANDBOX_CLEANUP_FAILED")`，会话仍是 `submitted`，答卷成功不撤销。待清理记录继续可查；后续清理必须再次拿锁、核对原 ID。清理后保留历史映射，不创建新空环境。

协调器已实现上述回执恢复、交卷失败记录与成功后的清理尝试。冻结/导出失败抛出 `SubmissionAttemptFailed`，安全代码为 `SANDBOX_FREEZE_EXPORT_FAILED`；导出身份不匹配为 `SANDBOX_EXPORT_MISMATCH`；快照校验、存储或最终数据库事务失败为 `SNAPSHOT_PERSIST_FAILED`。错误消息及日志不包含沙箱 SDK 原始异常。适配器冻结失败时，数据库门禁仍禁止编辑；真正暂停是否成功须由适配器确认，失败不能被当作已有稳定快照。

`SessionBusy` 表示已有保存、交卷或清理持有数据库锁；本次请求不调用沙箱，应轮询或有界重试。未知会话和不允许的起始状态分别保留 `LifecycleNotFound`、`LifecycleConflict`。提交事务失败后，协调器先查询已提交回执再记录失败；数据库不可用或锁冲突可能使错误台账暂时无法写入，此时已提交的 `submitting` 门禁继续生效，重试仍使用原会话。进程退出同样可能留下 `submitting`，由后续调用重试恢复。

`submit()` 在提交成功后尝试清理；清理失败仍返回固定提交回执，并尽力记录错误，待清理台账不会消失。单独调用 `cleanup()` 失败抛出 `SandboxCleanupFailed`，也可能透传清理资格错误或 `SessionBusy`；调用方逐条捕获后安排后续重试。当前没有自动重试循环、到期扫描或清理 worker，示例循环是接入方式，并非已启动的后台任务。

## 6. 仍需沙箱侧提供的接入信息

需要陈弘毅提供代码分支或方法签名，确认固定资源名/标签查找、不可变镜像解析、归属验证、真正的 pause 与稳定工作区导出接口，并实现第 4 节的 `SubmissionSandbox` 协议。已有创建、状态、停止、删除能力不会自动等于已经完成冻结导出。创建侧 `adapter.*` 调用仍为接口示意；交卷协调器已实现并用沙箱替身与真实 PostgreSQL 验证事务和重试。真实 Docker + gVisor 联调、截止自动交卷和后台清理在后续阶段完成。

## 7. Windows 快照并发兼容修复

已复现 `test_concurrent_writers_publish_one_complete_object` 的路径误判：Windows 并发发布硬链接和创建目录时，`Path.resolve()` 偶尔保留 `\\?\` 命名空间前缀，使同一快照根目录的合法路径与普通盘符路径比较失败。现在仅解析父目录边界，并将 Windows 普通盘符、UNC 与扩展命名空间转换成一致的比较表示；实际读写路径不改写，快照文件符号链接仍被拒绝，读取仍校验摘要。

修复后本地 Windows 16 线程累计 5,120 次写入无误；Linux 容器中的 20 项快照测试全部通过，包括并发、目录越界和符号链接拒绝。Windows 测试夹具使用存储模块规范化后的根目录，兼容 CI 临时目录的短名或目录别名；另有真实目录链接越界用例确认不会向根目录外写入。合入修复无需改动调用方，也不改变快照格式。涉及新版本字段时，再执行 Alembic `upgrade head` 到 `0004`。

# 工作区文件持久版本

本次基于 `codex/sandbox-lifecycle@91ebff9`，补齐读取业务已经调用的版本接口。模型在 `backend/app/db/models.py`，方法在 `backend/app/db/lifecycle.py`，迁移为 `0005_add_workspace_file_revisions.py`。接入后执行 `python -m alembic upgrade head`，当前 head 为 `0005`；无需修改 `WorkspaceReader` 的调用。

## 数据与接口

`workspace_file_revisions` 的复合主键为 `(session_id, path)`，`session_id` 外键关联原 `exam_sessions.session_id`，会话删除时通过 `ON DELETE CASCADE` 清理版本行。`path` 最长 500 字符，`version` 为正整数 BIGINT，`content_sha256` 是 64 位小写十六进制 SHA-256；四列均非空。该表仅保存最新一次观察的版本与摘要，不存文件正文或另建会话。

```python
with lifecycle.lock_session(session_id) as locked:
    view = locked.require_writable()
    content = sandbox.read_file(view, path)
    digest = sha256(content.encode("utf-8")).hexdigest()
    version = locked.observe_file_revision(path, digest)
# 退出上下文提交成功后，再响应浏览器。
```

`LockedSession.observe_file_revision(path: str, content_sha256: str) -> int`：

- 首次观察该会话和路径返回 1；摘要相同保持原版本。
- 摘要变化递增 1，并保存新摘要；内容改回早先值也递增，不复用旧版本。
- 不同路径、不同会话分别计数；进程或后端实例重建后从数据库继续。
- 只用对象已有的 SQLAlchemy session 和事务，不调用 `lock_session`、不对版本行再次 `FOR UPDATE`、不创建数据库连接、不自行 commit。
- 新建或递增时会 flush 当前事务的待写入内容，支持关闭 autoflush 的调用方；flush 不等于提交。外层异常时新建和更新均回滚，不对其他事务提前可见。
- 锁上下文失效后拒绝调用。方法校验规范相对 POSIX 路径和摘要格式，错误抛出不包含原始输入的 `ValueError`。

路径白名单、会话归属、可编辑状态、截止时间以及读取/写入的权限仍由工作区业务负责；版本方法本身不访问沙箱、不替代 `require_writable()` 或文件安全校验。

## 并发与边界

跨请求、跨进程统一持有原会话行锁。竞争者按现有 `SessionBusy` 语义重试，获锁后读取最新已提交的版本，不使用 Python 内存计数。所有文件版本写入者必须遵守该协议；直接绕过会话锁修改此表不在支持范围内。

这是“观察到的内容变化”计数，不是每次终端写入的事件编号。两次观察之间发生多次修改，最多在下一次观察时增加一次；最终内容相同不会递增。该方法不提供 PUT 的安全保存、预期版本校验、外部写进程互斥或失败恢复，相关实现仍由沙箱/文件模块继续完成。

迁移只创建此表，不改动已有会话、容器绑定、考试截止时间或最终答卷。降级到 `0004` 会删除版本表及其记录，因此不能把降级当作保留版本的回退方式。

## 验证

从 `backend` 执行，使用具备创建测试数据库权限的 PostgreSQL 配置：

```sh
VIBEEVAL_RUN_DB_TESTS=1 python -m unittest discover -s tests -p test_workspace_file_revisions.py -v
```

测试只创建和清理自己的随机测试数据库。14 项新增测试覆盖版本恢复、相同/变化/回退内容、作用域、数据库约束、级联删除、迁移升降级、关闭 autoflush、失效锁、事务可见性与回滚、实际 OS 子进程竞争，以及原 Cookie + 原读取路由 + PostgreSQL 的版本对接。沙箱 I/O 使用替身，这组测试不计为真实 runsc 或安全保存验收。

2026-11-03 本地 Windows / Python 3.13 / PostgreSQL 16：14 项新增测试通过；完整回归 268 项，261 通过、7 项需显式开启的真实沙箱用例跳过。总行覆盖率 90.7%，迁移升降级与模型一致性测试通过，Black、Ruff 通过。远程 CI 结果以对应提交的 Actions 为准。

# 到期交卷、恢复与清理扫描

`app.submissions.maintenance.SubmissionMaintenance` 从 PostgreSQL 的共享会话和最终提交记录重建待办，每轮最多处理一个有界批次。它复用既有 `SubmissionCoordinator` 和沙箱适配器，不另建会话表、任务队列或 Redis 唯一状态；本阶段无新迁移，仍使用 `0004`。

## 处理范围

| 条件 | 动作 |
| --- | --- |
| 已开考、已绑定沙箱，状态 `in_progress`，服务端时间达到 `deadline_at` | 提交 `submitting` 门禁，调用同一冻结/导出/快照事务 |
| 状态 `submitting`，例如进程在提交门禁后退出 | 继续原会话的交卷，不要求浏览器在线 |
| 状态 `submission_failed` | 共享重试间隔到达后重试，即使尚未到考试截止时间；原交卷意图已由状态记录 |
| 状态 `submitted`，确有最终提交、容器未记录清理完成 | 只重试原容器的清理，不再导出或修改答卷 |

仅处理配置的 `SANDBOX_BACKEND_REF`；其他宿主、未开考、准备失败、未绑定、已清理会话不处理。状态被手工改为 submitted、但没有真正最终答卷的记录，不具备清理资格。准备失败会话仍由沙箱恢复流程处理，不能由扫描器新建空环境。

扫描使用 `FOR UPDATE SKIP LOCKED` 跳过正被保存或其他处理者锁定的会话，防止一条繁忙记录阻塞后面的记录。实际动作前再次在生命周期行锁下核对宿主、状态、重试时间和截止时间，提交门禁后再进入协调器。外部冻结/删除仍按原 ID 幂等重试，不声称跨数据库与 Docker 的 exactly-once。

失败时间复用 `lifecycle_error_at`，每个处理进程都从共享数据库判断重试间隔。优先处理没有失败记录及更早失败的会话，避免永久失败记录占满每个批次。扫描和执行之间可能出现竞争，竞争者返回 busy/deferred 或由幂等回执吸收；不会覆盖第一份答卷。清理失败保持 `submitted`，提交失败继续禁止编辑。

## 启动

在 Linux/gVisor 后端安装 `requirements-dev.txt`，或 `requirements.txt` 与 `requirements-gvisor.txt`。使用与 HTTP 服务相同的 PostgreSQL、沙箱宿主和持久快照存储，环境变量见 `backend/.env.gvisor.example`。从 `backend` 目录执行一轮：

```sh
python -m app.submissions.maintenance --batch-size 20 --retry-delay 30
```

持续运行：

```sh
python -m app.submissions.maintenance --watch --interval 5 --batch-size 20 --retry-delay 30
```

默认一轮，不自动安装系统计划任务、启动后台服务或在 FastAPI 导入时启动线程。持续模式在上一轮结束后等待 interval 秒再扫描；由部署方选择启动与监督方式。Ctrl+C 返回 130；单轮存在处理失败返回 1，成功返回 0；持续模式逐轮输出结果，全局数据库/配置故障返回 1 供进程监督者重启。

默认快照目录沿用根 `runtime/snapshots`。HTTP 如通过 `SnapshotStore(root=...)` 使用自定义持久存储，扫描命令同时传入 `--snapshot-root /same/persistent/snapshots`；必须是相同存储内容，不能让两边分别指向不同文件系统。程序化注入时，给 `SubmissionMaintenance` 传入使用相同工厂和 `SnapshotStore` 的协调器。

## 批量、重试与观测

- `batch-size` 为 1～100，默认 20，提交和清理共用一个批次上限。
- `retry-delay` 为 1～3600 秒，默认 30，从共享错误时间计算；用于后续扫描的失败重试，不限制人工恢复操作。
- `interval` 为 1～3600 秒，默认 5，仅持续模式使用。每条外部操作仍受沙箱适配器的单次超时约束。
- stdout 每轮只输出 JSON 计数：`examined`、`submitted`、`cleaned`、`deferred`、`busy`、`failed`。submitted 表示已取得固定回执，cleaned 表示本轮处理的清理待办；一次成功交卷内部完成的清理不再重复计数。

退出 0 表示本轮无处理错误，不代表全库待办已经清空；剩余记录留待下一轮。单条失败不阻止本批其他会话；错误代码和时间继续存于共享会话，日志不打印原始 SDK 异常、数据库凭据、文件内容或磁盘路径。

到期扫描是服务端定期触发，提交时间记录实际完成时间，不倒填为截止时刻。真正暂停由适配器完成，触发精度受扫描、处理时长和宿主负载影响；文件/PTY 入口仍须严格执行 `write_guard` 的截止门禁。当前不承诺在 deadline 时刻对后台进程作硬实时冻结，生产有此要求时须与沙箱侧补充宿主级截止措施。

## 验证

`test_submission_maintenance.py` 使用真实隔离 PostgreSQL 验证截止边界、浏览器无关恢复、失败后共享间隔、进程中断、多处理者竞争、批次限制、跨宿主隔离和失败记录公平性。CLI 用例验证参数、计数、安全错误与自定义存储路径。

`test_sandbox_submission_integration.py` 增加浏览器关闭后的到期提交，以及保留原回执的定期清理重试；与其余用例一样，普通 CI 用 Docker I/O 替身，真实 runsc 需显式开启。开启方式和宿主要求见 [沙箱联合验收](sandbox-submission-validation.md)。这些测试不替代文件 HTTP、PTY、准备/开始路由和正式工作台的整体联调。

2026-11-02 本阶段验证结果：

| 环境或检查 | 结果 |
| --- | --- |
| Linux / Python 3.12 / PostgreSQL / Docker 29.1.3 / runsc release-20261026.0 | 完整回归 224 项全部通过，零跳过，含 6 项真实 gVisor 联合用例 |
| Windows / Python 3.13 / PostgreSQL | 24 项维护模块定向测试全部通过；Docker 替身联合测试通过，真实 gVisor 用例按环境跳过 |
| 覆盖率与静态检查 | Linux 后端总覆盖率 92.0%，维护模块 97.6%；Black、Ruff 通过 |
| 数据库 | Alembic 当前为 `0004`，升级及模型差异检查通过，无新迁移 |
| 实际命令启动 | 独立 Linux 测试数据库执行单轮 CLI，输出全零 JSON 计数并返回 0 |

验证使用独立测试 daemon、socket、网络和临时数据库；验收后移除临时服务与数据，保留官方安装包和测试镜像缓存。开发 PostgreSQL/Redis 未参与清理。

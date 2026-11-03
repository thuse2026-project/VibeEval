# 沙箱与交卷联合验收

本阶段接入成员 C 的 `codex/sandbox-lifecycle@761cdc7`，与既有共享数据库、交卷 HTTP、快照和管理端组合验证。显式应用入口为 `app.sandbox.http_app:create_app`；默认 `app.main:app` 继续保持无适配器配置，不会误创建沙箱或生成演示答卷。

## 接入与启动

在已注册 `runsc` 的 Linux Docker 环境，安装 `backend/requirements.txt` 和 `backend/requirements-gvisor.txt`，或直接安装 `requirements-dev.txt`。数据库配置沿用根 `.env`，运行 Alembic `upgrade head` 到当前版本（现为 `0005`，包括工作区文件版本表）。进程环境设置：

```sh
export SANDBOX_BACKEND_REF=docker-gvisor:exam-node-01
export SANDBOX_DOCKER_URL=unix:///var/run/docker.sock
export SANDBOX_REQUEST_TIMEOUT=20
export SANDBOX_OPERATION_TIMEOUT=45
python -m uvicorn app.sandbox.http_app:create_app --factory --host 127.0.0.1 --port 8000
```

命令在 `backend` 目录执行。`backend/.env.gvisor.example` 是示例，不会自动加载。SDK 使用本机 Unix socket；候选人环境不能获得该 socket。进程超时不意味着 Docker 已受理的操作必然取消，失败后按原容器 ID 重试，保留暂停环境。

准备侧通过 `prepare_bound(lifecycle, adapter, session_id, image_ref, task_version_id)` 保存原绑定；它不会把 running 自动视为 ready。可信健康检查完成后由调用方进入生命周期锁执行 `mark_ready()`，考生确认开始时执行 `start_exam()`。当前尚无准备/开始 HTTP 路由，文件 HTTP 与 WebSocket PTY 也仍待成员 C 继续交付。

## 构建测试题目镜像

题目上下文从固定 Git 提交和文件白名单读取，不包含维护者参考答案，也不执行候选人代码。已增加可直接调用的 tar 输出入口；需要完整 Git 历史中存在 `booking_template.REVISION` 指定的提交：

```sh
python -m app.sandbox.booking_template /tmp/vibeeval-booking-context.tar
docker build --tag vibeeval/python-debug-01:validation - < /tmp/vibeeval-booking-context.tar
export VIBEEVAL_GVISOR_IMAGE_REF="$(docker image inspect --format '{{.Id}}' vibeeval/python-debug-01:validation)"
```

镜像运行用户为 1000，公开文件只读；`src/booking.py` 和预建的 `tests/test_candidate.py` 内容可写。实际引用以构建出的不可变 ID 为准，不使用手工脚本里其他机器的镜像 ID。gVisor 安装参考 [官方安装说明](https://gvisor.dev/docs/user_guide/install/) 与 [Docker 配置说明](https://gvisor.dev/docs/user_guide/quick_start/docker/)。

## 自动化验收

`backend/tests/test_sandbox_submission_integration.py` 提供两组测试，均使用真实 PostgreSQL、原浏览器 Cookie 和真实提交/查询代码：

- `DockerBridgeIntegrationTests`：只替换 Docker I/O，实际调用 `BoundSandboxAdapter`、工作区 tar 解码、HTTP、事务和快照；普通数据库 CI 执行。
- `GvisorSubmissionIntegrationTests`：实际创建 `runsc` 容器，并通过 `SandboxRuntime` 的 spawn 子进程执行冻结导出和删除。需显式开启；缺少本地 Unix socket、runsc 或预建镜像时明确失败，不切换为 runc。

在有独立 PostgreSQL 测试权限的 Linux 环境执行：

```sh
export VIBEEVAL_RUN_DB_TESTS=1
export VIBEEVAL_RUN_GVISOR_TESTS=1
python -m unittest discover -s tests -p test_sandbox_submission_integration.py -v
```

两组现在分别覆盖六条路径：正常交卷后恢复固定回执并从考官后台读取；快照写入失败保持暂停、原 ID 与原截止时间，随后重试成功；删除超时保留成功答卷和清理待办，单独重试；资源身份不匹配拒绝导出，修正后重试原环境；浏览器关闭后由维护命令触发到期交卷；维护命令按共享间隔重试已提交答卷的清理。真实测试核对六文件集、考生写入标记、空考生测试文件及未提供公开测试输出时的 `null`。

测试会创建随机数据库和带随机会话标签的容器/卷。先断言生产清理保留工作区卷，再由验收夹具删除它自己创建的测试资源；不会清理开发数据库或真实考生答卷。普通 CI 未开启真实 gVisor 时，六个真实用例明确标为 skipped，不计作已通过。

## 本地验证环境与范围

本次在独立 Docker-in-Docker 测试 daemon 中安装官方 `release-20261026.0` 完整 bundle，SHA-512 校验通过，Docker 为 29.1.3；与开发 PostgreSQL/Redis 服务使用不同网络、数据和 socket。测试 daemon 只监听 Unix socket，临时环境使用可信的特权管理容器；此方式用于本地验收，生产部署需另行设计宿主与权限。

gVisor bundle 校验值：`26e4a327565f5da0f73261a6387e2c20f4695a232e330095a3111b70efd604fdf09d734e8cc8f714dbb5c5607ce8c87ea80b26df549d83a146f2efd5a1af448a`。记录具体测试结果时区分普通 CI、Docker 替身和真实运行，不能据文件/终端接口尚未交付的本批代码宣称整个平台闭环完成。

2026-11-02 初次沙箱接入阶段（`3f2f1f8`，增加维护命令前）的验证结果：

| 环境 | 结果 |
| --- | --- |
| Windows / Python 3.13 / PostgreSQL | 完整回归 196 项：192 通过、4 项真实 gVisor 用例跳过；最终 Docker 替身集成用例另行定向复测通过 |
| Linux / Python 3.12 / Docker 29.1.3 / runsc release-20261026.0 / PostgreSQL | 完整回归 196 项全部通过，零跳过；含四项真实 gVisor 联合用例 |
| 覆盖率与静态检查 | Linux 总覆盖率 91.5%，包含完全未导入的 namespace 模块；Black、Ruff、actionlint 通过 |
| 迁移 | Alembic 升级至 `0004`，当前版本与模型一致，无新增迁移 |
| 题目构建命令 | CLI 输出 tar 与原受控构建函数的 SHA-256 相同 |

完整回归负载下实际观察到删除容器请求超时：答卷与待清理台账保持不变，按原 ID 有限重试后成功记录清理完成，工作区卷在生产删除调用后仍存在。验收用例最多尝试三次清理，每次受适配器超时限制；这不是已启动的生产重试 worker。不要把清理超时改成自动恢复编辑或撤销答卷。

同日增加维护命令后，Linux 再次完成 224 项全量测试，含 6 项真实 gVisor 用例，零跳过；维护阶段的覆盖率、CLI 和 Windows 验证见 [提交定期处理](submission-maintenance.md#验证)。

本次仅证明提交侧与该批沙箱适配器的联合链路；文件 HTTP、PTY、准备/开始路由、正式工作台与生产部署仍需后续交付。临时 Docker/数据库服务验收后清理，官方安装包和测试镜像保留作本地缓存；主开发数据库不参与这些清理操作。

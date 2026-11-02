# 应用容器部署与持久化验收

`deploy/compose.yml` 是独立的应用栈，包含前端静态站点、同源 API 代理、单进程后端、一次性迁移、PostgreSQL 16 和 Redis。根 `docker-compose.yml` 继续用于本地数据库开发，两套项目使用不同的项目名和命名卷。这里提供部署配置和本地验收，不代表已经上线服务器或完成整个考试工作台。

## 镜像与存储

- `deploy/Dockerfile.backend` 安装应用与 Docker SDK，包含后端和迁移，以 UID/GID 1000 运行；不包含题目参考答案、Git 历史、测试夹具和本地配置。
- `deploy/Dockerfile.frontend` 按 npm 锁文件构建，以 UID/GID 101 的 Nginx 提供静态文件和 `/api/` 代理。SPA 页面刷新保留入口，未知 API 与静态资源保持真实错误响应。
- Python、Node、Nginx、PostgreSQL 和 Redis 基础镜像均固定 digest。应用镜像标签使用审查后的 Git 提交号，镜像标签同时写入 OCI revision label；部署方须保存实际 image ID，不能用重新构建的同名标签代替原验收镜像。
- `postgres_data` 保存共享数据库；`redis_data` 保存 AOF；`app_data` 保存 `runtime/admin-accounts.json` 和 `runtime/snapshots`。HTTP 与维护进程挂载同一个 `app_data`，默认快照路径均为 `/opt/vibeeval/runtime/snapshots`。

应用服务只读根文件系统，临时文件写入 `/tmp`，持久数据写入命名卷。只发布 `127.0.0.1:8080`，数据库、Redis 和后端不发布宿主端口。`.dockerignore` 通过白名单排除 `.env`、`runtime`、考生数据和维护者题解，构建不会把这些内容送入上下文。

默认启动先等待 PostgreSQL 健康，再运行 Alembic `upgrade head`，迁移成功后启动后端和前端。后端健康检查只确认 HTTP 进程可响应，不代替数据库、沙箱或考试就绪检查。依赖条件依据 [Compose 启动顺序说明](https://docs.docker.com/compose/how-tos/startup-order/)。

代理保留 Cookie、请求路径与来源校验头，关闭访问日志；后端也关闭 access log。Nginx 覆写浏览器传来的 `X-Forwarded-For`，后端从不发布端口的内部网络接受代理地址，避免所有考生共用一个代理 IP 限流桶；可信代理参数依据 [Uvicorn 配置说明](https://uvicorn.dev/settings/#http)。WebSocket Upgrade 已配置，依据 [Nginx 官方说明](https://nginx.org/en/docs/http/websocket.html)，但目前没有 PTY 路由可做实际终端验收。代理通过 Docker DNS 定期重新解析后端地址，支持后端容器替换。

## 首次启动

需要 Linux 容器、Docker Compose 的 `--wait` 支持和本地构建能力；基础模式也可在 Docker Desktop 验收。以下命令在仓库根目录的 Linux shell 执行：

```sh
mkdir -p runtime
cp deploy/.env.example runtime/deploy.env
```

编辑私有配置：设置非空 `POSTGRES_PASSWORD`，并将 `VIBEEVAL_IMAGE_TAG` 设为已审查提交号。生产文件限制为仅维护者可读，例如 `chmod 600 runtime/deploy.env`，不要提交实际配置。定义本次部署命令：

```sh
dc() { docker compose --env-file runtime/deploy.env -p vibeeval-app -f deploy/compose.yml "$@"; }
dc config --quiet
dc build backend web
dc up -d --wait --wait-timeout 120
dc ps --all
dc images
```

`migrate` 正常显示退出码 0，其余服务应健康。浏览器访问 `http://127.0.0.1:8080`。首次没有默认考官账号，维护者在交互终端设置账号后重启后端：

```sh
dc run --rm --no-deps backend python -m app.admin.accounts examiner --name '考官'
dc restart backend
```

密码通过终端输入，不放在命令、镜像或 env 文件中。当前考官登录会话与限流仍在单进程内，后端固定一个 worker；重启后考官重新登录。考生恢复记录和已提交答卷保存在 PostgreSQL，不因后端重启重置。请勿直接横向扩容后端，考官会话共享仍需后续实现。

基础模式不挂载 Docker socket，使用默认应用入口：尚无最终答卷的交卷返回 503，已持久化答卷仍可恢复回执。实际考试必须先完成下述 gVisor 接入和剩余工作台联调。

## Linux/gVisor 模式

先在宿主注册 `runsc`，构建可信题目镜像并记录不可变引用，要求见 [沙箱联合验收](sandbox-submission-validation.md)。私有 env 文件补齐稳定的 `SANDBOX_BACKEND_REF`、socket 路径和 `SANDBOX_DOCKER_GID`，后者从 `stat -c %g /var/run/docker.sock` 取得；不能为解决权限问题把 socket 改为全员可写。

改用包含覆盖配置的命令，后续升级、停止、备份均保持同一套文件和项目名：

```sh
dc() { docker compose --env-file runtime/deploy.env -p vibeeval-app -f deploy/compose.yml -f deploy/compose.gvisor.yml "$@"; }
dc config --quiet
dc up -d --wait --wait-timeout 120
dc logs --tail 20 maintenance
```

覆盖配置给后端注入 `app.sandbox.http_app:create_app`，同时显式启动 `maintenance --watch`，每轮输出处理计数。两个可信控制进程通过同一 socket 操作同一宿主，并共享数据库和快照存储。socket 的只读挂载不会把 Docker API 变成只读能力；它只给可信控制服务使用，不能传给考生容器。维护进程不使用 HTTP 健康检查，退出后由 Compose 重启，处理失败的持久状态和共享间隔仍由数据库恢复，语义见 [提交定期处理](submission-maintenance.md)。

应用栈不会自动准备考生环境、运行沙箱健康检查或把环境标为 ready；准备/开始、文件 HTTP、PTY 和完整候选人工作台仍需相应成员接入。正式浏览器入口还需部署方配置 HTTPS，并设置 `COOKIE_SECURE=true` 后验收 Cookie 和 WSS；当前回环 HTTP 用于本地验收。增加外层 TLS 代理时，需按可信代理地址配置 Nginx 的真实客户端地址处理并验证限流，不能直接信任浏览器自报的转发地址。

## 更新与回退

先结束或妥善保留活动考试，停止写入服务再备份；应用更新不删除已绑定沙箱或工作区卷。考生沙箱卷位于沙箱 daemon，不在 `app_data`，活动工作区的保留/备份需与沙箱负责人协调，不能只备份本应用卷就宣称能恢复活动考试。

备份至少包含同一停写窗口的 PostgreSQL dump、完整 `app_data`、已部署提交号及前后端 image ID。gVisor 模式先执行 `dc stop maintenance`。下面示例使用默认数据库用户名和数据库名，修改过名称时对应调整；备份目录在仓库之外或受控私有位置：

```sh
dc stop web backend
# 保持 postgres 运行供 pg_dump。
dc exec -T postgres pg_dump -U vibeeval -Fc vibeeval > /private/backup/database.dump
dc run --rm --no-deps -T backend tar -czf - -C /opt/vibeeval/runtime . > /private/backup/runtime.tgz
```

将 `VIBEEVAL_IMAGE_TAG` 改为已审查的新提交号，构建镜像，再 `dc up -d --wait --wait-timeout 120`；迁移服务随镜像变化重新创建。核对数据库版本、登录、入场恢复和答卷调阅后记录部署结果。不要在升级或普通停止时运行 `down --volumes`，也不要删除快照文件或重置考生绑定。

回退优先复用保留的旧应用镜像，使用原标签和 `--no-build`。只有数据库 schema 与旧版本兼容时才能直接切回；否则先停止写入，恢复同一份数据库和快照备份，再启动对应版本。不要未经验证自动 `alembic downgrade`，也不要回退数据库而保留另一时点的快照目录。

## 可重复的隔离验收

`tests/deployment/check_stack.py` 对已构建镜像做真实 HTTP 与持久卷验收。它要求项目名以 `vibeeval-app-check-` 开头、数据库名以 `vibeeval_compose_` 开头，且使用固定的公开测试密码 `ci-only-password`；不接受生产配置。脚本会重建测试容器与网络，保留卷来检查数据恢复，不自行删除卷。

复制示例到忽略的 `runtime/application-check.env`，设置 `VIBEEVAL_IMAGE_TAG=local-check`、`POSTGRES_DB=vibeeval_compose_check`、`POSTGRES_PASSWORD=ci-only-password`、`VIBEEVAL_HTTP_PORT=18087`、`SANDBOX_DOCKER_GID=0`。此 gid 仅用于覆盖文件静态校验，基础验收不连接 Docker socket：

```sh
docker compose --env-file runtime/application-check.env -p vibeeval-app-check-local -f deploy/compose.yml build backend web
python tests/deployment/check_stack.py --env-file runtime/application-check.env --project-name vibeeval-app-check-local
```

脚本验证静态资源、SPA 刷新、未知 API 错误、来源校验、Cookie、伪造转发头不能绕过限流、后端地址更新、迁移、非 root 运行、原提交回执和快照跨全栈重建恢复。测试专用的受信脚本生成合成答卷，不运行考生代码，不把这组测试计为真实 gVisor 验收。完成后仅清理该测试项目：

```sh
docker compose --env-file runtime/application-check.env -p vibeeval-app-check-local -f deploy/compose.yml down --volumes --remove-orphans
```

CI 增加 `Application Compose persistence smoke`，执行同一组构建和验收，并纳入 `Required checks`。本地通过不表示本阶段尚未推送的 CI 已执行，远程结果以具体提交的 Actions 为准。

2026-11-02 本阶段本地验证记录：

| 检查 | 结果 |
| --- | --- |
| 环境 | Windows Docker Desktop 的 Linux 容器；Engine 29.9.2，Compose 5.6.1，后端 Python 3.12.16，Nginx 1.30.6 |
| 镜像构建 | 前端 npm 锁文件安装、类型检查及构建通过；后端运行依赖和迁移打包通过 |
| 应用栈 | `check_stack.py` 完整通过，含伪造转发头限流检查及全栈容器重建后的原会话、账号、答卷恢复 |
| gVisor 组合 | 覆盖配置解析、共享数据库/快照卷、稳定宿主标识、socket 和组权限配置核对通过；本批未通过该覆盖栈运行真实 runsc 容器 |
| 维护命令 | 打包镜像内执行单轮 CLI，空待办返回全零计数和退出码 0 |
| 静态与 CI 配置 | Black、Ruff、actionlint 通过；实际汇总脚本 1024 种上游结果组合符合全部成功才放行的规则 |
| 清理 | 仅删除 `vibeeval-app-check-local` 的容器、网络和三个测试卷；开发 PostgreSQL/Redis 保持健康，镜像缓存保留 |

本地工作区构建标签为 `local-compose-check`，后端 image ID 为 `sha256:1657425983925e2585b4fe39784f57abaf97b3d0f3eb2b65e342f9eae7d3bb76`，前端为 `sha256:0c3584fc3bebc11193fd2a5963f89c4eb3d8de1a363a58793961194b3c61a816`；这些是开发验收镜像，正式部署另行记录已审查提交对应的镜像。真实 gVisor 提交链路此前的 224 项全量结果见 [维护阶段记录](submission-maintenance.md#验证)。

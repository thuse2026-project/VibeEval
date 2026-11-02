# VibeEval

面向 AI Coding 能力考核的软件工程课程项目。

当前开发 **Release 1.0 MVP**：先完成无 AI 辅助的「凭证入场 → 独立沙箱准备 → 多文件编辑与终端操作 → 交卷冻结 → 考官调阅」闭环。AI 辅助、隐藏测试和能力画像留待后续版本。

陈智恒负责的模块已提供考官登录、共享数据库入场与恢复、考务总览、只读答卷详情和标准样题；真实提交查询已接共享持久化服务。赵晨一已接入交卷 HTTP 路由与协调器，真实沙箱适配器及候选人交卷交互仍需联调。演示来源可单独预览，陈智恒模块进度与验证结果见 [BCL.md](BCL.md)。

已整合 PostgreSQL 与 Redis 开发环境、共享会话与提交服务。入场接共享数据库的启动、旧数据和沙箱对接说明见 [入场 PostgreSQL 交接](docs/entry-postgres-handoff.md)。

## 文档入口

- [完整 VibeEval 文档](docs/requirements/VibeEval-AICoding%20平台.md)：一期范围以附录 E 为主要依据，正文和其他附录作为背景参考。
- [一期接口草案](docs/api.md)：从附录 E 提取的初始联调参考，尚待组内确认；确认后的接口变更统一在此维护。
- [会议室预约样题](problems/python-debug-01/README.md)：完整考生工程，包含两个待修复缺陷和公开测试。
- [样题维护说明](maintainer/python-debug-01/README.md)：参考修复与发布前验证，仅供维护者使用。
- [陈智恒一期交接](docs/chen-zhiheng-handoff.md)：本人交付、剩余外部依赖与组员接入位置。

- [沙箱与共享数据库接入](docs/sandbox-integration.md)：会话映射、数据库锁、统一状态及交卷/清理边界。
- [交卷 HTTP 接入](docs/submission-http-handoff.md)：原浏览器鉴权、提交回执、沙箱注入与错误处理。

原始需求文档保留讨论背景；README 维护开发入口、目录与协作约定。接口草案允许经组内讨论调整，不要求照搬。

## 开发前需要知道

- 技术基线：前端 React + TypeScript + Vite，后端 Python + FastAPI，数据库 PostgreSQL。沙箱通过适配器接入，当前协作按 Docker + gVisor 生命周期模块对接，尚未完成真实联调；原需求中的 E2B 保留为背景方案。
- 前端负责交互；身份权限、会话状态和考试截止时间由后端决定，`session_id` 本身不等于访问凭证。
- 考生代码在独立沙箱运行，平台后端负责管理沙箱；不在平台后端直接执行考生代码。
- 交卷后以固定快照为准，后台调阅读取提交快照。
- `problems/` 是给考生的题目工程，与平台源码及平台自身的测试分开。

## 目录与分工

```text
VibeEval/
├── frontend/src/
│   ├── app/                     # 应用入口、路由、整体布局
│   ├── candidate/
│   │   ├── entry/               # 入场、规则确认、环境准备
│   │   └── workspace/           # 文件树、编辑器、终端、交卷
│   ├── admin/
│   │   ├── login/               # 考官登录
│   │   └── submissions/         # 提交列表与详情
│   └── shared/                  # 实际共用的组件、请求工具和类型
├── backend/
│   ├── app/
│   │   ├── core/                # 公共配置、通用鉴权依赖
│   │   ├── exams/               # 考务、入场凭证校验
│   │   ├── admin/               # 考官登录与管理端接口
│   │   ├── workspace/           # 文件接口、终端网关
│   │   ├── sandbox/             # E2B 操作封装
│   │   ├── submissions/         # 交卷、快照、提交查询
│   │   └── db/                  # 数据库连接与模型
│   ├── migrations/             # 数据库迁移
│   └── tests/                  # 平台后端测试
├── problems/                   # 标准题目工程
│   └── python-debug-01/
│       ├── src/                # 待修复代码
│       └── tests/              # 题目的公开测试
├── maintainer/                 # 维护者材料，不发布到考生工作区
├── docs/
│   ├── requirements/           # 原始需求文档及配图
│   └── api.md                  # 当前一期接口草案与后续契约
└── .github/workflows/          # CI 配置
```

| 成员 | 主要维护范围 |
| --- | --- |
| 陈智恒 | 标准题目、考务接入、考官管理端页面与接口 |
| 刘婧瑶 | 候选人工作台及其交互组件 |
| 陈弘毅 | 沙箱生命周期、工作区文件操作、终端网关 |
| 赵晨一 | 数据库模型与迁移、快照提交及查询、开发环境和 CI |

管理端接口调用提交模块提供的查询逻辑；共享会话数据库模型由赵晨一统一维护，各业务模块负责自己的操作逻辑。应用入口、公共配置等共享文件由首次接入者添加，修改时与相关成员协调。

模块内部按实际需要添加文件，不提前批量创建空路由、空服务或模拟实现。目录中的 `.gitkeep` 仅用于保留空目录，加入实际文件时可以删除。

## 本地开发

### 考官登录（本地）

需要 Python 3.12 和 Node.js 22（当前验证环境）；安装及启动命令在 PowerShell 执行。考官登录自身不依赖数据库，但真实入场与总览需要先按 [共享数据库启动说明](docs/entry-postgres-handoff.md) 配置 PostgreSQL 并运行迁移。先在仓库根目录运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
Set-Location backend
..\.venv\Scripts\python.exe -m app.admin.accounts examiner --name "陈智恒"
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

初始化命令会提示输入并确认密码（10～128 字符），仅将带盐哈希写入被 Git 忽略的 `runtime/admin-accounts.json`。再次执行同名账号命令可修改密码，配置生效需重启后端；不同账号可重复执行。不要将真实密码写进命令或提交到仓库。

另开终端，在仓库根目录运行：

```powershell
Set-Location frontend
npm.cmd ci
npm.cmd run dev
```

访问 `http://127.0.0.1:5173/admin/login`，使用刚配置的账号登录。前端将 `/api` 代理至本地 8000 端口，接口文档位于 `http://127.0.0.1:8000/docs`。退出、刷新及未登录直接访问 `/admin` 均可人工检查。

登录使用 HttpOnly Cookie，由后端记录固定 3 小时有效期，刷新不会续期，退出会撤销会话。当前为单进程内存会话，后端重启后全部退出，不使用多 worker；账号数据库持久化后续与工程底座集成。

配置示例见 [`backend/.env.example`](backend/.env.example)。这些是可选的进程环境变量，示例文件不会自动加载；PowerShell 可用 `$env:变量名 = "值"` 设置。账号路径默认位于仓库 `runtime/`；以后通过 HTTPS 部署时应设置 `COOKIE_SECURE=true`，当前本地 HTTP 预览保持默认值。

验证命令（从仓库根目录运行）：

```powershell
Set-Location backend
..\.venv\Scripts\python.exe -m unittest discover -s tests -v
Set-Location ../frontend
npm.cmd run build
```

接口测试通过模拟服务端时钟验证恰好 3 小时失效，不需要实际等待。`npm run dev` 仅用于本地调试，正式部署方式后续补充。已提供的 `docker-compose.yml` 和数据库迁移配置见后文。

### 考生入场与恢复（本地联调）

先配置共享 PostgreSQL 并运行迁移，再沿用上述前后端启动方式，无需 E2B。在 `backend/` 执行以下命令预配置考生凭证：

```powershell
..\.venv\Scripts\python.exe -m app.exams.provision --candidate-id student-001 --name "张同学" --exam-id exam-001 --title "平台入场联调" --duration-minutes 60 --valid-hours 24
```

命令只输出一次随机 `tok_...` 凭证，请私下保存并发给对应考生，不放入仓库；服务端只存哈希。同一考生编号在同一场次不能重复生成。场次标识、标题和时长目前随凭证预配置，不包含题目，后续对接正式考务数据。

访问 `http://127.0.0.1:5173/entry` 粘贴凭证，成功后展示考生、考核和固定会话编号。此页面是最小入场联调入口，后续由候选人工作台接入。尚未启动沙箱，也没有“开始作答”或考试计时；响应中的 `started_at`、`deadline_at` 均为 `null`，状态为 `admitted`。

- 凭证默认只在生成后 24 小时内允许首次入场；`--valid-hours` 可调整。一次成功后只能原浏览器恢复，其他浏览器不能复用该凭证。
- 同一浏览器刷新或重新打开 `/entry` 自动恢复；邀请凭证之后过期不影响恢复，重试不会创建新会话。已经入场的浏览器暂不支持切换考生或场次。
- 恢复依赖本站的 HttpOnly Cookie，固定保存最长 30 天。清除本站数据、使用新的隐私窗口、Cookie 到期或丢失后，无法自动恢复，需要联系考官；已消费的凭证不会因此重新可用。这个恢复窗口与考试时限分开，后续开始作答后仍以考试截止时间为准。
- 入场记录与恢复凭证哈希保存于共享 PostgreSQL 的 invitations/exam_sessions，重启可恢复。旧 SQLite 数据不自动导入或删除；旧测试 Cookie 的处理见入场交接说明。数据库故障不回落 SQLite。
- 首次浏览器绑定与兑换使用同源 Web Locks 在多标签页间串行处理，建议本地联调使用当前 Chrome/Edge。未收到兑换响应时可直接重试或刷新恢复，不能改用其他浏览器重试。

用普通窗口入场后，可在隐私窗口粘贴同一凭证检查拒绝重复入场；普通窗口刷新应保留相同会话编号。接口细节及开源参考见 [docs/api.md](docs/api.md)。测试仍使用上面的统一命令，包含考官登录回归。

### 考务总览（本地）

考官登录后直接进入 `/admin` 总览。默认“真实记录”，显示预配置考生及其实际入场状态；选择场次或考生状态可筛选，每页 10 条，点击“刷新记录”更新进度。统计作用于所选场次的全部状态与全部页，每位考生在每个场次计一人次。

切换“数据来源”为“演示数据”，可检查两个虚构场次的未入场、已入场、作答中、已交卷，以及分页。示例不写入数据库、不参与真实统计，页面始终显示演示提示。切换来源会清空场次/状态筛选并返回第一页。

真实数据中，入场成功只更新同一考生行的入场状态和时间，不会自动记为开始作答或交卷；首次入场凭证过期与恢复期限过期分别标记。已交卷记录可进入答卷详情。持久化负责人通过统一 `SubmissionReader` 接入真实提交，契约见 [docs/api.md](docs/api.md)。

人工联调可以先预配置考生，刷新总览看到“尚未入场”；在另一浏览器使用该凭证进入 `/entry`，再刷新总览，同一条记录应变为“已入场，未开始”。网络错误可刷新重试，考官登录过期会返回登录页。

### 答卷详情（只读）

在总览选择“演示数据”，筛选“已交卷”，点击“查看答卷”即可预览固定文件快照和公开测试输出。左侧选择文件，右侧纯文本展示；返回总览保留来源、场次、状态和页码。详情支持直接打开链接，未登录时先登录再继续，浏览器前进/后退也可用。

演示详情示例：`http://127.0.0.1:5173/admin/submissions/demo-submission-4?source=demo`。真实详情必须接入队友的只读 reader，没有接入时明确提示；不会拿演示内容替代真实答卷。此处没有编辑、交卷、重跑测试或评分功能。

### 会议室预约样题（独立运行）

样题仅依赖 Python 3.12 标准库，不需要启动平台前后端。在仓库根目录运行：

```powershell
python problems/python-debug-01/src/main.py
python -m unittest discover -s problems/python-debug-01/tests -p "test_*.py" -v
python maintainer/python-debug-01/verify_sample.py
```

原始演示应有一个场景通过、两个未通过；原始公开测试应为 6 项通过、2 项失败，测试命令非零退出是预期状态。维护者验证命令应成功，展示原始两个预期失败、参考修复 8 项全部通过，并确认原始工程未修改。

仅将 `problems/python-debug-01/` 作为考生工程发布；`maintainer/` 的参考答案不进入工作区或答卷快照。新增测试由考生编写，此样题目前尚未接入沙箱、网页或交卷服务。

### PostgreSQL 与 Redis

需要 Docker Engine 和 Docker Compose v2.20 或以上版本。Windows 可安装 [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/)，使用 WSL 2 后端和 Linux 容器。先确认 Docker 已启动：

```sh
docker version
docker compose version
```

在仓库根目录准备本地配置。PowerShell 使用：

```powershell
Copy-Item .env.example .env
```

Linux/macOS 使用 `cp .env.example .env`。已有 `.env` 时直接编辑，不重复复制覆盖。打开 `.env`，填写 `POSTGRES_PASSWORD`；空密码会使配置检查失败。`.env` 已被 Git 忽略，真实密码不要写入 `.env.example`。

从仓库根目录检查并启动：

```sh
docker compose config --quiet
docker compose up -d --wait --wait-timeout 120
docker compose ps
```

首次启动需要下载镜像。启动成功后，`postgres` 和 `redis` 均应显示为 `healthy`。检查失败时使用 `docker compose logs --tail 100 postgres redis` 查看服务日志。

| 服务 | 镜像 | 本机连接地址 | 数据位置 |
| --- | --- | --- | --- |
| PostgreSQL | `postgres:16-alpine` | `127.0.0.1:5432` | 命名卷 `postgres_data` |
| Redis | `redis:7.4-alpine` | `127.0.0.1:6379` | 命名卷 `redis_data`，启用 AOF |

端口由 `.env` 中的 `POSTGRES_PORT`、`REDIS_PORT` 控制；冲突时修改后重新启动。数据库名和用户名默认均为 `vibeeval`，密码取自 `.env`。后续运行在同一 Compose 网络内的服务应通过 `postgres:5432`、`redis:6379` 连接。

验证数据库查询和 Redis 连接（若改过数据库名或用户名，相应调整命令）：

```sh
docker compose exec postgres psql -U vibeeval -d vibeeval -c "SELECT 1;"
docker compose exec redis redis-cli ping
```

预期分别返回 `1` 和 `PONG`。停止服务使用 `docker compose down`；命名卷保留，下次启动可继续读取原数据。数据库初始化变量仅在新数据卷首次启动时生效，修改 `.env` 不会自动修改已有数据库的账号、密码或库名。

此配置供本地开发使用，端口只绑定本机；Redis 未配置认证。PostgreSQL 保存业务数据，Redis 用于缓存与通知，不能作为业务数据的唯一持久化来源。这两个基础服务不需要 E2B 账号。

### 数据库模型与迁移

需要 Python 3.12 或以上版本，本地已在 Python 3.13 与 PostgreSQL 16 验证。下面的 PowerShell 命令均从仓库根目录执行，已有 `.venv` 时跳过创建步骤：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements-db.txt
.\.venv\Scripts\python.exe -m alembic -c backend/alembic.ini upgrade head
.\.venv\Scripts\python.exe -m alembic -c backend/alembic.ini current
.\.venv\Scripts\python.exe -m alembic -c backend/alembic.ini check
```

Linux/macOS 将 Python 路径换为 `./.venv/bin/python`。先按上一节启动数据库；迁移完成后版本应为 `0004 (head)`，`check` 应提示没有新的迁移差异。启动应用时不自动建表，结构变更统一通过 Alembic 管理。

连接配置从仓库根目录 `.env` 读取，不依赖当前工作目录；同名环境变量优先。默认使用 `POSTGRES_HOST`（`127.0.0.1`）、`POSTGRES_PORT`、`POSTGRES_DB`、`POSTGRES_USER` 和 `POSTGRES_PASSWORD`。可用 `DATABASE_URL` 整体覆盖连接配置，支持 `postgresql://` 或 `postgresql+psycopg://`；直接填写 URL 时须对账号和密码中的特殊字符进行 URL 编码，使用分项配置时无需编码。

共享模型位于 `backend/app/db/models.py`：

| 表 | 用途与约束 |
| --- | --- |
| `invitations` | 保存考生、场次和入场期限；凭证仅保存 SHA-256 哈希，同一考生同一场次只能有一条邀请 |
| `exam_sessions` | 关联邀请，保存浏览器凭据哈希、状态、沙箱引用及入场/开始/截止/提交时间；邀请和浏览器哈希分别唯一 |
| `snapshots` | 保存快照所属会话、内容摘要、字节数、文件数和创建时间；文件正文放持久文件存储 |
| `submissions` | 每会话唯一的最终提交，记录幂等键、固定快照引用及当时的考生/场次身份和时间；禁止引用其他会话的快照 |

`ExamSession.task_version_id` 保存逻辑题目发布版本，`sandbox_image_ref` 保存不可变镜像引用；两者在最终提交时复制到 `Submission`。新接入通过 `prepare_sandbox(..., task_version_id=...)` 固定题目版本，未知旧记录不自动回填。具体调用及 Windows 快照并发修复说明见沙箱接入文档。

时间统一使用 UTC Unix 秒（`BIGINT`）。会话默认状态为 `admitted`，入场时开始、截止、提交时间均为空；开始时间与截止时间必须同时设置，时间顺序由数据库约束保护。`session_scope()` 在成功时提交，异常时回滚并关闭会话。迁移 `0003` 固定了状态集合，新增不可变镜像引用、宿主标识、稳定资源名、清理时间及安全错误台账；`SessionLifecycle` 提供跨进程行锁、绑定读写、幂等开考、写入门禁和清理资格检查。

本分支的入场 HTTP 已使用这些共享模型，状态与固定截止时间从同库读取。真实沙箱冻结、考官账号持久化仍由后续接入完成，锁约定见沙箱接入文档。

### 快照与提交持久化

服务位于 `backend/app/submissions/`，内部契约及 HTTP 接口见 [docs/api.md](docs/api.md#主动交卷-http已实现)。`POST /api/v1/sessions/{session_id}/submit` 复用原浏览器 Cookie 和会话归属校验，需要 `X-VibeEval-Request: 1` 与 `Idempotency-Key`；只在最终答卷成功入库后返回 202 回执。文件和已有公开测试输出由服务端沙箱适配器提供，不接受浏览器上传作为答卷来源，也不在平台后端执行考生代码或测试。

交卷编排入口为 `SubmissionCoordinator(sandbox_adapter).submit(session_id, idempotency_key)`：提交写入门禁后，调用注入的适配器冻结导出，核验会话和容器身份，再持久化最终答卷，数据库提交成功后尝试清理。失败重试复用原会话和沙箱；提交成功而清理失败时保留成功回执及清理待办，可通过 `cleanup(session_id)` 单独重试。真实适配器通过 `create_app(submission_sandbox=adapter)` 接入；默认应用未配置适配器，尚未交卷的请求明确返回 503，已有答卷仍可恢复回执。到期自动提交和后台清理仍待接入；协议及错误语义见 [沙箱接入约定](docs/sandbox-integration.md#4-交卷顺序与事务边界)。

调用方须先完成会话归属校验，将会话置为 `submitting`，禁止新写入并等待进行中的保存结束，停止仍会写文件的进程，再导出固定文件集。之后调用 `persist_frozen_submission()`：服务锁定会话行，落盘并校验快照，在同一数据库事务写入快照元数据、最终提交及会话的提交时间和 `submitted` 状态。重复请求（包括更换幂等键）始终返回首份已提交答卷，不读取或覆盖新传入的文件。

快照以格式版本为 1 的 JSON 保存，默认位置为被 Git 忽略的 `runtime/snapshots/<摘要前两位>/<SHA-256>.json`。写入使用同目录临时文件、文件同步与硬链接发布，已有对象不会被覆盖，读取时检查摘要、字节数及文件数。目标文件系统需支持硬链接；部署时须使用后端独占的持久存储，可通过 `SnapshotStore(root=...)` 指定位置。

默认限制为：最多 1000 个文件、UTF-8 文件内容合计 10 MiB、公开测试输出 1 MiB、序列化快照 16 MiB。文件名须为不含越界片段的相对 POSIX 路径，最多 500 字符；重复路径、文件/目录冲突、二进制内容及超限输入会明确失败，不静默丢弃。空文件和空文件列表允许保存；公开测试输出的 `null` 表示未提供，空字符串表示输出为空，都不表示测试通过。

文件系统与 PostgreSQL 不共享事务。若文件已经落盘但数据库提交失败，数据库记录和会话提交时间会回滚，文件可能作为未引用对象保留；此类文件不会出现在提交查询中，重试可安全复用。当前没有自动垃圾回收，避免误删被其他提交复用的对象。读取损坏或缺失快照时明确失败，不回退到活动工作区。

### 数据库测试

数据库与快照测试沿用标准库 `unittest`。从仓库根目录进入 `backend` 后运行配置、快照存储测试（无需数据库，集成测试默认跳过）：

```powershell
cd backend
..\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

要验证真实 PostgreSQL，先启动 Compose，再在同一目录执行：

```powershell
$env:VIBEEVAL_RUN_DB_TESTS = "1"
..\.venv\Scripts\python.exe -m unittest discover -s tests -v
Remove-Item Env:VIBEEVAL_RUN_DB_TESTS
```

集成测试使用当前连接配置，在 `postgres` 管理库中创建随机命名的 `vibeeval_test_*` 临时数据库，验证迁移、约束与事务后自动删除；账号需有创建数据库权限（本地 Compose 默认账号具备）。测试不会在业务数据库执行迁移回退或清空数据。Linux/macOS 可在 `backend` 目录执行 `VIBEEVAL_RUN_DB_TESTS=1 ../.venv/bin/python -m unittest discover -s tests -v`。

### 后端代码检查与 CI

开发检查依赖固定在 `backend/requirements-dev.txt`，Black、Ruff 和覆盖率配置统一维护在 `backend/pyproject.toml`。从仓库根目录安装开发依赖，再进入 `backend` 检查：

```powershell
$env:PYTHONUTF8 = "1"
.\.venv\Scripts\python.exe -m pip install -r backend/requirements-dev.txt
cd backend
..\.venv\Scripts\python.exe -m black --check .
..\.venv\Scripts\python.exe -m ruff check .
```

需要自动整理格式时，在 `backend` 目录运行 `..\.venv\Scripts\python.exe -m black .`。先按前文启动 PostgreSQL，再执行完整测试和覆盖率检查：

```powershell
$env:VIBEEVAL_RUN_DB_TESTS = "1"
..\.venv\Scripts\python.exe -m coverage run -m unittest discover -s tests -v
..\.venv\Scripts\python.exe -m coverage report
Remove-Item Env:VIBEEVAL_RUN_DB_TESTS
```

覆盖率统计 `backend/app` 的代码行，最低为 80%，不把测试文件纳入统计。未开启数据库测试时不能代替这组完整检查。Linux/macOS 换用 `../.venv/bin/python` 并通过 `VIBEEVAL_RUN_DB_TESTS=1` 设置环境变量。

GitHub Actions 工作流为 `.github/workflows/backend-ci.yml`，在 push、PR 和手动触发时运行：

| 检查 | 内容 |
| --- | --- |
| Linux / Python 3.12 与 3.13 | Black、Ruff、真实 PostgreSQL 完整测试、最低 80% 行覆盖率、迁移升级及模型一致性 |
| Windows / Python 3.13 | 配置单元测试、快照并发及路径校验，不启动 PostgreSQL 服务容器 |
| Linux / Compose | 启动 PostgreSQL 16 和 Redis，验证健康状态、数据库查询和 Redis PING，结束后清理本次 CI 的容器与卷 |

CI 使用独立临时数据库和固定测试密码，不需要额外配置仓库 Secret。检查失败会返回失败状态；是否禁止合并失败 PR 取决于团队另外配置的分支保护，本工作流不修改合并规则。GitHub 实际执行结果以 Actions 和 PR 检查页为准，本地验证不替代远程执行记录。当前工作流检查后端；前端已有依赖清单并可本地构建，前端 CI 仍待工程底座统一补齐。

### 前后端应用

前后端启动见前文。真实沙箱联调仍需要对应适配器、宿主和运行环境，本地 Docker Desktop 不表示已具备 gVisor 沙箱能力。

## 开发与部署流程

日常流程：**本地任务分支开发与调试 → 人工检验 → PR 复查并合并 `main` → 服务器部署已验证的提交 → 联调与验收**。

- 本地用于开发，GitHub 用于代码协作，服务器用于运行集成后的版本。服务器上的项目目录是同一仓库的独立副本，通过 Git 更新，不与本地目录自动同步；日常代码修改在本地完成。
- 当前先推进本地功能开发，暂不配置服务器。首个可运行功能完成后安排首次部署，提前验证部署流程，后续随集成进度更新。
- 部署沿用 Docker Compose 方向，由赵晨一协调。首次部署时补齐启动步骤、部署提交号及回退方式；服务器凭证和环境配置不入库，数据库与提交快照使用持久存储，避免更新服务时丢失。

## 开发约定

四位成员均有 GitHub 仓库管理员权限，日常开发仍遵守以下协作约定。管理员权限不替代代码复查；以下是团队约定，不表示仓库已启用分支保护。

1. **按任务开分支**：从最新 `main` 创建任务分支，例如 `feat/token-entry`、`fix/file-save`、`docs/development-workflow`。一个 PR 完成一件可独立说明和验证的事，不长期使用个人大分支；不另设 `develop` 分支。
2. **交叉复查后合并**：日常修改通过 PR 合并到 `main`，不直接推送主分支，也不因管理员权限跳过复查。至少另一位成员复查通过后再合并；涉及公共接口、数据库或跨模块流程的改动，由陈智恒重点复查。AI 可以辅助审查，最终由成员确认。合并前处理冲突，保持 `main` 可用，合并后删除任务分支。
3. **PR 说明与验证**：写清完成了什么、如何验证、还有什么没完成，并关联相关需求或接口。提交前检查受影响模块的启动、现有测试及代码检查，未完成或未验证的部分如实注明。每阶段还需在集成后的 `main` 验证一期完整闭环，不能只依赖单个 PR 的检查。格式细则交给统一工具配置。
4. **接口与共享部分同步**：修改接口路径、字段、状态或错误语义时，同步更新 `docs/api.md` 并通知调用方；尚未达成一致的接口注明待确认。公共配置、数据库模型与迁移、依赖配置的变更先与相关成员协调；跨模块修改与主要负责人沟通，分工不限制协作。
5. **敏感信息与产物**：密钥、Token、真实考生数据、运行快照不入库；本地配置放 `.env`，新增必需配置时同步更新不含真实凭证的 `.env.example`。运行快照统一存放在被忽略的 `runtime/` 目录。

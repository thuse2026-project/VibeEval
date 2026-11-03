# 成员 C：终端与文件读写接入

`codex/sandbox-lifecycle` 提供真实 Docker/gVisor PTY、受限文件读写及持久版本冲突处理。已整合赵晨一的 `a36df40`，启动前执行迁移到 `0005`。准备、开始和提交继续使用共享 `ExamSession`，没有另建会话表。

## 启动

Linux 主机需已有 Docker、runsc 的 systrap 配置，以及通过 `booking_template` 构建的可信题目镜像。使用共享 PostgreSQL，先运行已有迁移。按 `backend/.env.gvisor.example` 设置进程环境变量，示例文件不会自动加载。

在 `backend/` 下运行：

```sh
python -m pip install -r requirements.txt -r requirements-gvisor.txt
python -m alembic upgrade head
python -m uvicorn app.sandbox.http_app:create_app --factory --host 127.0.0.1 --port 8000 --workers 1 --ws websockets --ws-max-size 16384 --ws-max-queue 4
```

`SANDBOX_TERMINAL_ORIGINS` 填浏览器页面的完整来源，例如 `http://localhost:5173`；多个来源用逗号分隔，不带路径或末尾斜杠。未配置时拒绝全部终端连接。生产页面使用 HTTPS/WSS。Compose 的 gVisor 配置已传递准备和终端所需变量，并限制 WebSocket 帧大小与队列；本批没有部署该应用栈。

Vite 开发代理需要为 `/api` 开启 `ws: true`。现有 nginx 配置已经转发 Upgrade。不要把 Cookie、入场 Token 放进 WebSocket URL。

## 终端契约

`/api/v1/sessions/{session_id}/terminal` 使用原浏览器 HttpOnly Cookie。浏览器 WebSocket 无法添加 HTTP 自定义请求头，因此这里用明确的 Origin 名单检查来源，并复用 `require_candidate_session` 校验归属。开考前、过期、交卷中及交卷后均不能输入。创建 PTY、每次输入和 resize 都持有共享 `write_guard`，交卷协调器可以等待在途操作结束。

消息沿用附录 E：`input/data`、`resize/cols/rows`、`output/data`。Ctrl+C 为 `data: "\u0003"`，不转换为宿主机信号。输入最多 8192 个 UTF-8 字节；整帧最多 16384 字节；cols 为 1–300 的整数，rows 为 1–150 的整数。输出增量解码 UTF-8，每次最多读取 8192 字节，等待浏览器发送完成后才读取下一批；发送超时 5 秒关闭连接。

连接打开的是原沙箱内 UID 1000 的交互 bash。刷新会打开新 shell，保留同一沙箱、文件和截止时间；不恢复屏幕历史，不重放命令。断线关闭代理连接，不承诺终止所有后台作业。提交时由现有适配器 pause 整个容器，冻结 shell 及后台进程；不能以“关闭终端”代替冻结。

已接受连接的关闭码：1000 为正常结束；1008 为消息/身份/会话状态不允许；1013 为会话忙，需要稍后重新连接；1011 为后端或传输故障。握手前拒绝可能表现为 HTTP 403，而非可见关闭码。不要自动重发失败的按键。网关每秒检查状态，空闲连接也会在过期或交卷后关闭；真正的输入权限仍以每条消息取得锁后的检查为准。

## 文件接口与共享版本服务

GET `/files` 与 GET `/file?path=...` 已接入路由、会话鉴权和持久版本观察。先升级迁移到 `0005`，即可使用读取业务已有的版本调用；接口缺失时仍保留 503 `FILE_REVISIONS_UNAVAILABLE` 兜底。底层 `SandboxRuntime.read_file/read_files` 已可读取原环境，固定允许六个题目文件。每个文件限制 1 MiB UTF-8，拒绝链接、异常路径及额外归档条目，不解压到宿主机。

赵晨一已基于 `91ebff9` 补齐，完整约定见 [工作区文件版本](workspace-file-revisions.md)：

1. `workspace_file_revisions` 表：`session_id` 外键关联原 `exam_sessions`，与 `path` 组成主键；`version` 为正整数，`content_sha256` 为 64 位小写十六进制摘要。会话删除时级联删除版本行，配套 Alembic `0005`。
2. `LockedSession.observe_file_revision(path: str, content_sha256: str) -> int`：使用该对象现有事务，不重新锁会话；首次记录返回 1；摘要相同返回原版本；摘要变化时递增并更新摘要。只在锁有效期间调用；路径校验与文件访问由 C 负责。
3. 方法仅在原事务中 flush，不自行提交事务。C 在原会话锁内读取文件、记录版本，退出锁上下文成功后才响应浏览器；异常回滚。已新增真实 PostgreSQL 测试覆盖新实例恢复、跨进程锁竞争、版本更新和回滚。

HTTP 层只检测观察到的内容变化，不能记录终端两次读取之间的所有中间修改。

## 安全保存与失败恢复

PUT `/file` 使用附录 E 的 `path`、`expected_version`、`content`，成功返回 `path`、`new_version`、`saved_at`。内容相同的保存不递增版本。只允许两个可编辑文件，UTF-8 编码后最多 1 MiB；只读路径返回 403，非法路径或参数返回 422。

保存全过程持有共享会话行锁：读取当前内容并核对持久版本，短暂 pause 原沙箱，停止前次遗留的写入容器，再次核对摘要。若终端在首次读取与暂停之间修改文件，返回 409 `FILE_VERSION_CONFLICT`，不覆盖该修改；响应含 `current_version`。已观察到的冲突版本会提交到数据库。

实际写入在同宿主的临时 runsc 容器完成：使用同一可信镜像和已核验归属的数据卷，根文件系统只读、无网络，UID 0 仅增加 CHOWN 能力以保持文件 UID 1000。它只执行平台固定 Python 脚本，不导入或运行考生代码。内容经受限参数传入，不写日志。临时容器资源为 256 MiB、0.5 CPU、64 PID；并发容量还需实测。

脚本使用不跟随链接的文件描述符，先写入同目录 staging 文件并 fsync，再原子替换目标。替换前检查截止时间；成功后移除写入容器、核对内容、恢复原沙箱进程。原子替换会改变 inode，考生程序此前已打开的旧文件描述符仍指向旧 inode；后续按路径重新打开读取新文件。原题目目录权限不放宽。

保存失败返回 503 `FILE_SAVE_UNCERTAIN`，可能保留暂停状态和旧文件或已完整替换的新文件，不返回成功。文件与数据库不是同一个事务：文件替换后数据库提交失败，下一次读取会按实际摘要恢复版本。前端应保留编辑器内容、重新读取核对，禁止自动把旧版本改成新版本后覆盖。用户确认后可用新版本显式重试；成功重试会恢复进程，交卷也可直接冻结当前文件。

写入容器使用原沙箱名加 `-writer` 的固定标识，并核验会话、原容器和镜像。冻结与清理会先终止它并确认不存在；失败时不导出、不恢复编辑。未完成的两个固定 staging 路径不进入快照。API 和维护进程必须一起升级到本批适配器，不能让旧维护进程与新保存模块并行操作同一环境。

保存占用会话锁时，终端输入可能按既有协议以 1013 断开，前端不要重放按键；文件请求遇到 `SESSION_BUSY` 可按 `Retry-After: 1` 有界重试。考试计时不会因保存暂停而延长。

## 验证

普通测试不要求 Docker：`python -m unittest discover -s tests -v`。

真实联合测试在专用 Linux 验证主机运行，需本地已有 `public.ecr.aws/docker/library/postgres:16-alpine` 镜像和可信题目镜像：

```sh
export SANDBOX_IMAGE_REF=sha256:替换为本机可信题目镜像ID
python tests/manual/workspace_database_probe.py
# 同时执行现有数据库回归：
python tests/manual/workspace_database_probe.py --all-tests
```

脚本创建仅监听 127.0.0.1 随机端口的临时 PostgreSQL，生成临时密码，不输出密码；测试创建自己的数据库和沙箱。网络测试启动仅监听回环地址的临时 Uvicorn，使用真实 HTTP Cookie 和 WebSocket，不向公网部署服务。结束后清理临时数据库、测试容器与测试数据卷。

已验证真实网络下的入场、准备、开考前拒绝终端、跨浏览器拒绝、resize、Ctrl+C、终端文件修改、受限读写、断线重连不重置会话，以及交卷门禁与冻结。另有真实保存故障测试：1 MiB 内容、替换前终止脚本、原文件保留、暂停状态下重试，以及交卷清理遗留写入容器并排除未完成 staging。前端 xterm/Monaco 尚未联合验收；并发容量、长时间运行、宿主断电与网络故障尚未验收。公开测试输出仍返回 None，没有把任意终端输出当作可信测试结果。

2026-11-03 专用服务器回归：干净代码集合 253 项测试，247 项通过，6 项旧的 opt-in 沙箱验收因开关未启用而跳过；本批新增真实工作区验收已启用并通过。后台循环写文件在冻结后停止，两次导出文件集相同；测试结束后没有遗留测试容器或卷。随后增加的文件 HTTP 缺少版本服务测试在本地通过。Black/Ruff 与 diff 空白检查通过。

同日接入迁移 `0005` 和安全保存后的服务器完整回归：280 项，274 项通过、6 项旧 opt-in 沙箱用例跳过。两项真实工作区用例均启用，覆盖网络 HTTP 保存及故障注入。Windows 本地完整集合包含 10 项未提交的历史探索测试：290 项，146 项通过、144 项因数据库/沙箱开关跳过；这不算真实数据库验收。

## 刘婧瑶工作台对接

已核对 `release1_ypd@b3830e1`，API 目录、Editor、FileTree、Terminal 和工程配置已补齐。当前仍默认 mock；TerminalPane 只展示传入的 output，没有连接 WebSocket 或发送键盘输入。

接真实后端时请使用同源 `/api/v1`，HTTP 带 `X-VibeEval-Request: 1`，通过 `/current` 初始化或恢复 Cookie，再兑换 Token。准备使用 `/prepare`，ready 后由考生点击 `/start` 才进入作答。终端使用同源 WS/WSS，将 xterm `onData` 发为 input，FitAddon 得到的行列发为 resize，收到 output 后追加 `term.write`。连接状态跟随 WebSocket 事件，不固定显示已连接；断线不重发输入。后端允许名单填写实际页面 Origin，开发代理启用 WebSocket 转发。

文件读写后端已具备联调条件。前端保存需串行调度，同一文件上一请求结束后再发下一请求，采用返回的新版本号；遇到 409 冲突保留编辑器内容并提示重新读取，不自动覆盖。交卷前立即 flush 并等待所有保存成功，失败则阻止主动交卷并提示用户。不要把 503 当作空文件列表，也不要把模拟公开测试结果显示为真实执行结果。

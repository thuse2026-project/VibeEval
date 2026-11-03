# 一期统一联测版本

## 组成与运行方式

本轮统一候选版本为 PR #8 的 `integration/release1`，新版交互由用户人工验收后再合并 `main`。后端业务以陈弘毅 `codex/sandbox-lifecycle@d41c38e7a7ccce250202adfa58ab4f7745c90da4` 为基线，包含赵晨一文件版本迁移 `0005`、交卷协调器、维护任务及部署配置。候选人前端整体采用刘婧瑶 `feat/candidate-workspace@85d5459` 的规则、准备、工作台、保存队列、提交回执与视觉样式，撤回我们在 `674df07` 额外添加的配色。仅补实际行为修复：原生 Monaco 文件模型隔离、服务器校时、二次冲突保护、终端连接与滚动、失败重读及未保存草稿复制。考官可信评分保留。原分支保留，不逐个重复合入早期分支。

前端使用同源 Cookie/来源头、浏览器初始化锁、真实准备及确认开始、按服务端时间推进倒计时、多文件草稿和串行版本保存、交卷前保存确认、真实 WebSocket PTY，以及提交回执恢复。编辑后等待“已保存”再在终端运行 unittest；采用她的 5 秒自动保存，不提供另一套“运行测试”按钮。默认流程不使用演示 Token、定时器假就绪、假测试通过或假评分报告。

## 服务器预览

复用已验证的助教服务器，不替换 `/home/ubuntu/vibeeval-validation` 下组员原有材料。统一联测文件放在其 `integration-release1` 子目录，使用独立 PostgreSQL 数据库、运行快照和考官测试账号。现有 Python 虚拟环境为 Python 3.14.4；API 与维护进程使用同一套代码及配置，考生镜像仍为 Python 3.12.16、标准库 unittest。

Docker 29.1.3、runsc release-20261026.0、systrap、原有题目镜像均直接复用。题目镜像以 `docker image inspect` 得到的不可变 ID 配置，不抄写其他机器的 ID。考生容器继续使用 UID 1000、禁网、只读根文件系统、512 MiB、0.5 CPU 和 64 PID；维护者参考答案不进入题目镜像。

浏览器入口通过 SSH 隧道使用安全上下文的 localhost：

```powershell
ssh -N -L 5181:127.0.0.1:18080 ubuntu@192.144.156.34
```

打开 `http://localhost:5181/` 是真实候选人工作台；`/admin` 是考官管理；`/entry` 保留入场信息验证页。服务器允许 localhost 和 127.0.0.1 两个精确页面 Origin；人工使用 localhost 可与原 127.0.0.1 本地预览的 Cookie 分开。已有隧道占用 5181 时直接复用，不重复执行命令。当前仅提供受控联测入口，不把公网 HTTP 当成正式部署；正式发布需 HTTPS。

轻量预览入口 `app.preview:app` 在同一个 ASGI 应用内服务构建后的前端和真实沙箱 API，省去本次服务器安装 Node/Nginx 或重复构建基础环境。生产 Compose 仍使用既有 Nginx 配置。其启动条件与真实沙箱入口相同，另需先完成 `frontend/npm run build` 并将 `dist` 放在工程对应目录。

```sh
# 在统一工程目录，使用现有虚拟环境；先在可信 shell 加载私有环境变量。
export PYTHONPATH="$PWD/backend"
python -m uvicorn app.preview:app --host 127.0.0.1 --port 18080 --no-access-log
# 独立维护进程使用同一环境、数据库和代码目录。
python -m app.submissions.maintenance --watch --interval 5 --batch-size 20 --retry-delay 30
```

私有 `.env`、考官哈希文件、Token、运行日志和快照不提交 Git。所有测试资源使用 `vibeeval-integration` 前缀；不删除组员原有镜像、卷或验证目录。

## 已实际验证

- 通过真实网页 Token 入场、prepare 健康检查和考生确认 start，读取完整六文件题目。
- 从网页 PTY 输入 unittest 得到原始 6 通过、2 预期失败；模拟考生在编辑器修复并添加回归测试，真实终端得到 9 项通过。
- 浏览器切文件前确认保存；公开测试文件写入返回 403；另一写入者更新版本后，前端保存冲突保留本地文本，不覆盖远程文件。
- 不同浏览器重复使用已兑换 Token 被拒绝；复制原浏览器恢复凭据后刷新保持截止时间；将浏览器本机时间调快 36 小时仍显示正常剩余时间。
- 点击交卷前的最后一处未保存标记进入固定快照；刷新保持相同提交回执；考官从真实列表打开对应六文件快照；交卷后读/写工作区返回 409。

沙箱冻结导出的 `public_test_output` 仍保留原始 null，原答卷不回写。用户已确认增补独立平台评测：考官可查看相对发布版本的文件差异，点击评测固定快照，获得 8 项公开行为测试的明细、输出与结果分（每项 7.5，共 60）。过程分 40 未评，暂不形成总分。报告另存于快照存储的 `evaluations/`，绑定提交、快照、题目/镜像/规则/基线版本；刷新可读取，失败重试不覆盖已有成功报告。候选 stdout 不作为成绩依据。

当前只支持 `python-debug-01:f898a77`；可信原题与测试封存在 `backend/app/grading/baseline.json`，由已发布 Git 对象生成，不使用随开发变化的 README 作基线。可信测试控制器在宿主子进程执行断言，候选代码只在新建、禁网且资源受限的 runsc 容器内执行，经有限 JSON RPC 返回业务结果。父子进程均有超时和清理。API 仍按既有约定单 worker；本轮使用独立报告文件，未新增共享数据库表或自动评测队列。备份须包含整个快照存储目录。

## 尚未验收的内容

本机 WSL2 的 systrap 存在 seccomp notify 冲突；ptrace 的 PTY 用例失败，不能视为通过。真实验收使用课程服务器 systrap，不能混用这两份结果。完整回归、监督复查及最终提交号更新于根 BCL。

用户已检查上一版完整作答、交卷与考官结果；随后要求采用刘婧瑶最新界面，因此仍须新版人工验收后才合并 main。服务器预览仍是联测环境，不代表公网生产部署、考官账号数据库化或所有后续迭代功能已交付；自动启动、HTTPS 固定入口等部署收尾单独处理。

## 重复执行浏览器验收

先在测试库用现有 `app.exams.provision` 分别生成普通时长和一分钟的全新 Token；已兑换 Token 不能用于新浏览器再次执行。仅在明确选择的测试环境运行，不使用真实考生账号。

在 `frontend` 设置以下私有进程环境变量，再运行 `npm.cmd run test:integration`：

- `VIBEEVAL_E2E_BASE_URL`：受控联测入口，默认 `http://127.0.0.1:5181`。
- `VIBEEVAL_E2E_TOKEN`：普通测试场次全新 Token。
- `VIBEEVAL_E2E_DEADLINE_TOKEN`：一分钟测试场次全新 Token。
- `VIBEEVAL_E2E_ADMIN_USER` / `VIBEEVAL_E2E_ADMIN_PASSWORD`：预配置测试考官账号和密码。

需要本机 Chrome；脚本使用已有浏览器，不要求服务器安装浏览器或 Node。两条测试涵盖真实保存、版本冲突、PTY 测试、交卷，以及截止自动交卷后的未保存草稿复制；提供考官凭据时追加后台快照核对。缺少考生凭据时跳过对应测试，缺少考官凭据时在测试注释中明确后台部分未重跑，不能计作验收通过；失败时的截图和 trace 留在被忽略的 `frontend/test-results/`。

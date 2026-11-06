# 入场共享 PostgreSQL 接入

## 接入版本与职责

分支 `feat/entry-postgres` 整合陈智恒管理端与赵晨一 `feat/submission-coordinator`（`cc2abb8`）共享底座。默认入场配置命令、HTTP Token 兑换、Cookie 恢复、会话归属检查、状态与考官总览均访问同一 PostgreSQL。考官账号和三小时登录会话仍维持原方案，本次不迁移考官登录。

共用 `Invitation` 和 `ExamSession`，未修改共享模型或数据库迁移，未实现沙箱适配器或交卷 HTTP 路由。真实提交列表与详情默认接现有 `SubmissionService`，只读取持久化快照。

## 配置与启动

在仓库根目录复制 `.env.example` 为 `.env`，填写本地数据库密码。所有成员后端使用相同的数据库连接配置，不能各用不同的库联调。

```powershell
docker compose up -d --wait
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
Set-Location backend
..\.venv\Scripts\python.exe -m alembic upgrade head
..\.venv\Scripts\python.exe -m app.exams.provision --candidate-id student-001 --name "测试考生" --exam-id exam-001 --title "会议室预约考核"
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

前端另开终端，在 `frontend` 运行 `npm.cmd ci`、`npm.cmd run dev`。配置命令仅输出一次 Token，请私下保存。考官账号初始化沿用根 README。

## 给沙箱模块

1. 浏览器先 GET `/api/v1/sessions/current` 初始化或恢复 Cookie，再 POST `/verify-token`。HTTP 保持 `X-VibeEval-Request: 1`。Cookie 名称、路径和三十天固定恢复窗口不变。
2. 复用 `app.exams.router.require_candidate_session`。它从共享库验证当前 Cookie 和 session_id 归属；不要再建入场表，不信任前端直接提交的 session_id。
3. 入场事务成功提交后才返回 session_id，因此沙箱模块可立即在同库查到 `ExamSession`。浏览器锁与邀请行锁分别保护同浏览器和同 Token 并发，不重复创建会话。
4. 已有 `GET /sessions/{session_id}/status` 与 `/current` 返回真实 `status`、`started_at`、`deadline_at`，补充 `task_version_id`。入场不创建沙箱，不开始计时。沙箱准备和考生确认开始沿用 `SessionLifecycle`；重复开始不刷新截止时间。
5. 题目发布版本由可信后端配置提供，沙箱 `prepare_sandbox(..., task_version_id=...)` 固定该版本；不要从前端任意接收版本。此分支不新增场次题库配置或开始考试 HTTP 路由，具体发布版本须与实际挂载内容对应。
6. 持久化共享状态原样返回；准备失败、正在交卷和交卷失败不会伪装成未入场。文件/终端权限仍由沙箱侧根据共享生命周期校验。

## 旧数据与错误

旧 `runtime/entry.sqlite3` 是本地开发材料，本次不自动导入、不删除、不双写。旧 Cookie 对应的会话不会凭空出现在 PostgreSQL：联调请用独立浏览器配置或清除本地测试站点 Cookie，使用新库重新配置的 Token。正式历史数据若需迁移，必须另做受控导入，不得将这份说明当成允许清空真实场次。

SQLite 仅保留为显式 `create_app(entry_db=...)` 的旧回归测试适配器；生产入口和 CLI 不再读取 `ENTRY_DB_FILE`，连接故障不回落 SQLite。数据库配置不完整或连接/查询故障返回不含内部凭据的 503。

## 题目权限

题目目录预建空 `tests/test_candidate.py`，考生只编辑内容，不删除重建。公开测试和 tests 目录只读、候选人测试文件内容可写的操作系统权限由沙箱侧配置和验证；平台不能仅靠网页禁用按钮保护公开测试。Python 3.12、unittest，原始八项测试仍应六通过、两预期失败。

## 验收顺序

真实 Token 入场后，同库查询对应会话；原浏览器刷新保持同一编号，其他浏览器拒绝；沙箱准备后状态可见；点击开始后截止时间固定，刷新不重置。最后由沙箱与提交侧验证真实冻结提交，再在考官总览调阅固定快照。本分支自动化结果见 BCL，沙箱端到端与权限专项验收仍需双方完成。

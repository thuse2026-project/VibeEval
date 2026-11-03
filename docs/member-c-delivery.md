# 成员 C 第一批接入交付

正式入口：app.sandbox.runtime.SandboxRuntime，提供 freeze_and_export(view) 和 delete_sandbox(view)。数据类型直接复用 D 的模块；不在子进程中创建数据库连接或获取会话锁。底层 BoundSandboxAdapter 同时提供 ensure_bound(view)，调用者必须持有 D 的 lock_session。

运行环境：Linux x86_64，Docker 29.1.3，runsc release-20261026.0 已实际验证，需在 Docker 注册 runsc；systrap 无需 /dev/kvm。后端需要访问本机 Docker Unix socket，该权限等同可信管理权限，不得传给考生容器。不同宿主不能随意使用相同 backend_ref。

安装依赖：在 backend 下 python -m pip install -r requirements.txt -r requirements-db.txt -r requirements-gvisor.txt。数据库配置沿用根 .env.example 和 D README，运行 alembic upgrade head。gVisor 环境变量见 backend/.env.gvisor.example，须由进程环境提供，不会自动加载该示例文件。

启动（backend 目录）：python -m uvicorn app.sandbox.http_app:create_app --factory --host 127.0.0.1 --port 8000。该显式入口调用现有 create_app(submission_sandbox=...)，不改共享默认入口。

freeze/delete 在 spawn 子进程中建立独立 Docker 客户端，请求超时默认20秒，工作进程响应等待默认45秒，清理工作进程另有最多约1.2秒宽限。超时终止工作进程，不自动 unpause 或删除环境；Docker 已受理的操作可能继续完成，下次按原ID重试。导出最大16MiB，返回普通文本SnapshotFile元组，不返回延迟读取句柄；public_test_output=None。

必须先通过协调器提交 submitting 状态，所有保存和PTY输入必须走 write_guard；本模块不独立替代写入门禁。冻结失败保留环境；删除仅允许submitted，保留数据卷。HTTP状态由D路由处理。

本批没有文件HTTP接口、WebSocket网关、自动到期交卷、卷配额、完整题目健康检查。旧探索代码不包含在本次提交中。创建镜像应通过 booking_template 白名单构建，实际镜像ID保存到计划；ready由可信健康检查后设置，不能将容器running直接视为ready。

验证：backend目录 python -m unittest discover -s tests -v。真实服务器验证脚本在 tests/manual，普通CI不执行。已有真实证据涵盖权限、基础PTY和底层创建冻结导出；本次进程超时包装及HTTP factory仍需真实服务器联合验收。不能把离线测试当作完整交卷验收。

本次核对基线：origin/feat/submission-http@b0a5d8d（已包含入场PostgreSQL接入）。整套后端195项测试，98通过、97跳过，无错误。跳过项未计入真实数据库验证。
真实服务器 runtime_submission_probe.py：spawn 包装的冻结、六文件导出及冻结重试通过；清理调用返回 SANDBOX_OPERATION_FAILED，需定位，不能交付为全部通过。测试视图为替身。故障注入及完整HTTP+数据库端到端仍未完成。不能据此认定超时故障路径已验收。



清理修复复验：Docker日志显示暂停容器强制删除可能等待10秒后进入直接SIGKILL；原10秒请求超时不足。调整为20秒请求/45秒操作等待后，真实runtime_submission_probe全部通过，包括重复冻结、重复删除、保留卷。没有unpause。新增离线测试验证等待超时终止worker、安全错误传播及拒绝远程Docker地址。更长的清理仍可能超时，协调器应按原ID重试；不会因超时撤销已提交答卷。


## 第一批发布范围

正式入口为 SandboxRuntime（提交注入）和 BoundSandboxAdapter（数据库锁内创建）。文件HTTP接口和WebSocket网关尚未提供；ensure_bound仅完成容器绑定，健康检查、ready转换及prepare/start路由仍需继续接入。不要将此分支视为完整考生工作台。

拉取后需安装 requirements-gvisor.txt，或安装已包含它的 requirements-dev.txt。仓库没有提交密钥、服务器登录凭据或本地需求附件。手工验证脚本中的本地镜像ID对应本次测试服务器；其他机器需先构建 booking_template 并替换测试镜像ID，不可直接假定该镜像存在。

### 2026-11-02 准备/开始接口数据库验证

新增 tests/test_preparation_http.py。服务器独立 PostgreSQL16 临时容器上执行迁移0001–0004，真实HTTP TestClient、Cookie入场和会话数据库验证通过：匿名请求拦截；准备前开始返回409；准备后ready且未计时；重复准备只调用一次创建；重复开始返回同一开始/截止时间；原Cookie恢复原会话；其他浏览器prepare/start返回404。Docker调用在该项测试中使用Mock，不将其记为真实沙箱全链路。临时数据库容器与卷已清理。未部署HTTP监听服务。

## 第二批：准备与开始接口

显式沙箱应用新增 POST /api/v1/sessions/{id}/prepare 与 /start，详情见 docs/api.md。入场鉴权、数据库状态和计时直接复用共享实现。服务器临时PostgreSQL上HTTP入场、准备、重复开始和跨浏览器访问测试通过；Docker在该HTTP测试中为替身。真实Docker权限验证为先前独立测试，不混称全链路。

新增 workspace/files.py 是纯策略模块：固定六文件白名单、两文件可写、1MiB UTF-8限制、观察到内容变化后递增整数版本、拒绝过期版本。尚未接持久版本账本、实际安全写入或文件HTTP接口；不能当作已完成安全保存。不会将未实现能力挂为可用接口。

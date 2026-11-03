# VibeEval 候选人 Web IDE（前端）

Release 1.0 MVP 的候选人端工作台，对应需求文档 E.2 中刘婧瑶的交付物：

1. 工作台界面布局：考前规则弹窗、顶栏倒计时与状态栏、文件树、编辑器、终端。
2. 核心交互组件：目录树、Monaco Editor、xterm.js 终端。
3. 5 秒防抖自动保存，未保存/保存中/已保存/冲突/失败状态反馈。
4. 沙箱就绪轮询过渡页、交卷二次确认、Submission ID 回显。

## 启动

```bash
npm install
npm run dev
```

打开 http://localhost:5173 。

## 后端与入口

工作台始终访问同源 `/api/v1`，开发代理默认转发本机 8000 端口。更换后端时，在启动 Vite 前设置进程环境变量 `VIBEEVAL_BACKEND_URL`。不再提供旧的 mock 开关、`VITE_API_BASE_URL` 或 `?screen=` 假页面入口。

- `/`、`/entry`：真实候选人入场与恢复，使用后端配置的一次性 Token 和 Cookie。
- `/workspace`：刘婧瑶最新工作台；编辑后等待“已保存”，再在终端运行题目 README 中的 unittest 命令。
- `/admin`：考官总览与固定答卷调阅；演示来源明确标识，不与真实作答混用。

共享数据库、真实 gVisor 后端、题目镜像和启动步骤见根 README 及 `docs/release1-integration.md`；只有前端无法完成真实作答。浏览器验收脚本在 `tests/`，测试凭证通过私有进程环境变量提供。

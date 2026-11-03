# 候选人工作台

使用既有 React/Vite/TypeScript/ESLint 工程，保留 `/admin` 考官端与 `/entry` 的 Cookie 入场/恢复。

```sh
npm ci
npm run dev
```

打开 `http://127.0.0.1:5173/entry`，输入考务凭证，点击进入 `/workspace`。不要将 Token 放进 URL 或本地存储。开发代理将 HTTP 和 WebSocket 的 `/api` 转发到 `127.0.0.1:8000`。本分支不默认模拟任何考试数据或测试通过结果。

后端必须使用成员 C 的 `app.sandbox.http_app:create_app` 工厂，并先迁移到 `0005`；默认 `app.main` 没有工作区路由。实际 Linux 沙箱、题目镜像和 `SANDBOX_TERMINAL_ORIGINS` 见 [终端交接](../docs/workspace-terminal-handoff.md)。允许名单必须与浏览器地址的 Origin 一致。

工作台先恢复原会话；准备环境后，考生确认开始才调用 `/start`。截止时间来自后端，刷新不会重新计时。可编辑文件和版本来自 `/files`，完整题面读取沙箱 README。测试命令使用题目规定的 Python unittest，不伪造 pytest 通过信息。

Monaco、worker 和 xterm 本地打包，不从公共 CDN 下载编辑器。文件逐个串行保存，5 秒防抖。冲突或结果不确定时保留本地编辑，提供服务器内容比较及明确的取舍按钮。主动交卷前等待全部保存，失败则阻止交卷；交卷网络失败保留幂等键，可查询状态或恢复首份回执。到期后的未保存编辑不会强行写入已关闭工作区，最终快照以服务器为准。

```sh
npm test
npm run lint
npm run build
```

测试覆盖保存竞争、多文件串行、未知结果阻止覆盖与交卷、只读文件和结构化 HTTP 错误。真实浏览器与 gVisor 联合验收另行执行，不将单元测试称为真实沙箱验收。

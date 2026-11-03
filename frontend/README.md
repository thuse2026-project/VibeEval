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

打开 `http://localhost:5173/?token=考务Token`。Mock 模式可省略 Token。

## 后端切换

复制 `.env.example` 为 `.env`，按需修改：

- `VITE_USE_MOCK=true`：使用内存 mock，无需后端即可跑通全流程。
- `VITE_USE_MOCK=false`：切换到真实后端，访问 `VITE_API_BASE_URL`，终端连接 `VITE_WS_BASE_URL`。

接口契约见需求文档 E.3。Mock 开发预览：URL 加 `?screen=workspace|grading` 可直达对应界面。

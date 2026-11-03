# 刘婧瑶：一期候选人工作台对接

正式任务分支为 `feat/candidate-workspace`，临时 `release1_ypd@17bdf1c` 保留。根据主分支的任务分支/PR 规则建立独立工作目录，再接入已发布的队友集成基线 `codex/sandbox-lifecycle@d41c38e`；它包含考务 Cookie、共享 PostgreSQL、快照提交与工作区保存。队友依赖尚未全部进入 main，因此本分支相对 main 也会显示这些既有提交；本人的新增提交只修改前端及交接说明。请先审查/整合依赖，再审查本提交，不能把其他成员的既有实现算作本人的新开发。

## 本轮适配

- 保留考官页面与 CI 的 ESLint、TypeScript、Vite 配置，不覆盖为临时分支工程。
- 复用 `/entry` 的 `/current` 初始化 Cookie 与 Web Locks 串行兑换，不在 URL、localStorage 或日志存入 Token。
- `/workspace` 只恢复已有 Cookie；`admitted/provision_failed` 允许准备，`ready` 由考生确认开始；不把准备成功等同于开考。
- HTTP 为同源 `/api/v1`，附 `X-VibeEval-Request: 1`；WS/WSS 用原 Cookie 和浏览器 Origin，代理启用 `ws: true`。
- 每秒按后端 deadline 显示剩余时间，定期读取真实状态；到期、交卷中与失败冻结状态不开放编辑或终端。
- 六文件来自后端，只有后端允许的文件可编辑；题目和 unittest 命令按会议室工程调整。
- 全部文件共用串行保存队列，自动保存 5 秒防抖；只对 `SESSION_BUSY` 有界重试。版本冲突/503/网络不确定结果保留本地内容，重新读取比较后由考生明确确认，不自动升级版本覆盖。
- Ctrl+C 原样发 input，resize 限制 300×150，长粘贴按 UTF-8 字符边界切分；断线手动重连，不重发按键，折叠终端保留 shell。
- 交卷二次确认，先 flush 保存再提交；保存失败阻止主动交卷。回执 `frozen` 只显示快照成功，不伪称隐藏评测或画像已运行。
- Monaco 资源本地打包；DOMPurify 暂以 3.4.17 override 修补 Monaco 0.57.0 精确锁定的旧版本。

## 用户设计稿适配（2026-11-03）

按用户提供的五份 HTML 设计，适配规则确认、环境准备、工作台、交卷确认和成功回执：蓝白卡片、网格背景、流程侧栏、深色编辑器与终端。样式限定在候选人工作台，不替换队友的考官页面或入场鉴权。没有引入设计稿依赖的远程 Tailwind/Iconify 脚本。

设计稿中的订单题、示例身份、固定倒计时和测试结果均未照搬；使用当前会议室题目与真实会话/文件/提交接口。后端没有准备百分比或步骤日志，因此只显示已确认状态。AI 对话、Diff、隐藏测试、能力雷达、报告发布属于后续迭代，未实现评审报告页，也不在成功页显示虚构评测进度。

本轮复测：现有 4 项 Node 测试、ESLint 和生产构建通过；Chromium 拦截 HTTP/WS 回归通过规则门禁、准备/开始、多文件防抖、刷新保留、409 保留本地并阻止交卷、显式比较恢复、PTY Ctrl+C/resize、交卷前保存和考官登录。已查看桌面和移动规则页、工作台、确认弹窗与回执截图；工作台面向桌面使用，小屏保持横向滚动。构建仍提示 Monaco 工作台包较大。此轮不改变后端接口或公共依赖。

## 先前联调验证记录

实际验证：`npm test` 4 项全部通过；`npm run lint` 无警告；`npm run build` 通过，Monaco 工作台包仍有体积提示；`npm audit --omit=dev` 0 漏洞。完整 audit 在既有 ESLint/minimatch 开发依赖链报告 11 项中等级别提示，未使用 audit force 降级队友工具链。新增无测试框架的 Node 自检，并接入现有 Project CI，不修改后端、数据库、迁移或考官业务规则。

本机 Playwright/Chromium 真实浏览器、拦截 HTTP/WS 测试契约的冒烟回归通过：prepare/start、本地 Monaco 资源、多文件 5 秒保存、冲突保留与比较后覆盖、PTY resize/Ctrl+C、立即交卷前 flush 和考官登录页回归。测试发现 Monaco 切换模型会影响另一文件草稿，已按文件独立挂载并复测通过；测试脚本位于被忽略的 runtime/candidate_browser_smoke.py，不提交模拟考生运行数据。

本机没有用真实 gVisor、数据库和浏览器完整联测，本分支不代表集成 main 闭环验收。公开测试输出仍由沙箱/提交成员提供，不能把任意 xterm 文本当作可信持久化测试报告。到期后台冻结由队友维护进程执行，浏览器不重新延长期限或强行覆盖服务器快照。人工复查与合并继续由成员完成，本轮不合并 main、不删除临时分支、不部署。

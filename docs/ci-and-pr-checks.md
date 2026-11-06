# CI 与 PR 合并检查

工程检查统一在 `.github/workflows/backend-ci.yml`，工作流显示名为 `Project CI`。它在 push、pull request 和手动触发时执行，权限为 `contents: read`，同一分支/PR 的新运行取消旧运行。工作流没有目录过滤，文档或配置 PR 也需要完整检查。

## 本地检查

后端开发依赖及命令见根 README。开启 `VIBEEVAL_RUN_DB_TESTS=1` 后，完整 unittest 在随机 PostgreSQL 数据库运行；CI 继续检查 Black、Ruff、最低 80% 行覆盖率及迁移一致性。

部署验收脚本也由后端检查任务格式化和静态检查，使用同一 `backend/pyproject.toml` 规则；本地从仓库根执行 `python -m black --check backend tests/deployment` 和 `python -m ruff check --config backend/pyproject.toml backend tests/deployment`。

覆盖率同时纳入没有 `__init__.py` 的 namespace 目录，尚未被测试导入的模块也统计为未覆盖，避免遗漏沙箱构建工具或工厂入口；配置依据见 [Coverage.py 说明](https://coverage.readthedocs.io/en/latest/config.html#report-include-namespace-packages)。

前端使用 Node.js 22.13+（22.x）或 24+，CI 固定使用 Node 22。进入 `frontend` 执行：

```powershell
npm.cmd ci --no-audit --no-fund
npm.cmd run lint
npm.cmd run build
```

Linux/macOS 使用 `npm`。`npm ci` 按已提交的锁文件安装，依赖清单和锁文件不一致时失败；修改依赖须同时提交两者。新增 ESLint 相关直接依赖固定版本，既有应用依赖版本保持不变。

`frontend/eslint.config.js` 使用 flat config，检查源码、Vite 配置和 ESLint 配置，排除依赖及构建产物。启用 JavaScript/TypeScript 推荐规则，以及 `react-hooks/rules-of-hooks` 和 `react-hooks/exhaustive-deps` 两项核心规则；未引入 React Compiler 检查或额外代码格式化迁移。`--max-warnings 0` 使警告同样阻止通过。配置依据见 [typescript-eslint 快速开始](https://typescript-eslint.io/getting-started/) 和 [React Hooks 插件说明](https://react.dev/reference/eslint-plugin-react-hooks)。

`build` 执行 TypeScript 检查与 Vite 生产构建，不启动应用或执行考生代码。本阶段没有新增前端组件测试框架，后端基础测试仍运行全部现有用例。

## 工作流任务与汇总

| Job ID | 检查名称 | 内容 |
| --- | --- | --- |
| `backend` | `Backend / Python 3.12`、`Backend / Python 3.13` | 后端检查、PostgreSQL 测试、覆盖率与迁移 |
| `frontend` | `Frontend / Node 22` | 锁文件安装、ESLint、类型检查与构建 |
| `windows` | `Windows snapshot compatibility` | 配置、快照并发和路径兼容 |
| `compose` | `Compose service smoke test` | PostgreSQL/Redis 启动与连接 |
| `application` | `Application Compose persistence smoke` | 构建前后端镜像，验证代理、迁移和容器替换后的会话、账号及答卷恢复 |
| `required` | `Required checks` | 上述全部成功才通过 |

`required` 使用 `needs: [backend, frontend, windows, compose, application]` 和 `if: always()`，即使上游失败也执行状态判断。它只接受 `success`；`failure`、`cancelled` 和 `skipped` 均失败。这样不会因依赖失败导致汇总被跳过。GitHub 对被跳过的 job 可能报告成功，相关语义见 [GitHub 条件执行说明](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-jobs-with-conditions)。更改 Job ID 或增加检查时，必须同步更新汇总依赖和判断。

应用打包任务使用单独的 Compose 项目和测试卷，执行 `tests/deployment/check_stack.py`。普通 CI 只静态校验 gVisor 覆盖配置，实际运行基础应用模式；测试中的答卷为受信夹具生成，不把它计为真实沙箱验收。启动与完整范围见 [应用部署说明](application-deployment.md)。

Actions 均按核验过的完整 commit SHA 固定；Node 缓存以 `frontend/package-lock.json` 为依据，Python 缓存同时包含运行与开发依赖文件。CI 不上传快照、凭证、考生文件或部署产物。

## 主分支规则

`.github/main-ruleset.json` 是可审查的目标配置，**推送该文件不会自动启用规则**。应用后仅保护 `refs/heads/main`：

- 必须通过 PR，至少一位其他成员批准；新增可审查提交后旧批准失效，评审讨论须解决。
- 必须保持与主分支最新代码对齐，并通过 `Required checks`。检查来源限定为已核验的 GitHub Actions App（integration ID `15368`）。
- 禁止强制推送及删除主分支；没有管理员或其他角色的绕过名单。

规则不自动合并 PR，也不更改其他开发分支。具体规则语义见 [GitHub 受保护分支说明](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)，API 配置字段见 [repository rulesets API](https://docs.github.com/en/rest/repos/rules#create-a-repository-ruleset)。

首次应用顺序：先提交并推送本工作流，确认对应提交的全部检查（当前七项，含 Python 矩阵两项）成功；再由具有仓库管理权限的账号应用规则，并通过 API 回读确认目标分支、检查名、来源和审批数。应用前读取已有 ruleset 与传统分支保护；如已有同名规则，仅更新该规则，不覆盖其他配置。当前团队的管理员账号可执行此步骤，Token 不写入配置文件或命令记录。

启用后，尚未包含此工作流的旧开发分支须先同步已审查的共享基础，才能产生必需检查。主分支的合并仍由团队按人工复查流程完成。PR 描述模板已放在 `.github/PULL_REQUEST_TEMPLATE.md`，要求说明最终行为、实际验证和未接入部分；不要把依赖队友的工作当作已完成。

检查失败时在 Actions 中查看对应任务日志，修复后推送新提交；不要为了合并移除必需检查或将失败步骤设为 `continue-on-error`。远程结果及实际强制规则以 GitHub 页面/API 为准，本地验证只说明当前文件可通过检查。

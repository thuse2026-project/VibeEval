# 第一、第二题：TinyDB v1 接入与交付

更新：2026-11-20。已授权将首版题目及答案、评分材料提交至 GitHub，供团队试答和接入。题包已生成并通过源码和构造行为验证；尚未完成人工试答、评分校准或平台端到端联调。

## GitHub 交付状态

题包及换行/摘要修正已推送至 `codex/tinydb-first-two-tasks-v1`；合并请求：[PR #10](https://github.com/thuse2026-project/VibeEval/pull/10)。当前待评审，未合并 main。先前 Git 直连失败已通过本机现有代理恢复；连接器创建 PR 权限不足，改用本机 Git 已登录身份创建成功。

## 仓库入口

- [考生开始说明](../problems/tinydb-pilot-v1/开始试答.md)
- [第一题探索工作区](../problems/tinydb-pilot-v1/01-explore/01-explore.code-workspace)
- [第一题五道题](../problems/tinydb-pilot-v1/02-answer/QUESTIONS.md)与[空白答卷](../problems/tinydb-pilot-v1/02-answer/ANSWERS.md)
- [第二题审阅题面](../problems/tinydb-pilot-v1/03-review/REVIEW.md)与[空白答卷](../problems/tinydb-pilot-v1/03-review/FINDINGS.md)
- [评分点、参考答案及分值](../maintainer/tinydb-pilot/rubric-v1.json)
- [评分执行与复核规则](../maintainer/tinydb-pilot/GRADING-v1.md)
- [缺陷原因、复现序列和正确结果](tinydb-pilot-validation.md)
- [维护者复现与重建说明](../maintainer/tinydb-pilot/README.md)

## 阶段映射

| 题号 | 环节与目录 | 时间 | AI | 提交物 |
| --- | --- | --- | --- | --- |
| 第一题 | 01-explore，正确源码 | 20 分钟 | 只读探索 | 个人笔记；探索环节不独立计分 |
| 第一题 | 02-answer，相同正确源码 | 15 分钟 | 禁止 | 五道文字答案，合计 100 分 |
| 第二题 | 03-review，独立缺陷源码 | 20 分钟 | 禁止 | 缺陷报告，合计 100 分 |

三个工作区是两道题的三个环节，不是三道独立考试题。探索结束只带入考生自己写的笔记，关闭此前对话；第一题答卷固定后进入独立审阅。全部完成后再反馈答案和评分。离线材料由考生手动计时和切换，.mdc 文件仅提供规则提示，不执行强制隔离或计时。

## 配套材料与版本

包编号 tinydb-pilot-v1；来源为 TinyDB 提交 edff96aeb2616402e9489d46e90a592c05565621。三个 repository 目录均含上游 MIT 许可证。探索和答题源码与正确基线一致；第二题仅在 table.py 植入两处逻辑缺陷。逐项说明和验证结果只在维护者材料中。

考生目录中的答卷和反馈均为新生成的空白模板，不复制原本机试答目录里的答案、笔记或聊天。分环节 ZIP 方便单独分发。build_package.py 可以从仓库内基线离线重建，默认拒绝覆盖已有输出目录。

`package-manifest-v1.json` 记录交付文件摘要；`package-checks-v1.json` 记录实际题包行为验证。记录中的本地环境路径仅为运行证据，不是运行前提。上游完整测试结果为选题阶段的 227 通过、1 项可选 YAML 示例跳过，不将其描述为平台集成测试。

## 前后端接入要求

1. 为当前考试配置绑定题包版本，并将三个目录映射为两个阶段中的三个环节。
2. 第一题探索只下发 01-explore 的材料，不能提前下发 QUESTIONS.md；进入独立答题后撤销 Agent 权限，再开放五题和原正确源码。
3. 第二题重新初始化 03-review 的源码；禁止访问上一工作区、远程上游及正确版本差异。
4. 平台负责截止时间、草稿保存、提交冻结和身份权限；不能依靠 Cursor 规则或隐藏按钮保证。
5. 评分服务读取固定提交及 maintainer 中评分标准；各题型独立 100 分。本提交不包含模型 API 调用或新接口，规则 JSON 是维护者评分数据，不应被误认为已经接入的后端配置。
6. GitHub 上提交答案是为了团队协作，目录分开不等于保密。正式考试只部署当前环节的候选材料，剥离 Git 历史与远程，并按既定规则限制外网，不能向考生挂载整个仓库。

平台尚未实现的接入工作应在对应功能分支完成。本次题包提交本身不表示已经跑通整场平台考试。

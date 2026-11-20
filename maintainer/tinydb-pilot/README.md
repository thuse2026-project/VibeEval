# TinyDB 试答维护入口

当前交付：[考生入口](../../problems/tinydb-pilot-v1/开始试答.md)。候选题包 v1 已制作并完成源码、探针、压缩包和配置结构检查。尚未由考生试答，尚未实际操作 Cursor UI 或验证其权限设置。

## 文件用途

- `validate.py`、`validation-results.json`：选题阶段的基线和缺陷行为验证。
- `build_package.py`：从固定源提交构建三个 Cursor 工作区和分环节 ZIP，拒绝覆盖已有交付目录。交付目录已有答案时不得删除或重建。
- `rubric-v1.json`、`GRADING-v1.md`：维护者参考答案和评分说明，不能交给探索 Agent 或考生工作区。
- `package-manifest-v1.json`：首次交付时的文件摘要，答卷及笔记会在试答时变化。
- `package-checks-v1.json`：对打包实际源码执行的验证记录。

## 收回答卷

收到用户“试答完成”后读取交付目录中的 `02-answer/ANSWERS.md`、`03-review/FINDINGS.md` 和 `FEEDBACK.md`。如需分析探索过程，可读取 `01-explore/NOTES.md` 中记录的模型与时间，不假设拥有 Cursor 的聊天记录。

按两个独立的 100 分量表评分，引用实际答案，区分未作答、错误与题面歧义。用户已经接触过缺陷方向，需如实记录熟悉度。先收齐两个阶段答卷再反馈答案。除非用户明确请求，不在试答期间发送提示、揭示参考答案或修改题目。

本地工作区及 .mdc 规则仅为自测约定，不提供强制隔离、倒计时、提交冻结或禁用 AI 的能力。尚未接入 VibeEval 平台或模型评分 API。

## 克隆后的验证与重建

在仓库根目录使用 Python 3.10 或以上版本运行：

```text
python maintainer/tinydb-pilot/validate.py problems/tinydb-pilot-v1/01-explore/repository --output tmp/tinydb-validation-rerun.json
python maintainer/tinydb-pilot/build_package.py
```

重建默认输出到 `tmp/tinydb-pilot-v1-rebuilt/`，目标已存在时拒绝覆盖。默认源码来自已提交的探索包，先核对源文件摘要；无需从 GitHub 再下载源码，也不需要第三方 Python 包。构建会刷新本目录的配套评分表和打包检查记录。不要将整个维护者目录下发考生。

# Agent 接入第四批 B：版本化单文件写入与补丁

状态：2026-11-08，用户已确认，本地提交，未推送。
第四批 A 已确认并提交 `7248d51`。

## 工具与边界

本批继续使用核心原生 ToolCall/ToolResult；新增 `write_file` 和 `apply_patch`，
复用 agent_operations、批准、工作区占用、执行回执与等待恢复。
迁移 `0010` 为操作增加 kind=command/file_edit，既有记录回填 command。

只有服务端启用 confirm_actions/sandbox_auto，且执行器声明文件编辑能力并实现
read_file/resume_workspace 时，才向模型注册编辑工具。
read_only/chat_only 不增加写能力；服务端 deny 仍优先，confirm_actions 始终需要批准。
不允许浏览器提交工具成功回执，执行结果仍由可信后端供应给核心。

当前任务允许修改两个已有 UTF-8 文本文件：`src/booking.py` 和 `tests/test_candidate.py`。
每个调用只修改一个文件，沿用工作区 1 MiB 上限。
新建、删除、重命名、跨文件原子事务及自动文件恢复不在本批范围。
模型可以在同一 Turn 请求多次单文件工具，各自有独立版本、批准与回执。

## 原生输入

整文件替换：

```json
{
  "path": "src/booking.py",
  "expected_version": 3,
  "content": "def available():\n    return True\n"
}
```

`write_file.content` 最多 12000 字符，可为空字符串；不执行内容，不自动运行测试。
NUL、非法 Unicode、只读/越界路径、权限覆盖字段会拒绝。

精确替换补丁：

```json
{
  "path": "src/booking.py",
  "expected_version": 3,
  "edits": [
    {"old_text": "return False", "new_text": "return True"}
  ]
}
```

`apply_patch.edits` 为 1–20 个有序替换块，每个文本最多 12000 字符。
每一步 old_text 必须在该步内容中恰好出现一次；缺失、歧义或重叠匹配会失败，
不会只应用前几个块。old_text 非空，new_text 可为空以删除精确匹配的内容。
不进行模糊匹配、换行归一化或空白修正；中文、emoji、CRLF 和末尾无换行保持原义。
它接受声明的结构化替换块，审批预览以统一 diff 展示。

整个原生 tool arguments 仍受核心 16000 字符限制；各字段上限不扩大总预算。
补丁可用较短的替换块修改较大的文件，结果仍须符合 1 MiB 工作区限制。

## 提议、批准与实际结果

预检读取真实文件并核对 expected_version，保存三份不可变材料：

| 材料 | 含义 |
| --- | --- |
| before_material_id | 基础文件正文，绑定实际工作区版本 |
| after_material_id | 提议结果，标为 proposed_file，不冒充实际落盘文件 |
| diff_material_id | before → after 的完整审阅 diff，标为提议 |

这些内部读取不伪造模型 ReadObservation。
提议只生成一次并绑定原 operation、tool_call、参数、策略、题目/沙箱以及六个发布文件版本和摘要。
恢复时重新核对版本，不能针对后来变化的正文重新解释原补丁。
从预检到取得工作区占用之间也会再次核对数据库版本。

批准继续使用已有 interactions API。request.change 包含材料 ID、基础版本、
修改前后摘要、字节数、changed 和有限 diff_preview；完整内容可分页取回。
拒绝、到期或版本冲突不会开始写入；已保存的提议可继续审阅。

原生终态结果的 change 增加：

失败结果进入模型历史时，沿用核心的 `{status:"error", content:"原结果 JSON 文本"}` 封装；
change 位于内层结果。操作查询中的 file_result 直接返回原结果结构。

| 字段 | 语义 |
| --- | --- |
| applied | true=有匹配的可信写入回执；false=明确在替换前拒绝/取消；null=效果未知 |
| changed | 提议是否与基础正文不同；无变化成功不会增加文件版本 |
| current_version | 结束核查时实际观察到的版本；无法观察时 null |
| current_matches_proposal | 当前观察正文是否等于提议；未观察时 null |
| observed_material_id | 当前实际观察的文件材料，与提议结果分开 |
| diff_preview / diff_preview_has_more | 有限 diff 正文及是否还有余量 |

效果未知时，即使当前字节恰好等于提议，也不推断 applied=true。
反过来，有可信已应用回执但后来文件又被人工修改时，applied=true 与
current_matches_proposal=false 可以同时成立。不会用旧提议覆盖后来的人工内容。
文件版本是观察版本，不根据 base_version+1 猜测某个已经丢失的历史版本。

## 写入、暂停和交卷

复用第四批 A 的命名 runsc helper、私有 /control、一次派发和独立查询/取消。
编辑 helper 只拥有 CHOWN 能力，执行固定平台代码，目标路径在固定脚本中再次校验；
不解释文件内容，不导入工作区代码，不向候选内容授予 root 执行能力。
只读根文件系统、禁网、资源上限和 no-new-privileges 沿用命令 helper。
较大的准备配置分为不超过 32768 字符的 argv 片段，避免单参数长度上限。

开始写入前取得考试生命周期锁，在确认原容器 running 后保存私有暂停归属标记，
再暂停原容器、启动已准备的同一 helper。预先存在的暂停不归本次编辑所有。
原考试进程暂停期间，固定脚本用 NOFOLLOW 打开受控目录与普通单链接目标文件，
复核摘要，将新内容写入同目录临时文件、同步并原子替换，保持 UID/GID 1000 和 0644。
只读发布文件仍不可写。替换前检查取消、超时/截止；异常或丢失回执保留未知结果。

同一考试的占用约束覆盖命令、结构化编辑和人工 API 保存；原人类终端在编辑期间暂停。
长模型请求、提议读取和命令运行期间仍不持有操作事务锁。
编辑的开始/结束有界生命周期调用在考试锁内执行，以保证暂停/恢复与交卷门禁串行。

取得可信 helper 停止证明后，只有持有该操作的暂停归属、当前绑定相符、考试仍在进行
且未到期时才恢复原容器。workspace_resumed 表示本次是否执行了恢复，不表示全部环境状态。
交卷已开始时保持冻结；不能用旧快照中的 in_progress 状态解除暂停。
恢复 RPC 失败会保留占用等待核查，不因超时重放写入。

若数据库在实际替换/恢复后回滚，仍只查询相同 helper 的私有回执并观察当前文件，
不重新 start、不重新应用补丁，也不把当前文件相等当作执行证明。
取得原生结果后才闭合原 tool_call 并续调模型；waiting_operation 沿用第四批 A。

## 查询、材料与上下文

操作列表/详情增加 kind、change 提议元数据及终态 file_result，仍不返回 Docker 句柄。
新增接口前缀沿用
`/api/v1/sessions/{session_id}/agent/conversations/{conversation_id}`：

| 方法 | 后缀 | 作用 |
| --- | --- | --- |
| GET | /operations/{id}/change?part=diff | 返回不可变提议片段；part 为 before/after/diff |

支持 offset=0、max_chars=8000（最多 16000），采用既有 Unicode 字符偏移与 has_more/next_offset。
预览不执行写入、不调用模型，也不伪造自主阅读记录。
批准、advance、取消和中断接口不变。返回的 material_id 可以继续走既有材料接口或
原生 read_material；同一考试的其他对话按已有材料读取归属规则取回，不能跨考生读取。

实际 ToolResult 中的 diff 预览进入窗口和压缩材料清单；
仅返回 ID 的 before/after/observed 材料标为 locator，不声称正文已进窗口。
压缩后保留 compacted 边界与材料 ID，原始文件、提议和回执记录不被摘要覆盖。
结果序列化不超过核心 16000 字符，必要时进一步缩短 diff_preview。

## 验证与升级

测试使用隔离 PostgreSQL、原生 MockTransport 模型及执行器替身，不产生真实模型费用。
覆盖版本化整文件写入、精确/歧义补丁、无变化、批准/拒绝、陈旧版本、丢启动回包、
暂停后启动未知、取消/中断、交卷禁止恢复、未知结果不猜成功及写入后数据库回滚恢复。
另有原生 schema/CRLF/Unicode/尺寸、暂停归属与大配置分片、窗口/压缩清单测试。
Linux/runsc 验收用例覆盖真实原子替换、任务权限、大文件传输及过期摘要拒绝。
当前 Windows 环境不运行这些实机用例，不能据此宣称真实模型/沙箱联合验收已完成。

```powershell
# backend/；临时 PostgreSQL 独立创建并清理
..\.venv\Scripts\python.exe tests/manual/agent_database_probe.py --all-tests
```

升级前停止旧 worker 并执行 Alembic upgrade head；工具指纹变化的旧活动 Run 明确停止。
回退 0010 前必须核查/取消所有未解决的编辑；迁移会拒绝直接丢掉活动编辑的 kind。
本批不新增前端、模型流式、常驻 worker、多文件事务或自动恢复检查点。

2026-11-08 最终验证：完整后端 539 项，524 项通过，15 项真实沙箱用例按配置跳过；
数据库用例全部启用，迁移升降级及模型一致性通过。
本批新增 31 项中 28 项通过，3 项 Linux/runsc 用例待实机验收。
全应用行覆盖率 86.8%，file_changes 模块 97.9%，操作服务 90.6%。
Black、Ruff、依赖检查和密钥扫描通过，临时 PostgreSQL 已清理。

此前运行出现过 Windows 子进程管道/超时异常；原有并发用例保持原条件，
移除临时诊断后最终完整重跑通过。隔离数据库 probe 新增 `--pattern`，
可定向复查某个测试模块，不连接业务数据库。
本批已获用户确认并本地提交，未推送。

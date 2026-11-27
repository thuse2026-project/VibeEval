# 四题考试考生端接口约定 · 第 8.2 节

对接人为陈弘毅（考试流程、草稿、快照与导出）和刘婧瑶（工作台、操作记录、证据读取与展示）。表中“已有”对应 main 的 `1ac1593`，“新增”对应四题版约定。11 月 26 日已在独立预览核对手工保存、记录测试、问答回看与后台导出。普通终端和 Agent 记录仍有缺口。详见[四题考生端验收记录](r2-candidate-acceptance-20261126.md)。

[同一测试会话的完整请求和响应](r2-candidate-api-examples.json)可直接作前端联调样例。按数组顺序阅读。HTTP 样例来自 FastAPI TestClient，使用真实 Cookie 交换和路由、内存流程库、受控时钟和资源替身。文件中 proposed_evidence 为构造样例。verified_evidence 是另一次使用 PostgreSQL 16.2 与 FastAPI 实际取得的响应和评分导出片段；该测试的命令事件和沙箱仍是替身。

所有相对路径以 `/api/v2` 为前缀。沿用同源 Cookie 与 `X-VibeEval-Request: 1` 请求头。入场后先用原浏览器会话调用 `POST /api/v1/sessions/{session_id}/exam-access` 换取 v2 Cookie。时间均为服务端 Unix 秒。候选端只使用本人当前环节的资源路径。

## 一、当前在哪个环节，按钮做什么

**已有** `GET /attempts/current` 用于首次进入、刷新、断线恢复和切换后核对状态。取 `current_stage.ordinal/title` 显示题号，取 `current_phase.key/title` 选择页面，取 `capabilities` 开放工具。倒计时按 `deadline_at - server_now` 计算，题间过渡使用 `transition_deadline_at`。过期后刷新状态，由服务端决定下一步。响应可以含四题名称和进度，不含未来问答的题干、评分维度或私有规则。

| 四题版环节键 | 当前可以做什么 | 新增字段 next_action |
|---|---|---|
| 第一题 explore | 读源码、使用只读 Agent、读写笔记。读取五问返回 403 | finish_phase |
| 第一题 answer | 读源码、读同题笔记、保存五问。探索聊天关闭 | submit_stage |
| 第二题 review | 读独立缺陷源码、填写报告 | submit_stage |
| 第三或四题 coding | 编程、运行终端和 Agent。本环节 questions 接口仅给编程材料 | finish_phase |
| 第三题 interview | 首次取得本题问答，读固定代码和本人本题证据、写答案 | submit_stage |
| 第四题 interview | 同上；按钮显示“提交整场考试” | submit_exam |

`next_action` 表示当前**作答环节的结束操作**。准备、题间等待、封存、整场结束时为 null。入场与下一题的准备和开始仍沿用既有 prepare/start 接口。主动作答结束前先等待所有本地草稿保存成功。

| 操作 | 已有请求 | 成功响应 |
|---|---|---|
| 结束探索或编程 | POST /phases/{当前环节ID}/finish | AttemptView。读真实状态，可能仍在切换 |
| 提交本题或末题 | POST /stages/{当前题ID}/submit | 本题 Receipt。末题再读 current，以整场 completed 显示离场页 |
| 提交响应中断 | GET /attempts/current；需要原回执时 GET /stages/{原题ID}/submission | 已提交返回原回执；正在封存可返回 409 SUBMISSION_IN_PROGRESS |

三个结束动作的请求均使用 `{"request_key":"本次操作的唯一键","expected_state_version":12}`，其中 12 取最近的 current 响应。同一次操作遇到超时或丢包时复用原键和原目标。409 STATE_VERSION_CONFLICT 时重读 current，确认目标仍是当前可操作环节；409 IDEMPOTENCY_CONFLICT 表示同一键被用于不同操作。四题版不提交旧 summary。

**切换时的页面判定**

- current 表示 phase.closing、stage.sealing 或下一环节尚未 active 时，关闭旧输入；编程结束后显示“正在固定代码”，不显示问答倒计时。若 HTTP 很快完成，页面可以直接收到新环节。
- 资源故障时 current 仍可能返回 HTTP 200。结合 `error_code`、`retry_at` 和当前状态提示“固定失败，正在重试”。服务端按原操作恢复；前端轮询 current。题面准备或快照固定失败时都不得开始问答计时。
- 独立问答 active 后以新 deadline_at 开始计时。整场 `status=completed` 后只显示“答卷已提交。感谢作答，可以离场。”考生不等待评分，考生接口不返回成绩。样例 `close_temporarily_failed → interview_after_retry` 展示第三题恢复，`timeout_to_interview` 展示第四题到时切换。

## 二、哪一份草稿已经保存

| 材料 | 读取 | 写入与约定 |
|---|---|---|
| 探索笔记 | GET /phases/{当前环节ID}/notes → Draft | PUT 同路径，传 expected_revision 和 text。第一题两个环节共享一份笔记，答题时改用新环节 ID 读取 |
| 已开放的题干 | GET /phases/{当前环节ID}/questions → {items:[…]} | 仅返回本环节题干及公开的输入格式 |
| 各问答案与缺陷报告 | GET /phases/{当前环节ID}/answers → 按题目 ID 索引的 Draft 对象 | PUT /phases/{当前环节ID}/answers/{item_id}，传 expected_revision 和 value。每个 item 单独计修订号 |

以上路由均已有。首次保存的 expected_revision 为 0。成功响应形如 `{"revision":1,"value":"检查表名与存储的关联。","saved_at":1795150800}`。答案映射里没有该 item 表示尚无已保存版本。第二个窗口继续用修订号 0 写入时会得到 HTTP 409 和 `{"detail":{"code":"REVISION_CONFLICT"}}`。冲突响应不携带服务器正文。前端保留本地输入，GET 最新草稿并展示两个版本，由考生决定采用哪一版。写入响应丢失也先重读，以服务器值核对是否已成功。正在保存、网络失败或冲突均不得显示“已保存”。主动提交前有冲突就停留当前页，到时收最后成功保存的版本。

五问、后两题独立问答的 value 为字符串。第二题约定一个 item，ID 为 `findings`。value 含 `conclusion`（found、none、undecided）及 `reports` 数组。明确未发现问题用 `{"conclusion":"none","reports":[]}`；从未填写则保持无草稿。每条报告有 location、trigger、actual、expected、cause 五个字符串字段。允许草稿暂时缺内容，空字段取空字符串。最多五条，每条五个字段合计最多1200个 Unicode 码点。第一题五问每问最多1500个码点；后两题字数上限以获审公开输入格式为准。非法答案返回 422 ANSWER_INVALID。草稿修订号与整场 state_version 分别用于保存和状态切换，不能混用。

## 三、问答页和评分端如何读到同一份材料

| 接口或数据 | 提供人 | 消费者与约定 |
|---|---|---|
| 新增 GET /phases/{问答环节ID}/frozen-code | 陈弘毅 | 仅当前问答的 read_frozen_code 权限可读。返回 workspace_id、sha256、coding_phase_id、closed_at。sha256 标识固定代码内容，coding_phase_id 标识已关闭的编程环节，closed_at 是服务端封存完成的时间 |
| 已有 GET /phases/{问答环节ID}/files 和 /file?path=… | 陈弘毅 | 刘婧瑶复用文件浏览器。四题问答环节从上述快照读取，文件 readonly=true；服务端拒绝写入、终端和 Agent |
| 新增 GET /phases/{问答环节ID}/evidence?limit=50&cursor=… | 陈弘毅提供冻结数据读取，刘婧瑶接入证据记录与页面 | 问答页分页读本人本题的证据。返回与 frozen-code 相同的 sha256、coding_phase_id，以及 items、next_cursor。每页使用同一冻结证据集；next_cursor 为 null 表示结束 |
| 已有本题回执及受信任的 export_submission | 陈弘毅 | 本题交卷后，导出 content.artifact.sha256 必须等于问答页的 frozen-code.sha256。Receipt.content_sha256 是整份答卷（含文字等）的摘要，两者用途不同 |

**已定证据截止规则。** 编程权限结束时停止接收本环节的新记录。已持久化的记录按服务端序号排序；封存时把最后一条序号和编程环节 ID 与代码摘要一同提交。问答浏览、分页和评分只读该序号以内的记录。若已记录测试开始、尚无完成回执，就展示“截止时未完成”。即使完成回调带着相同的时间戳，也不能事后写回。若已授权的保存先取得锁并成功提交，再关闭环节，则该记录纳入本次封存。

**给记录服务的接入点。** 在可信后端调用 `StageResources.append_evidence(session_id, coding_phase_id, item)`，成功返回本环节的递增序号。为一次测试的开始和结束分别使用不同的事件 ID，二者的 `data.run_id` 相同。相同事件 ID、相同内容在活动期重试返回原序号，内容不一致返回 409 EVIDENCE_ID_CONFLICT。关闭、到时或越题写入返回 409 PHASE_CLOSED。浏览器没有直接写入或自报证据的接口。只记录允许考生回看的内容。

**候选端手工操作记录。** 成功的 `PUT /phases/{coding_phase_id}/file` 会在保存事务中追加 `actor=candidate` 的 `edit` 事件，包含路径和新版本。`POST /phases/{coding_phase_id}/test-run` 接受 `{ "command": "..." }`，仅在 `use_terminal` 权限有效时由后端在当前隔离沙箱、`/workspace` 目录下执行；命令最多1000字符，运行上限30秒，回显最多64 KiB。服务端生成两个不同事件 ID、相同 run_id 的开始和结束事件，并以运行前沙箱文件内容的 SHA-256 作为 `code_version`。这不是客户端自报退出码或输出。Datasette 验证页的“运行并记录测试”调用此接口；将命令复制到交互终端运行不会生成可信 test_run 事件。

目前 Agent 对话/审批和交互终端中的任意命令尚未接入同一采集器。使用文件保存或结构化测试入口时，后端将证据完整性标为 partial / CAPTURE_GAP，不能宣称整题证据完整；待这些来源也纳入采集后再显式标记 complete。

采集方能够覆盖本题所需来源时，调用 `mark_evidence_collection(session_id, coding_phase_id, complete=True)`；发现已知漏采时传 False，之后不能恢复成完整。没有接入采集方时，快照保留 partial / COLLECTION_NOT_CONNECTED；检测到漏采则为 partial / CAPTURE_GAP。两者均须提示复核。记录已开始但尚未完成的测试属于真实执行状态，可与完整采集同时成立。

`frozen-code.evidence` 和最终导出的 `content.artifact.evidence` 均含 coding_phase_id、sequence（含此序号）、completeness 和可选 reason。旧代码答卷可以没有 evidence。评分方读取该固定范围，使用 `app.stage_resources.evidence.page(db, cutoff, after=0, limit=50)` 分页。前端原样传回 next_cursor，仅对当前问答有效。

证据项共用 `id`、`kind`、`actor`、`at`、`data`。kind 取 message、approval、edit、test_run；actor 取 candidate、agent 或 system。对话保留作者与所属对话 ID；审批关联操作 ID 和决定；编辑关联路径和版本；测试记录命令、退出状态、输出或可继续读取的引用，以及实际运行时的代码版本。运行时版本可以早于最终快照。纯手工操作使用同一记录结构。证据采集故障应显式标为不完整，不能用空列表冒充完整记录。证据响应包含 `completeness`（complete 或 partial）及 `reason`（完整时为 null）。缺少完整记录时供考官复核。

鉴权沿用当前问答环节的考生身份和只读授权；旧编程环节 ID 不能当读取凭据。使用过期环节返回 409 PHASE_CLOSED，当前环节缺能力返回 403 CAPABILITY_DENIED。证据读取故障建议返回 503 EVIDENCE_UNAVAILABLE 供重试；私有评分规则、其他题材料均不进入该接口。样例文件中的 proposed_evidence 给出了同题同快照的一页数据。

## 双方第一次对接的验收动作

1. 陈弘毅以同一会话提供探索、切换与答题状态，以及笔记和答案的成功、冲突、重读样例。刘婧瑶用这些样例验证按钮、只读权限和保存提示。
2. 刘婧瑶用一条手工测试回执说明如何记录实际运行时的代码版本，返回该题冻结证据集的一页样例。陈弘毅核对阶段归属、关闭时机及快照摘要。
3. 双方用第三题的一次实际提交对照问答页、证据接口和评分导出。完成这一操作才能记录为真实联调通过。已在本地对读写边界做单独验证，真实采集和考生操作仍由双方联调。正式题面与评分锚点的发布审核沿用方案第9节。

**复验入口。** 在隔离 PostgreSQL 配置下，从 backend 运行 `python -m unittest discover -s tests -p test_stage_resources.py -k interview`。该用例先写入一条编辑和一条测试开始记录，模拟代码导出失败，在重试前投递迟到的结束回执，再恢复问答。它核对两页 HTTP 响应、最终答卷的代码摘要与截止序号，以及评分端实际读出的记录。新数据库表随 Alembic 0013 创建。

## 考官按回执创建和读取评分任务

四题版分支 `codex/r2-grading-jobs` 增加两个考官接口，以 `/api/v1/admin/r2/submissions/{submission_id}` 为前缀。部署此分支时先运行 Alembic 迁移至 `0014_stage_grading_jobs`。该迁移的父版本是包含 `stage_evidence` 的 `0013`。考官房间分支另有同号迁移，整合前需核对两个分支的数据库版本。

- 登录考官后，`POST /grading-job`（带 `X-VibeEval-Request: 1`，无请求正文）按回执建立任务，重复调用返回原任务。服务端核对封存答卷摘要，编程题还从快照读取代码、按固定截止序号读取证据。并发创建只保留一行，重试保留已有分项。
- `GET /grading-job` 返回 `submission_id`、`created_at`、`basis`、`components`。尚未建任务返回 404。两种请求均需考官身份。通过同一回执的 `GET /answer-sheet` 可读取封存题面和答案，按 `basis.answers` 中的环节、题目和修订号定位。修订号为 null 表示没有该问的已保存答案。
- 编程题的 `basis` 含代码摘要、采集完整度、证据截止点、按类型统计的事件数和证据摘要。`test_run` 统计事件条数，一次运行可有开始和结束两条。采集为 partial 时过程分项标记 `needs_review` 并保留原因，初始分数均为 null。此接口准备评分材料；自动评分、人工评定和发布分别由后续步骤处理。

11 月 24 日用两份封存答卷复验了上述接口。代码摘要 `cad56ded780b` 对应一次编辑、一次测试的起止记录；`9c9a5001675f` 对应两次编辑，未收录测试事件。两份任务的问答修订号均与封存答案相符，过程分项均为待复核。验证只从共享库和代码快照读取输入，任务写入连接私有的 PostgreSQL 临时表。共享预览 `743ddc1` 尚未部署评分任务接口。

### 评定第三、四题的客观 70 分

考官对同一回执发送 `POST /evaluate`，请求头沿用 `X-VibeEval-Request: 1`。四题版会按需创建评分任务，按封存镜像和题目版本加载可信题包。读取 `GET /grading-job` 的 `components.objective` 可取得评测状态，以及成功结果中的分组得分、总得分、规则版本和摘要、代码摘要、运行编号及时间。Bug 题核心行为未通过时，该报告的 `cap=50` 供最终合计使用；`score` 始终表示客观分项。

同一规则下重复请求复用完成结果。若此前已写出有效的封存评测报告、任务更新尚未提交，重试会校验并接回该报告。沙箱基础设施失败保留失败状态和空分数，可以重试。已绑定的规则指纹变化返回 409；报告与固定代码、规则或分组不一致返回 503。重试保留问答和过程分项。此接口不发布成绩。

### 读取第三、四题的问答评分建议

考官沿用 `GET /grading-job`，读取 `components.answers`。每问满分 10 分。`suggested` 表示两问的机器建议已通过结构与引用检查，建议分在 `suggestion.score`；`answers.score` 此时仍为空，供后续人工评定和确认发布使用。存在待复核分项时，保留可核对的分项和另一问的得分，本项及受影响的合计为空。引用有效表示能在封存材料中找到出处，对得分理由的判断仍需评分校准。

写入入口只在服务端运行。从已配置四题版的 `backend` 目录执行：

```sh
python -m examples.r2_interview_grading SESSION_ID STAGE_ID PRIVATE_RULE.json
python -m examples.r2_interview_grading SESSION_ID STAGE_ID PRIVATE_RULE.json --delivery ATTEMPT.json
```

第一条命令核对封存答案与已保存草稿、固定代码和截止范围内的事件，按需创建任务并返回 `input_sha256`。规则文件由维护者为本场明确选择，包含 `version`、`prompt_version`、`task_version`、`questions_sha256`（封存问答环节的 `definition.question_ref`）及 `questions`。其中 `q1`、`q2` 各自按分项 ID 列出满分 `maximum`、评分依据 `anchor` 和是否需要文件或事件引用 `evidence_required`。测试量表只用于开发验证。

第二条命令接收可信评分进程的投递。外层记录绑定摘要、尝试序号（1 或 2）、模型、推理档、输出上限、用量和耗时。模型原始 JSON 字符串放在 `response`，超时或服务暂不可用则填 `transient_error`。完整字段见 `backend/app/grading/interview.py` 中的 `Delivery` 和 `Suggestion`。两问分别提交分项、理由、原文摘录、引用、复核原因及合计。事件用 `event_id`，代码用相对路径及从 1 开始、含两端的行号。未作答项的建议只能为 0；已保存答案与封存材料不符会中止读取。纯手工作答可用编辑、测试或代码支持满分建议。

格式、引用或得分校验失败，或模型发生瞬时故障，首次返回 `retryable`，相同配置的第二次仍失败则返回 `needs_review`。重复投递同一编号和内容会读回同一结果，同号不同内容会被拒绝。`attempts` 留存失败原因和调用元数据。此版本负责接收和校验评分结果，真实模型调度、考官接手和最终发布仍待接入。

## 四题工作台与答题页

11 月 27 日的真实预览已接入文件树展开状态保留、当前文件保存与未保存关闭确认、终端高度调整与剪贴板操作，以及独立答题页的 Markdown 阅读和参考区调宽。阅读第二题时默认打开任务说明。确认切换后显示提交状态；服务端受理并关闭环节后执行封存，下一环节就绪后开始计时。实测耗时、考官端与剩余缺口见[考生体验复验](r2-candidate-acceptance-20261126.md#11-月-27-日复验)。

11 月 26 日确认四题均分两段，共 175 分钟。第二题先进入只读 IDE（`inspect`，20 分钟），结束阅读后进入缺陷报告（`review`，10 分钟）。旧会话沿用入场时绑定的定义。第一题探索笔记在文件树显示为 `note.md`，读写使用当前环节的 notes 接口。第三、四题的终端位于编辑器正下方，与编辑区同宽。独立答题页沿用 IDE 顶栏，左侧导航、中间作答、右侧查阅允许的材料。布局已确认，后续美化另行安排。

## TinyDB 两题的本地交接

从仓库的 `backend` 目录运行 `python -m examples.build_tinydb_images`。这个命令需要本机 Docker 已有 `python:3.13-slim`，也可以用 `--base` 指定本机的 Python 镜像。构建只打包清单允许的源码，输出的两个镜像 ID 分别用于 `VIBEEVAL_TINYDB_CORRECT_IMAGE` 和 `VIBEEVAL_TINYDB_REVIEW_IMAGE`。工作区准备时还会逐文件核对摘要；两份镜像即使包含同名文件，也不能互换。

复用 Datasette 预览已有的两个镜像和 `VIBEEVAL_DATASETTE_CONTEXTS`，另将 `VIBEEVAL_R2_INTERVIEW_FILE` 指向选用的问答 JSON 文件。开发联调可用 `backend/examples/r2-interview-draft.json` 中的方案题面。在已配置 PostgreSQL 和 runsc 的联测环境，运行 `alembic upgrade head`，在 `frontend` 执行 `npm run build`，从 `backend` 以 `uvicorn examples.datasette_preview:create_preview --factory` 启动同源预览。

**前两题的浏览器验证。** 在同一考生会话中，第一题探索时可读源码、记笔记，结束探索后才显示五问，此时笔记只读，聊天入口关闭。保存一问并刷新后内容保留。第二题填写一条五字段的审阅报告，刷新后恢复，提交后进入第三题。验证使用实际浏览器、FastAPI、PostgreSQL 和两个镜像的真实文件，工作区生命周期使用测试替身。单独用 Docker 执行了与开考准备相同的材料校验，两次正确配对通过，两次互换均被拒绝。真实 runsc 和模型调用留在联测环境验收。

**整场导航与异常保存。** 第二个浏览器会话中，服务端保存笔记后人为丢弃成功响应，页面通过重读确认了保存结果，刷新也能恢复。该会话随后完成四题。第三、四题在编程期只取编程任务，点击“确认结束编程”后显示本题两道问答。两题各保存两问后，整场提交进入离场页，刷新仍可见提交成功。数据库中四份回执均与封存答卷的摘要一致。后两题使用占位编程材料，过程记录标记为未接入。正式 Datasette 任务执行、真实手工测试回执与证据回看仍按第 8.2 节联合验收。

**考官读取已交卷材料。** 登录后，通过 `GET /api/v1/admin/r2/submissions?kind=text` 查找前两题回执，`kind=all` 查询所有类型，省略时沿用原先的编程答卷列表。`GET /api/v1/admin/r2/submissions/{submission_id}/answer-sheet` 返回考生与题目元数据、回执及封存内容。第一题从 `content.notes` 读笔记，从答题环节的 `questions`、`answers` 读固定题面和已保存回答。第二题从 `answers.findings.value` 读结论与报告。未作答的题目没有答案记录，文字题的 `artifact` 为 null。浏览器交卷的两份材料均已从这个接口读回，并核对整份内容的摘要与回执一致。服务端也可用 `python -m examples.datasette_exam export SESSION_ID STAGE_ID` 导出；对编程题同时输出固定代码文件。

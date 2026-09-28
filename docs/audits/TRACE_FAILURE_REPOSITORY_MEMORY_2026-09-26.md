# 四仓库记忆研究失败 Trace 复盘（2026-09-26）

## 结论

本次未完成“四个项目分别写入 Wiki”。直接阻塞是 **数字校验规则误把排版编号、commit 哈希片段、源码阅读位置当作事实数值**，并且 **具体拒绝原因没有通过工具返回给 Agent**。Agent 因此反复改写整篇文章、回读同一个笼统错误，最后触发无信息增益停止。GitHub 访问失败和检索低效加重了问题，但不是 8 次写入被拒的直接原因。

这是对实际运行的事后分析，不是四个项目的重新研究，也不表示已经修复代码或补齐 Wiki。数据读取使用 SQLite `mode=ro`；没有修改原会话、revision、Wiki 或计划。

## 证据与定位

- 原请求：研究 deepseek harness、codex cli、pi agent、hermes 的记忆系统，并分别写入 wiki。
- Run：`68c69fba-fac3-452e-a334-e208e3f30bc8`。
- Session：`97acd81f-4cb6-49e1-a7e4-81174a307461`。
- Plan：`06826469-7457-4dea-ac39-d811df53f58f`。
- 时间：2026-09-26 21:57:40.806—22:04:05.014（Asia/Shanghai），约 384.2 秒。
- 原始数据库：项目根目录 `sessions.db`；核对 `session_tool_results`、`agent_events`、`wiki_revisions.verification_json`、`agent_runs`。
- [用户粘贴的 Trace 副本](<C:/Users/付同学/Desktop/paper wiki测试/queries/audits/trace-repository-memory-2026-09-26/user-trace.txt>)。
- [数据库证据摘要与校验复现](<C:/Users/付同学/Desktop/paper wiki测试/queries/audits/trace-repository-memory-2026-09-26/evidence.json>)：含工具记录编号、全部拒绝理由、claim 原文、数字规则重放结果、模型调用统计。
- 找到的现有失败经验位置：`.paperwiki/memory/projects/paperwiki-default/topics/failed-attempts.md`，本次追加在 `Manual notes` 下。该目录被 Git 忽略，因此另保存本文作为可版本管理的复盘。

证据等级：数据库结果是本次运行事实；当前代码用于解释机制；下文建议是尚未实施的改进。通过数字校验不等于架构结论已通过语义验证。

## 实际规模与结果

| 项目 | 实际值 |
| --- | --- |
| 工具调用 | 61 次：repository 44、wiki_write 9、read_tool_result 6、task_plan_write 2 |
| 工具失败 | 10 次：仓库访问 2、Wiki 写入 8；其余 51 次成功 |
| 仓库操作 | discover 15、search 12、read 11、open 4、list 2 |
| 模型调用 | 48 次 completed，0 次 model.failed |
| Wiki 交付 | DSH 1 篇 committed 且 verified_readback=true；Codex/Pi/Hermes 3 篇未提交 |
| 运行层状态 | completed / COMPLETED |
| 研究层状态 | insufficient / no_information_gain |
| 最终覆盖 | 9 个问题中 3 个 covered，且仍有 3 个 pending_writes |
| 调用预算 | 研究动作 used_calls=50/max_calls=64，不是 61 个总工具调用的同一口径 |

`completed` 表示这次会话运行收尾成功，不能代表用户交付成功。最终回答如实承认只写入一篇，这一点正确；外层运行状态仍不足以独立表达任务完成度。

## 失败链条

### 一、搜索成功不等于研究推进

前期 15 次仓库发现和 12 次路径/内容搜索消耗了较多动作。#130、#131 的 Codex 查询返回的是外围工具/教程候选，没有直接得到目标官方仓库；后续才直接 open `openai/codex`。搜索偏差造成了改写关键词、重复“继续确认身份”的循环。

#139、#141 都在 `codex-rs` 搜索 `memory`，limit 从 6 改为 8，属于扩大结果窗口，而不是同参数完全重复。返回值明确为 `file paths only`，第一页偏向 app-server/schema 文件。不能把路径命中等同于实现证据，也不能把局部路径窗口当成记忆模块的完整目录。

本次没有仓库 README 正文读取记录。open 成功和固定 commit 只能证明仓库可访问及版本已固定，未完成验收文档要求的 README/官方链接身份核验。这里不据此断言仓库选错，只指出核验链不完整。

### 二、两次外部访问失败

| 工具记录 | 动作 | 实际错误 | 边界 |
| --- | --- | --- | --- |
| #158 | discover Pi 仓库 | HTTP 403，`forbidden_or_rate_limited`，含 retry_after | 单凭该记录不能进一步区分权限限制和配额限制 |
| #170 | read Hermes `agent/memory_manager.py` L1–90 | HTTP 429，`rate_limited` | 没取得正文 span；provider 文档约定不能替代 manager 实现 |

没有证据证明这里是调用参数/schema 错误。两条错误均被正确标记为 operational_error，不能用来支持“该能力不存在”。本轮未调用 checkout/local_shell 替代读取；最终仍存在 Hermes 实现缺口。

### 三、Wiki 的真正拒绝原因是确定性数字规则

调用链：

`RepositoryWikiWriter.write → WikiRevisionManager.commit_card → propose_card → EvidenceVerifier.verify_claim_payload → verify_claim → _numeric_tokens`。

- `system/wiki/repository_writer.py` 把每个 section 的整个 content 转成一条 claim；对应源码正文作为 evidence_excerpt，commit/行号位于来源 metadata。
- `system/wiki/evidence_verifier.py` 对 claim 的所有文本提取数字，再要求这些数字存在于源码正文。没有区分列表序号、标识符、阅读范围、操作诊断和事实数值。
- 正则 `(?<![A-Za-z])[-+]?\d+(?:\.\d+)?%?` 会截取十六进制 commit 中间的数字，也把 `L1-120` 中的 `-120` 识别成负数。
- writer 默认实例化的 verifier 没有注入 llm；本轮全部 checks 的拒绝都是数字规则，不是语义模型判定“整篇文章不可信”。

| 工具记录 | 仓库 / revision 前缀 | 被拒内容及数据库真实原因 |
| --- | --- | --- |
| #175 | Codex / e8166e2c | 三点概述中的 `(3)` → missing `3`；其余四条 claim 通过 |
| #178 | Codex / 04a1a541 | 流程有序列表 `1.`—`7.` → missing `2,3,4,5,6,7` |
| #179 | Pi / 5a38442d | 读取范围 `L1-120`、文件总行数 `453/278` → missing `-120,453/278`，三条 claim 被拒 |
| #180 | Codex / d9cc3393 | 正文放入真实 commit → missing `1949,3805894878023,5,82` |
| #181 | Pi / bd9ce718 | 正文放入真实 commit → missing `069661721,2,23,4,794,8318` |
| #182 | Hermes / 87f5156d | 正文放入真实 commit → missing `11,158,3,5,501,7,70,77`；其余六条 claim 通过 |
| #183 | Codex / d01c7c18 | 流程序号再次触发；另有“未读取范围/404”被当成源码事实核验 |
| #184 | Pi / 78cb055a | 阅读边界再次触发 `-120,278,453` |

重放：仅从当前文件用 AST 取出无副作用的 `_NUMBER_WORDS` 和 `_numeric_tokens`，对持久化 claim 与 evidence_excerpt 计算差集。9 个 revision 的 47 条 claim 中，36 条无缺失数值，11 条有缺失数值，逐条匹配数据库的 supported/unsupported 分布；11 条分布在上述 8 个 rejected revision 中。没有调用网络/模型，也没有执行写入。

重要区别：序号、真实哈希、真实行号被拒属于规则/数据模型不匹配。#183 的 `404` 另有事实问题：本次 session 的工具记录没有对应两个 Codex 路径的 404 请求，因此这句操作经历缺乏 trace 支持，不能把所有被拒文字一概判为正确。规则通过的 36 条同样没有因此完成语义审查。

### 四、错误详情存了，但恢复路径拿不到

`system/wiki/revision.py` 的 propose_card 已把 checks（claim_id、statement、reason、evidence_ids 等）保存进 `wiki_revisions.verification_json`。但 commit_card 只抛：

`revision <id> rejected: unsupported claim(s)`。

`system/wiki/wiki_chat.py` 的 wiki_write 异常分支只返回 `wiki_write_failed + str(exc)`，没有附 checks。因此 #176/#185 再读 #175，也只能看到相同笼统错误；增加 max_chars 无法找回原工具结果里根本没有的信息。

还出现了工具记录的嵌套回读：#187 读取 #185（#185 是对 #175 的回读），#188 读取 #177（#177 是对成功记录 #174 的回读）。这增加包装层而没有新增诊断。#189 为解释 Codex 失败而读取的是 Pi 的 #184，目标也错位。

模型确实改写了内容，并非八次完全相同的重放；但重试没有依据 claim 级拒绝原因，后来加入真实 commit 反而增加误拒。根本问题是诊断与修复动作没有形成闭环。

### 五、无信息增益停止留下未完成交付

最终研究状态：`no_gain_rounds=2`、`max_no_gain=2`、`pending_writes=[openai/codex:memory-system, badlogic/pi-mono:memory-system, nousresearch/hermes-agent:memory-system]`。

控制器正确保留了未完成写入，未将其判为 evidence_sufficient。但错误没有变成可执行修复材料，反复回读和改写后以 no_information_gain 结束。增加工具预算或禁止停止，不能解决隐藏错误，反而可能延长循环。

计划 #173 把“读取实现/测试”标为完成，但同时承认 Hermes manager 未读；之后没有更新计划记录 DSH 成功及三篇拒绝的最终状态。计划状态滞后，不能作为交付回执。

## 为什么消耗 1302.9k tokens

| 模型调用类型 | 次数 | 累计 total_tokens |
| --- | ---: | ---: |
| 工具决策 llm.tool_call | 28 | 782,074 |
| 证据评估 llm.evidence_assessment | 18 | 465,434 |
| 最终回答 llm.stream_invoke | 1 | 55,292 |
| 会话标题 conversation.title | 1 | 106 |
| 总计 | 48 | 1,302,906 |

其中输入 1,229,024 tokens，输出 73,882 tokens；输入缓存命中 306,560 tokens。累计 tokens 包含每轮重发的输入，不等同于唯一正文量、单次上下文长度或未缓存计费量。界面约 66K 的上下文估计与 1302.9k 累计调用量可以同时成立；本次没有上下文溢出证据。

证据评估占累计 tokens 约 35.7%，模型 span 总耗时约 172 秒；评估 prompt 会携带当前 ledger 和已读来源，后期频繁重复传入大段内容。18 次模型评估只形成 9 次成功 apply_assessment；代码存在每检查点最多两次修复调用，说明除常规检查外还发生了未成功应用的评估。最终 invalid_assessments=0 会在成功应用时重置，不能说明历史没有失败。原始模型响应和每次校验异常没有保存在这些 model event 中，无法从现有记录逐次确定具体失败字段，应保留为可观测性缺口。

## 研究质量的独立缺口

- Codex 只读了上下文片段、协议和状态处理器，未读记忆生成/抽取/合并链；三个文件不足以概括整个记忆架构。
- Pi 的 in-memory Storage 与 Agent 长期记忆不是同一个研究问题。只能说明这些后端的行为，不能通过局部阅读推出整项目不存在语义记忆。
- Hermes manager 读取失败，provider 的 docstring 是接口约定，不是调度实现已验证。
- #186 确实补读 Pi session/memory.ts L121–200，包含 close()；因此后续不能机械沿用“close 未读取”的旧状态。
- 最终回复披露了多数范围限制，但主段落仍有“Pi 的 memory 就是内存后端”等强概括。交付成功率和研究完整性应分别验收。

## 原始修复顺序与验收建议（复盘时尚未实施）

1. **优先返回可修复的拒绝详情。** 将 revision_id、claim_id、section、reason/error_code、evidence_ids、missing_numbers 作为结构化结果返回；支持按 revision 读取 checks。验收：第一次被拒即可精确定位 section，不必回读嵌套错误猜测。
2. **分离事实与元数据。** 版本哈希、路径、行范围、total_lines 对来源 metadata 校验；有序列表编号不进入事实数值比较；工具 HTTP 状态对 operational receipt 校验。unknowns 使用已有独立字段。保留对真正错误数值（阈值、字节数、实验结果）的拒绝，不能直接关闭数字核验或盲目忽略所有数字。
3. **按失败原因恢复。** 相同仓库/topic/claim 和错误签名若无新增证据或针对性修改，不再整篇盲试。优先修被拒 section，再继续其他独立项目。read_tool_result 返回原始记录定位，避免无限包装。
4. **减少无效探索和重复评估。** 身份候选与内容研究分阶段；已知候选尽早核验 README/官方链接；路径检索分页/缩小范围，必要时搜索 content。403/429 采用有界退避或替代通道，保留“未取得证据”边界。证据评估记录失败原因与修复次数，并只在证据/交付状态有实质变化时评估相应问题。
5. **区分运行结束与交付成功。** 输出 expected/completed deliverables（4/1）、partial/blocked 状态及 pending_writes；最终回执同步到计划。不要以 completed、计划步骤完成或证据充分替代四个 committed revision。
6. **增加真实失败夹具。** 取本次拒绝 section 做回归：列表编号、commit、行号应得到正确分类；错误业务数值仍拒绝；错误 404 经历不得冒充源码事实；拒绝详情可被 Agent 读到；一个项目限流不阻塞其他三个；四页分别提交并回读才算原任务成功。修复后仍需真实端到端验收，不能仅凭固定决策夹具宣称研究能力达标。

## 可复用经验

遇到 `unsupported claim(s)`，先查持久化 verifier checks，不先假设 span_id 错误或模型结论造假。有效 span 只证明出处；错误必须细到可修改的 claim。将来源元数据、工具执行事实和源码结论放到正确通道；重复的“继续研究”不是进展，继续动作必须带来新证据、精确修复或可验证交付。

## 2026-09-26 后续修复记录

- 已增加仓库专用校验，将可识别的排版编号、commit、行范围、文件总行数与事实数值分开处理；保留无依据数值的拒绝。
- 仓库事实段落增加无工具的批量语义复核。复制来源全文到 evidence_excerpt 不再充当语义支持；复核依据为持久化源码、来源元数据和本任务实际读取/错误记录。校验异常明确返回，不能自动提交。
- `RevisionRejectedError` / `wiki_write` 直接返回失败段落、revision_id、section_id、原因和具体差异；失败草稿保存在 revision verification 中。重试只替换被拒段落，未改的失败输入不再产生新 revision；不变的已通过段落可复用检查。
- 原始错误可通过 result_id 回读；嵌套回读会定位原记录并保留 operational_error 分类。研究状态保留可定位的写入诊断和最近 20 条评估异常历史。
- 回归夹具冻结本次 11 个被拒段落、6 个实际来源片段。10 个格式/元数据问题通过结构检查，未观察到的 404 仍被拒绝；这不构成旧文章语义正确的证明。
- 相关后端测试 **142 项通过**，包含原始 query 的四项目提交/局部修复流程（固定模型与网络夹具）、数字/语义反例、失败反馈、工具契约、论文修订、研究停止、仓库读取及恢复。
- 本次未重新执行真实四项目研究、未补写旧任务的三篇 Wiki、未重启服务。重启后端后，应在新对话用原 query 验收研究范围、四个提交回执和实际成本。GitHub 403/429 的外部访问原因仍可能出现；本次改动保留其状态和上下文，不将其当成源码事实。

### 后续简化：取消正文数字硬校验

用户明确要求数量和事实含义由模型结合上下文判断。已移除原论文路径的数字提取/集合比较，以及仓库路径的数字、commit、行号和文件行数正文正则。前述“10 条格式问题放行、404 由数字门槛拒绝”描述的是旧版行为；现在 11 条草稿均通过真实来源解析后进入模型复核，404 操作经历也由模型对照实际回执判断。

正常流程为：原始 evidence 持久化 → 模型生成带引用草稿 → 无工具模型复核原文与草稿 → 修订提交及回读。宿主保留引用解析、结构化来源版本、资源预算和提交一致性检查。数量换算、计数、语义范围与正文中的来源说明不再由代码匹配字符串裁决；语义标签决定复核结果，不使用未经校准的分数阈值。

回归验证的是原文与草稿完整送达模型、模型拒绝能阻止提交、模型允许的合理转换不会被数字规则拦截。固定模型回复不能证明真实复核准确率；同一模型的自查仍可能重复原有误读。

本轮相关回归 **124 项通过**，覆盖仓库写入/修复、论文证据与修订、工具契约、审批失败和论文入库。真实四仓库 query 未重新运行，后端需重启以加载改动。

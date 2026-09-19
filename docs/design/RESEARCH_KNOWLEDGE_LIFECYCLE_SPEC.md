# PaperWiki 多来源知识维护与长探索任务 Spec

- 状态：Proposed
- 版本：1.1
- 日期：2026-09-20
- 适用范围：PaperWiki 本地单用户版本

## 1. 背景

PaperWiki 当前已经具备论文入库、Evidence ID、Verifier、Revision、审批、版本回滚、Wiki 检索、会话压缩和研究任务进度表。但系统仍有两个未闭环的问题：

1. 多个 AI、论文和网页给出不一致结论时，缺少统一的候选观点、来源可信度、争议状态和当前接受结论；聊天内容容易被复制到不同对话，来源逐渐丢失。
2. 长探索任务只记录论文页是否被打开，没有可靠表示论文是否被有效阅读；模型仍可重复打开高相关页面，也缺少可验证的停止条件。

本 Spec 将 PaperWiki 定位为：

> 把易丢失、相互矛盾的多来源讨论，转化为可追溯、可审查、可回滚的研究状态与论文知识；同时让跨大量论文、跨上下文和跨会话的探索任务能够稳定推进。

## 2. 目标

### 2.1 必须实现

1. 区分原始资料、AI 候选观点、研究状态和已接受 Wiki 知识。
2. 所有候选结论保留来源、作用域、状态和证据关系。
3. 未经一手来源支持的 AI 输出不得自动成为已验证知识。
4. 冲突结论不得静默覆盖；必须保留双方来源和处理结果。
5. 长任务必须先形成结构化研究计划，再由程序分配未读论文批次。
6. 打开论文页不得等同于读完论文；只有结构化阅读记录验收通过才算完成。
7. 任务停止必须由覆盖、证据、信息增益、未决冲突和预算共同决定。
8. Compact、新建会话和服务重启不得丢失任务真实进度。
9. 用户直接粘贴未声明来源的 AI 对话时，系统必须能够保守识别并按候选资料处理。

### 2.2 非目标

1. 不构建通用真理判断器，不承诺 Verifier 能自动判断所有事实真伪。
2. 不与 Codex、Claude Code 或 Pi 竞争通用 Agent 能力。
3. 不引入 Multi-Agent、LangGraph、向量化用户记忆或新的分布式中间件。
4. 不把所有聊天自动写入 Wiki。
5. 不要求一次性支持任意开放域工作流；v1 只服务论文研究任务。
6. 不根据文风猜测内容来自 ChatGPT、Claude 或其他具体模型；没有明确来源时统一记为 `unknown`。
7. v1 不要求与通用 Agent 做同条件对照；先完成并验收 PaperWiki 自身的端到端链路。

## 3. 设计原则

1. **来源优先**：聊天内容和 AI 输出只是候选资料；论文、官方文档等一手来源优先。
2. **状态外置**：模型上下文不是任务真相；计划、批次、阅读记录和争议必须持久化。
3. **模型提议、程序约束**：模型处理语义，Python 控制队列、证据所有权、状态迁移和副作用。
4. **不静默覆盖**：不一致内容必须形成明确关系或保持 `unresolved`。
5. **可恢复而非假装无损**：Compact 可以丢失措辞，但不能丢失任务 ID、证据 ID、结论状态、未决问题和下一步。
6. **先定义验收，再实现功能**：每项能力必须对应可自动检查的场景和指标。
7. **保守识别来源**：格式信号和模型分类只用于判断内容类型；低置信度时保持 `unknown`，不得把来源识别当成事实核验。

## 4. 概念模型

### 4.1 四类内容

| 类型 | 定义 | 是否进入 Wiki |
| --- | --- | --- |
| Source Artifact | 论文、网页、AI 回答、用户笔记、原始会话片段 | 否 |
| Candidate Claim | 从来源中提取的候选观点，带对象、方面、适用条件和来源 | 否 |
| Research State | 当前目标、已接受事实、争议、开放问题、决定和下一步 | 否 |
| Accepted Knowledge | 经过证据门禁或人工批准的长期知识 | 是 |

### 4.2 来源级别

```text
primary_paper / official_document
  > reputable_web
  > ai_output / conversation
  > unsourced_note
```

来源级别只决定默认处理策略，不直接决定结论真伪。

### 4.3 Claim 状态

```text
candidate
  -> supported
  -> disputed
  -> accepted
  -> rejected
  -> superseded
```

任何状态变化必须记录操作者、依据、时间和前一状态。

## 5. 功能需求

### K-01 多来源捕获

系统必须允许保存以下来源：论文、网页、AI 输出、用户笔记和会话片段。每条来源必须包含 `source_type`、`origin`、`project_id`、`content_hash` 和时间。

来源既可以由用户显式声明，也可以由系统对粘贴内容进行保守分类。分类分为两层：

1. Python 先检查格式信号，例如 `User/Assistant`、`用户/助手` 等角色标签、连续的问答轮次和对话分隔符。
2. 格式不足以确定时，模型按照固定 Schema 判断内容是 `ai_conversation`、`ai_answer`、`article`、`user_note` 或 `unknown`，并返回置信度、分段结果和判断信号。

模型分类结果必须经过 Schema 校验。系统只能根据文本中明确出现的名称或外部元数据填写具体提供方；不能仅凭语言风格推断是 ChatGPT、Claude 或其他模型。

```json
{
  "content_kind": "ai_conversation",
  "provider": "unknown",
  "confidence": 0.93,
  "segments": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ],
  "signals": ["alternating_role_labels", "multi_turn_structure"]
}
```

无论来源是用户声明还是系统识别，原始文本都必须原样保存。来源分类可以修正，但修正不得改变原始内容和 `content_hash`。

#### 场景：粘贴另一个 AI 的回答

- Given 用户粘贴一段 Claude 或 ChatGPT 的回答
- When 用户选择“保存为研究资料”
- Then 系统将其保存为 `ai_output`
- And 不直接修改 Wiki
- And 后续从中提取的 Claim 初始状态为 `candidate`

#### 场景：用户未说明来源，直接粘贴多轮 AI 对话

- Given 用户粘贴一段具有连续用户与助手轮次的文本
- And 用户没有说明这是 AI 对话，也没有说明具体模型
- When 分类器基于格式规则或通过校验的模型输出给出高置信度的 `ai_conversation`
- Then 系统保存原始文本并标记 `source_type=ai_conversation`
- And 记录 `origin=unknown`、检测置信度和分段结果
- And 向用户显示可更正的“疑似 AI 对话”来源标记
- And 从中提取的 Claim 只能从 `candidate` 状态开始
- And 不自动修改 Wiki

#### 场景：只能识别为 AI 内容，不能识别具体提供方

- Given 文本结构明显来自 AI 问答
- And 文本和元数据没有出现提供方名称
- When 系统保存该来源
- Then `source_type` 可以为 `ai_conversation` 或 `ai_answer`
- But `origin` 必须为 `unknown`

#### 场景：粘贴内容类型不明确

- Given 文本只有单个 `Q:` 标记或同时具有文章与对话特征
- When 分类置信度低于配置阈值
- Then 系统按 `unknown` 或 `pasted_text` 保存
- And 保留原文，不自动拆分角色
- And 不自动修改 Wiki

### K-02 候选 Claim 规范化

模型必须将候选观点输出为结构化 Claim，至少包含：

```json
{
  "statement": "...",
  "subject": "...",
  "aspect": "...",
  "scope": {},
  "source_id": "...",
  "evidence_ids": [],
  "status": "candidate"
}
```

Python 必须验证枚举、项目作用域、来源存在性和 Evidence 归属，不得只验证 JSON 格式。

### K-03 分层证据门禁

Claim 进入 `supported` 或 `accepted` 前必须通过以下门禁：

1. Evidence ID 存在且属于声明的来源。
2. 关键数字、模型名、数据集名等可确定字段与 Evidence 一致。
3. 语义 Verifier 返回支持，或者人工明确批准。
4. AI 输出若没有一手来源，不得自动进入 `accepted`。

Verifier 返回 `uncertain`、输出解析失败或证据不足时必须保持候选状态，不得默认接受。

### K-04 冲突归组

系统必须只在“同一对象、同一方面、适用条件重叠”的候选之间判断关系。允许关系：

```text
equivalent / supports / complements / contradicts / supersedes / unrelated / uncertain
```

`contradicts`、`supersedes` 和 `uncertain` 必须进入争议或审批流程，不得静默覆盖。

#### 场景：两个 AI 对 KIVI 位宽描述不同

- Given 两条候选 Claim 的 subject 均为 KIVI，aspect 均为 quantization_bits
- And 一条声明 2-bit KV Cache，另一条声明 4-bit weight
- When 系统无法从一手来源同时支持两者
- Then 创建 dispute group
- And 保留双方来源和原文
- And 当前 Wiki 不发生修改

### K-05 Revision 的正确边界

Markdown Diff 只表示文本变化，不代表语义正确。审批界面必须同时展示：

- before / after Markdown；
- 受影响 Claim；
- 新旧 Evidence；
- 关系判断及理由；
- 自动门禁结果；
- 未决不确定性。

所有已提交 Revision 必须支持 rollback。

### K-06 研究状态投影

每个项目必须维护一份独立于 Wiki 和聊天记录的研究状态，至少包含：

```markdown
# Goal
# Accepted Facts
# Disputed Claims
# Decisions
# Open Questions
# Next Actions
```

研究状态可由结构化记录生成人类可读 Markdown。它不得被 Wiki 检索当作论文证据。

### L-01 结构化研究计划

长任务开始时，Planner 必须输出并持久化：

```json
{
  "research_questions": [],
  "dimensions": [
    {"id": "quantization", "minimum_sources": 3}
  ],
  "selection_policy": {},
  "stop_policy": {
    "saturation_batches": 2,
    "max_papers": 30,
    "max_tool_calls": 100
  },
  "deliverable_requirements": []
}
```

Python 必须验证最小覆盖、预算和交付要求。自由文本计划不得作为唯一任务状态。

### L-02 确定性论文分配

长任务必须通过 `research_next_batch` 获取下一批论文。Runtime 必须：

1. 从固定候选集中过滤已完成和正在处理的论文；
2. 优先补足覆盖缺口；
3. 为批次生成稳定 `batch_id`；
4. 默认每批分配 3～5 篇；
5. 同一论文不得同时出现在两个活动批次中。

模型不得通过反复 Top-K 搜索证明“已读完全部语料”。

### L-03 结构化阅读记录

`wiki_open` 成功只表示内容已返回。论文只有在 `submit_paper_reading` 验收通过后才进入 `completed`。

阅读记录必须包含：

```json
{
  "task_id": "...",
  "batch_id": "...",
  "card_id": "...",
  "covered_dimensions": [],
  "claims": [
    {"statement": "...", "evidence_ids": []}
  ],
  "experimental_settings": {},
  "novelty": "new|supporting|duplicate|irrelevant|uncertain",
  "conflicts": [],
  "open_questions": [],
  "needs_follow_up": false
}
```

Python 必须校验 Card、Batch、Evidence 归属和必填字段。

### L-04 重复读取策略

对已经存在有效阅读记录的 Card：

- 默认返回已有记录，不重新展开全文；
- 只有提供 `reopen_reason` 和目标章节或证据目的时才允许重读；
- 重读不得覆盖原记录，必须追加新的检查记录。

去重键必须基于 `task_id + card_id + purpose + section`，不能只依赖工具名与完整参数哈希。

### L-05 停止条件

任务进入综合阶段前必须同时满足：

1. 每个必要维度达到 `minimum_sources`；
2. 所有高优先级论文都有有效阅读记录；
3. 最近 N 个批次没有出现新方法类别或关键冲突；
4. 每个交付要求至少有一个受支持 Claim；
5. 没有阻塞性的未决证据缺口；
6. 未超过预算，或已明确进入 `BUDGET_EXHAUSTED`。

模型可以提议停止，但只有 Runtime 可以把任务迁移到 `SYNTHESIZE` 或 `COMPLETE`。

### L-06 自动续作与恢复

长任务不得受普通聊天 `max_steps=3` 限制。专用 Driver 必须在每个批次结算后，根据任务状态执行以下之一：

```text
dispatch_next_batch
request_targeted_discovery
enter_synthesis
pause_for_user
stop_for_budget
```

Compact、新会话和服务重启后，Driver 必须从计划、活动批次、阅读记录和剩余门禁恢复，不依赖旧工具文本仍在上下文中。

### C-01 上下文组装

长任务每次请求必须包含：

- 研究目标和计划摘要；
- 当前批次的 Card ID、标题和阅读目的；
- 已覆盖维度与缺口；
- 与当前批次相关的既有 Claim 和争议；
- 最近阅读记录；
- 剩余预算和下一门禁。

不得只提供 `opened_count` 而省略当前批次和可恢复进度。

### C-02 Compact

Compact 摘要必须保留：任务 ID、当前阶段、活动批次、已完成数量、覆盖缺口、争议、开放问题和下一动作。原始消息、工具结果和阅读记录继续保存在 SQLite。

### O-01 Trace 与审计

Trace 必须区分：

```text
page_opened
reading_submitted
reading_rejected
reading_reopened
claim_supported
claim_disputed
revision_proposed
revision_committed
task_gate_failed
task_completed
```

最终报告必须能够反查使用了哪些阅读记录和 Evidence ID。

## 6. 状态机

### 6.1 长研究任务

```text
PLANNING
  -> DISCOVER
  -> WAIT_INGEST
  -> BUILD_QUEUE
  -> READING
  -> CHECK_GATES
       -> BUILD_QUEUE
       -> TARGETED_DISCOVERY
       -> SYNTHESIZE
       -> PAUSED
       -> BUDGET_EXHAUSTED
  -> COMPLETE
```

### 6.2 单篇论文阅读

```text
PENDING -> ASSIGNED -> OPENED -> SUBMITTED -> VERIFIED
                    \-> FAILED      \-> REJECTED
VERIFIED -> REOPENED（仅带目的的复核）
```

## 7. 数据变更

在现有 SQLite 上新增或扩展以下逻辑实体：

| 实体 | 关键字段 |
| --- | --- |
| research_sources | project_id, source_type, origin, detected_type, detection_confidence, segments_json, content_hash, raw_ref |
| candidate_claims | subject, aspect, scope, statement, source_id, status |
| claim_relations | left_claim_id, right_claim_id, relation, decision_source |
| dispute_groups | subject, aspect, scope, status, resolution |
| research_plans | task_id, questions, dimensions, stop_policy, deliverable |
| reading_batches | task_id, batch_id, status, assigned_card_ids |
| paper_readings | task_id, card_id, receipt_json, status, prompt_version |
| research_state_snapshots | project_id, structured_json, markdown, version |

现有 `opened_card_ids` 仅保留兼容意义，不再作为“已读完成”的依据。历史任务中的 `opened` 页面迁移为 `legacy_opened`，不能自动生成有效阅读记录。

## 8. 工具接口

v1 只新增以下最小工具集：

| 工具 | 作用 |
| --- | --- |
| research_plan | 创建或读取结构化研究计划 |
| research_next_batch | 分配下一批未读论文 |
| submit_paper_reading | 提交并验证阅读记录 |
| research_task_status | 返回计划、覆盖、批次、缺口和预算 |
| research_reopen_paper | 带明确目的复核已读论文 |
| capture_research_source | 保存 AI 输出、网页、笔记或会话片段为来源 |

普通问答继续使用 `wiki_search -> wiki_open`，不强制进入长任务协议。

## 9. 验收指标

### 9.1 长任务

使用固定 30 篇论文完成一次端到端运行：

| 指标 | 验收线 |
| --- | ---: |
| 唯一有效阅读论文数 / 阅读完成数 | 100% |
| 无理由重复展开率 | ≤ 5% |
| 阅读记录字段完整率 | 100% |
| 必要维度覆盖率 | 100% |
| 报告核心结论 Evidence 覆盖率 | 100% |
| 注入一次服务重启后的恢复成功率 | 100% |
| 未满足门禁却宣布完成 | 0 次 |

### 9.2 多来源知识维护

构建至少 30 组包含一致、补充、冲突和不可判断关系的样例：

| 指标 | 验收线 |
| --- | ---: |
| 来源字段完整率 | 100% |
| AI-only Claim 自动进入 accepted | 0 次 |
| 已知冲突召回率 | ≥ 90% |
| 不确定关系错误覆盖旧知识 | 0 次 |
| Revision 回滚成功率 | 100% |
| 未声明 AI 对话高置信度识别准确率 | ≥ 95% |
| 普通文章被高置信度误判为 AI 对话 | ≤ 2% |
| 无明确依据时猜测具体 AI 提供方 | 0 次 |
| 原始粘贴内容完整保留率 | 100% |

## 10. 实施顺序

### Phase 0：冻结范围

- 停止新增通用 Agent、记忆、Multi-Agent 和 UI 功能。
- 保留现有论文入库、Wiki、Revision 和问答链路。

### Phase 1：长任务最小闭环（P0）

1. 增加 `research_plans`、`reading_batches`、`paper_readings`。
2. 实现 `research_next_batch` 与 `submit_paper_reading`。
3. 将 `opened_card_ids` 从完成门禁中移除。
4. 为长任务增加独立 Driver，不再使用三步聊天循环完成整项研究。
5. 实现覆盖、信息增益和证据缺口门禁。
6. 跑通一次固定 30 篇论文任务。

### Phase 2：多来源知识状态（P1）

1. 支持显式保存 AI 输出和会话片段，并保守识别未声明来源的粘贴内容。
2. 引入候选 Claim 与 dispute group。
3. 生成研究状态 Markdown 投影。
4. 审批页展示来源、关系、不确定性和实际 Diff。

### Phase 3：评测与简历口径（P1）

1. 固化多来源冲突集和长任务集。
2. 增加 AI 对话、普通文章、用户笔记和混合格式的来源分类集。
3. 跑通本 Spec 的端到端链路与故障恢复场景。
4. 只把达到验收线的能力写入 README 和简历。

## 11. 明确删除或降级的旧假设

1. `wiki_open` 成功不再代表论文已读。
2. `opened_count == verified_count` 不再代表可以综合。
3. Verifier 通过不代表事实绝对正确。
4. Markdown Diff 不代表知识语义正确。
5. Compact 摘要不承担任务状态存储。
6. 模型口头声明“完成”不改变任务终态。

## 12. 完成定义

本 Spec 只有在以下条件全部满足时才能标记完成：

1. 数据迁移可重复执行且不破坏现有 Wiki 与 Revision。
2. 所有 MUST 需求有自动化测试或冻结评测样例。
3. 固定 30 篇长任务达到第 9.1 节验收线。
4. 多来源冲突集达到第 9.2 节验收线。
5. 服务重启、Compact 和新会话恢复均通过。
6. README 清楚区分已完成能力、实验能力和已知边界。

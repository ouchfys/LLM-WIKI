# Wiki-native 科研知识编译系统优化方案

- 日期：2026-08-13
- 状态：目标方案；“当前能力”和“目标能力”严格区分
- 项目定位：Wiki-native Evidence-grounded Research Compiler

## 1. 一句话结论

下一版不把项目改造成“更复杂的论文 RAG”，而是把它升级为：

> 一个面向科研论文的 Wiki-native 知识编译系统：新论文进入后，系统根据现有知识结构生成可审查的 Markdown patch，持续更新概念、方法、比较与论文页面；查询首先解析并阅读 Wiki，只有精确事实、表格和争议结论才回到 Docling 原始证据核验。

项目主循环是：

```text
source -> compile -> resolve -> verify -> revise -> lint
```

而不是普通 RAG 的：

```text
source -> chunk -> retrieve -> answer -> forget
```

搜索仍然存在，但它只是 Wiki 规模增长后的导航设施，不是系统的产品定义。

## 2. 当前项目的真实基线

### 2.1 已经实现

- 原始论文上传、异步任务和 OSS/本地文件存储。
- Docling remote 解析以及本地 fallback parser。
- `Extraction -> Distillation -> Review -> Merge` 四阶段论文处理流水线。
- `PaperPage / ConceptPage / MethodPage` Markdown Wiki。
- Markdown-first 主存储，SQLite 保存页面、别名、链接、来源和可重建索引。
- Tool-use Wiki Chat、网页搜索、引用、查询轨迹和 30 题评测。
- 当前数据包括 56 个 Wiki 页面、2,991 个 Wiki chunks、146 个 aliases、46 个页面链接和 111 个来源关联。

这里的“四阶段”是编译流水线中的职责分离，不等同于四个拥有独立状态、工具和自主循环的 Agent。

### 2.2 关键缺口

1. **Docling 被扁平化使用**
   - 当前只请求 Markdown/Text。
   - 5,516 个 `paper_blocks` 全部是 `text`。
   - 表格、图片、公式、bbox、reading order 和真实 page provenance 没进入知识模型。

2. **Merge 还不是真正的 Wiki 演进**
   - 有 create/update/link/skip 的框架，但缺少可靠的页面解析、patch preview、revision、diff 和 rollback。
   - 缺少 claim 级新增、修正、冲突、被取代关系。

3. **Review 不是严格原文核验**
   - Reviewer 读取候选 claim 和候选自带 evidence。
   - 没有根据 `source_span_id` 重新读取原始段落或表格，因此无法独立验证蕴含关系。

4. **Wiki-native 查询已完成第一步，关系展开仍待实现**
   - 已新增确定性的 Wiki Resolver，通过 title、alias、页面 FTS 和元数据评分返回有解释的少量候选。
   - 默认全目录扫描已移除，`wiki_open`（兼容旧名 `wiki_card`）只读取 Resolver 选中的面向用户的 Markdown 正文。
   - 已读取持久化 incoming/outgoing links 供工具控制器有限展开；当前仍缺少 heading/body 独立字段权重、coverage-driven 自动展开和 source audit。

5. **评测只覆盖回答效果**
   - 30 题结果可作回归基线，但不能证明 Wiki 是否正确更新、是否产生重复页、是否保留证据、是否发现矛盾，以及是否优于 raw-document RAG。

## 3. 项目边界：什么使它不是普通 RAG

### 3.1 普通 RAG

```text
PDF
 -> 切分 raw chunks
 -> embedding / BM25
 -> Top-K chunks
 -> LLM 回答
```

其核心产物是检索索引，回答时重新从原始碎片拼知识。

### 3.2 本项目的目标模式

```text
Immutable Sources
 -> Structured Evidence
 -> Wiki Compiler
 -> Markdown Page Patch
 -> Review + Commit + Revision
 -> Wiki Resolver
 -> Page Reading + Link Traversal
 -> On-demand Evidence Audit
 -> Answer / New Patch / Knowledge Gap
```

其核心产物是经过持续维护的 Wiki。索引可以删除并重建，Wiki revision 和来源证据不可丢失。

### 3.3 四条强约束

- **Wiki-first**：概念和综合问题默认读取编译后的页面，不直接检索原论文碎片。
- **Evidence-on-demand**：原文检索只服务精确事实、表格、引用定位和事实核验。
- **Write-back with review**：高价值查询生成 patch proposal，而不是把聊天记录直接当知识。
- **Evolution over accumulation**：新论文优先更新已有页面和 claim，不按论文数量无限新增重复卡片。

## 4. 目标数据架构

### 4.1 五层存储

```text
Layer 1  Immutable Source Vault
         PDF / HTML / image / original metadata / content hash

Layer 2  Structured Evidence Store
         DoclingDocument JSON / text / table / figure / formula
         page / bbox / heading path / caption / reading order

Layer 3  Markdown Wiki
         paper / concept / method / comparison / synthesis
         wikilinks / aliases / claim ids / source refs

Layer 4  Revision & Claim Ledger
         patch / diff / author / reason / source ids
         supported / conflicting / superseded / uncertain

Layer 5  Derived Navigation Index
         title / alias / heading / FTS / optional vector / backlinks
```

事实源优先级：

```text
原始文件与 Docling element
  > 已验证 claim ledger
  > 当前 Markdown Wiki revision
  > SQLite/FTS/vector 派生索引
  > chat answer
```

### 4.2 建议数据对象

```text
source_documents
  id, content_hash, parser, parser_version, process_config,
  original_uri, docling_json_uri, created_at

document_elements
  id, document_id, element_type, page, bbox, heading_path,
  parent_id, reading_order, text, caption, docling_ref

wiki_pages
  id, page_type, title, aliases, current_revision_id,
  markdown_path, summary, status

wiki_revisions
  id, page_id, parent_revision_id, patch, full_markdown,
  reason, source_ids, review_status, created_at

wiki_claims
  id, page_id, statement, status, valid_from, valid_to,
  supersedes_claim_id, confidence

claim_evidence
  claim_id, element_id, relation, verifier_result,
  verifier_reason, verified_at

wiki_links
  from_page_id, to_page_id, link_type, source_claim_id

knowledge_gaps
  id, query, expected_page_ids, missing_evidence,
  frequency, status, resolved_revision_id
```

Markdown 仍是主要阅读和维护界面；claim ledger 不是用数据库替代 Markdown，而是解决 prose 无法可靠表达的 provenance、冲突和时间演进。

## 5. Wiki 编译流程

### 5.1 第一步：结构化读取

Docling remote 改为同时保存 `json_content` 和 Markdown：

- Markdown：供人阅读和生成初始论文页。
- Docling JSON：供系统保留 section、table、figure、formula、bbox 和 page。
- 每次解析保存 parser/version/process config/content hash，保证可重放和可比较。

输出不再只有段落，而是 typed elements：

```text
section_heading
paragraph
list_item
table
table_row
figure
caption
formula
footnote
header_footer
reference
```

### 5.2 第二步：层级编译

借鉴 CodeWiki 的 hierarchical decomposition，但按论文结构组织：

```text
paper
 ├── abstract/problem
 ├── method
 │    ├── architecture
 │    ├── objective/formula
 │    └── training procedure
 ├── experiments
 │    ├── setup
 │    ├── tables
 │    └── ablations
 └── limitations/appendix
```

每个 section compiler 只产出带 evidence ids 的候选内容。上层 compiler 负责跨 section 综合。并行化只用于独立 section，不把“Agent 数量”当创新点。

### 5.3 第三步：页面解析与更新决策

新候选进入 Wiki 前，先执行确定性 resolver：

1. normalized title 匹配。
2. alias 精确匹配。
3. wikilink/canonical slug 匹配。
4. source、DOI、arXiv id 匹配。
5. 内容 fingerprint 和页面类型约束。
6. 只有以上仍不确定时才让模型判断是否同一概念。

输出动作：

```text
create_page
update_page
add_claim
strengthen_claim
challenge_claim
supersede_claim
add_link
skip_duplicate
require_human_review
```

### 5.4 第四步：生成 Wiki patch

Merge 不直接覆盖页面，而是产生：

```text
target page
base revision
markdown patch
affected claims
supporting evidence ids
reason
confidence
```

Patch preview 需要展示：

- 新增/删除/修改的 Markdown 行。
- 哪篇论文触发变化。
- 哪些 source spans 支持变化。
- 是否与已有 claim 冲突。
- 是否需要人工确认。

### 5.5 第五步：独立验证后提交

Verifier 必须使用 evidence id 回读 Docling element，不能只相信 Distiller 提供的证据字符串。

每条 claim 的结果：

```text
supported
partially_supported
unsupported
conflicting
insufficient_context
```

只有受支持的 claim 可以自动进入正式 revision；冲突结论保留双方来源并标记争议，而不是静默覆盖旧内容。

## 6. Wiki-native 查询流程

### 6.1 查询不是默认 chunk retrieval

```text
User Query
  -> Intent & Entity Resolver
  -> Exact page / alias / index resolution
  -> Read compiled Wiki page
  -> Follow selected wikilinks/backlinks
  -> Coverage check
  -> Evidence audit only when required
  -> Answer + page citations + optional source spans
```

### 6.2 四级渐进式访问

#### Level 1：确定性页面定位

使用 title、alias、canonical slug、page type、DOI/arXiv id 和 `index.md`。

适合：

- “什么是 GRPO？”
- “打开 DPO 页面。”
- “有哪些策略优化方法？”

这一层不需要 embedding，也不需要模型扫描全部目录。

#### Level 2：Wiki 内容搜索

只有无法通过名称确定页面时，才搜索编译后的 Markdown：

```text
title + aliases + summary + headings + compiled body
```

FTS5 在这里是可重建的 page resolver/index。它负责词法导航，不定义项目为 RAG。

第一版可以使用页面级/section 级 FTS5：

- title、alias 权重最高。
- heading、summary 次之。
- compiled body 最低。
- 中文使用 trigram 或预分词。
- 返回命中字段和 snippet。

#### Level 3：Wiki link traversal

从已定位页面按问题需要展开有限链接：

- `implements`
- `extends`
- `compares_with`
- `evaluated_by`
- `supported_by`
- `challenged_by`
- `supersedes`

这是真正利用 Wiki 结构，而不是对所有页面做无差别向量 Top-K。

#### Level 4：原文证据审计

仅在以下情况触发：

- 精确数字、超参数和公式。
- 表格查值、排序和计算。
- 用户要求页码或原文位置。
- Wiki 页面存在冲突或低置信 claim。
- Coverage check 判断编译页不足。

Evidence resolver 首先被已选 Wiki 页和 source ids 限定，再在小范围内搜索 Docling elements。即使使用 BM25/dense/rerank，它也是 audit tool，而不是默认回答路径。

### 6.3 Search 与 Answer 分离

借鉴 GBrain 的 `search` / `think` 分层，本项目提供三个明确工具：

```text
wiki_resolve(query)
  返回候选 Wiki pages、alias/link 命中原因

wiki_read(page_ids, sections, link_depth)
  阅读编译页面和有限关联页

source_audit(claim_or_query, source_scope)
  返回原始 paragraph/table/figure/page/bbox
```

不要把三个工具都实现成同一个“全库混合 Top-K”。

### 6.4 查询后的知识回流

回答后判断：

- 现有页面是否缺少重要解释？
- 用户问题是否暴露 knowledge gap？
- 外部检索是否发现值得正式摄入的新来源？
- 回答是否形成可复用 comparison 或 interview page？

只生成 patch proposal；经过 evidence verification 后才能提交。普通聊天记录不自动污染 Wiki。

## 7. 表格是第一类 Wiki 证据

论文场景中，表格不是普通文本 chunk。

### 7.1 编译时

- 保存 table number、caption、page、section、headers、rows 和 Docling element id。
- PaperPage 写入对表格结论的编译性解释，而不是复制所有单元格。
- 重要结果可形成 claim，并绑定具体 row/column evidence。

### 7.2 查询时

概念性问题读取 Wiki 已编译的表格结论；精确数值问题才调用 `source_audit`：

```text
Wiki page identifies paper/table
 -> table resolver
 -> relevant row/column
 -> optional read-only DuckDB calculation
 -> answer with table/page/cell citation
```

跨块表格需要重复表头；解析不确定时标记 uncertainty，不允许模型补全缺失数字。

## 8. Wiki 自维护与健康度

借鉴 Karpathy 的 lint、GBrain 的 gap analysis 和 WeKnora 的 revision：

### 8.1 定期检查

- orphan pages：没有入口和入链。
- duplicate concepts：同一概念多个页面。
- broken links：无目标 wikilink。
- weak claims：没有有效 evidence。
- stale claims：被更新论文或新 revision 取代。
- contradictions：相同主题存在冲突结论。
- missing concepts：反复出现但没有独立页面的概念。
- unresolved gaps：多次被问但无法可靠回答。
- oversized pages：应该拆分但持续膨胀的页面。

### 8.2 输出

维护过程输出 proposal queue，而不是自动大规模改写：

```text
repair_link
merge_pages
split_page
refresh_claim
resolve_conflict
compile_gap
request_source
```

每个 proposal 都要可预览、可批准、可拒绝、可回滚。

## 9. 参考项目引入了什么

### 9.1 Karpathy LLM Wiki

引入：

- immutable raw sources。
- LLM 维护的 interlinked Markdown Wiki。
- schema 驱动的 ingest/query/lint。
- index 与 append-only log。
- 新来源更新已有综合，而不是只等待查询时检索。

本项目深化：把“来源可追溯”细化为 claim-to-Docling-element，并增加可验证 patch 和 revision。

来源：[Karpathy LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)

### 9.2 CodeWiki

引入：

- hierarchical decomposition。
- 大输入的分层、递归综合。
- incremental update，而不是全量重新生成。
- 专用 benchmark 和可视化产物。

本项目转换：代码 module hierarchy 转换为论文 section/table/figure hierarchy；只在结构允许时并行编译，不宣传虚假的多 Agent 协作。

来源：[CodeWiki](https://github.com/FSoft-AI4Code/CodeWiki)

### 9.3 DeepWiki Open

引入：

- generate -> browse -> ask 的完整产品闭环。
- 可配置模型 provider 和缓存生成结果。
- 面向复杂问题的多轮研究体验。
- 文档页面与图示的可阅读展示。

本项目转换：从“代码仓库自动文档”转为“科研知识持续编译”；多轮研究必须以 Wiki coverage 和 source audit 为状态，而不是重复检索。

来源：[DeepWiki Open](https://github.com/AsyncFuncAI/deepwiki-open)

### 9.4 GBrain

引入：

- `search` 与 `think` 分离。
- aliases、auto-link、backlinks 和有限 graph traversal。
- stale、uncited、contradiction、gap analysis。
- 每次搜索显示为什么命中。

本项目转换：`wiki_resolve`、`wiki_read`、`source_audit` 三层工具；检索首先服务页面导航，不把 raw chunks 作为默认知识。

来源：[GBrain](https://github.com/garrytan/gbrain)

### 9.5 WeKnora

引入：

- Wiki 页面 revision、line diff 和 rollback。
- per-upload parser/chunk/multimodal 配置。
- parent-child chunking 和表格处理思路。
- ingestion trace、任务失败重试和 E2E 测试。
- Wiki、关键词、向量和知识图索引可独立开启的架构边界。

本项目转换：只保留科研论文需要的结构化摄入、证据审计、版本和观测，不复制多租户、RBAC、IM 渠道和多数据库平台。

来源：[WeKnora](https://github.com/Tencent/WeKnora)、[CHANGELOG](https://github.com/Tencent/WeKnora/blob/main/CHANGELOG.md)

### 9.6 Docling

引入：

- `DoclingDocument` 结构化表示。
- text/table/picture/formula、hierarchy、bbox 和 provenance。
- native HybridChunker 和表格重复表头。
- JSON 作为无损结构化中间产物。

本项目转换：Docling 不是简历中的解析器名词，而是 claim 级证据定位和表格问答的基础。

来源：[Docling document model](https://docling-project.github.io/docling/concepts/docling_document/)、[Docling chunking](https://docling-project.github.io/docling/concepts/chunking/)

## 10. 修改前后对照

| 维度 | 修改前 | 修改后 |
| --- | --- | --- |
| 项目叙事 | 四阶段 Agent + Markdown Wiki + 工具问答 | Wiki-native 科研知识编译与验证系统 |
| Docling | PDF 转 Markdown/Text | 无损结构证据层，保留 table/figure/formula/page/bbox |
| 核心产物 | Paper/Concept/Method 卡片 | 可验证页面、claim ledger、revision、patch 和知识关系 |
| 新论文入库 | 生成候选并 merge | 分层编译，解析已有页面，生成受证据支持的 patch |
| 查询入口 | 模型扫描卡片目录后读整页 | title/alias/index resolver -> page read -> link traversal |
| FTS/向量 | 容易被讲成混合 RAG 主路径 | Wiki 扩展后的派生导航索引 |
| 原始 chunks | 潜在默认检索对象 | 只作为 evidence audit 的受控 fallback |
| 表格 | Markdown 文本的一部分 | 可定位、可检索、可计算、可引用的第一类证据 |
| Review | 检查候选自带 evidence | 回读真实 Docling element 做独立 claim verification |
| Wiki 更新 | 覆盖/合并页面 | proposal -> diff -> verify -> revision -> rollback |
| 知识图 | 页面链接展示 | 支持 resolver、关系展开、冲突与 supersede 的工作结构 |
| 评测 | 30 题回答得分 | QA + compilation + merge + provenance + evolution 评测 |
| 秋招亮点 | “做了一个更复杂的 RAG/多 Agent” | “让论文知识可持续编译、验证和演进” |

## 11. 评测方案

只评回答质量会把项目重新拉回 RAG。评测必须覆盖 Wiki 的生命周期。

### 11.1 Query benchmark

- Wiki page resolution accuracy。
- Concept/comparison answer correctness。
- source audit precision。
- table cell/aggregation accuracy。
- citation-to-element correctness。
- unanswerable/abstention F1。
- latency、tokens、cost。

### 11.2 Compilation benchmark

准备按时间顺序到达的一组论文，人工标注每篇应该：

- 创建哪些页面。
- 更新哪些页面。
- 新增/加强/挑战哪些 claims。
- 添加哪些 links。
- 避免哪些 duplicate pages。

指标：

- page action accuracy。
- duplicate-page rate。
- claim extraction precision/recall。
- evidence attachment accuracy。
- patch acceptance rate。
- unsupported patch rate。

### 11.3 Evolution benchmark

构造后续论文修正、否定或扩展早期结论的序列，评估：

- contradiction detection recall。
- supersede relation accuracy。
- stale claim detection。
- revision rollback correctness。
- 多次入库后 Wiki consistency。

### 11.4 最重要的对照实验

```text
A  Raw-document RAG：直接检索论文 chunks
B  当前系统：目录选择 + 读取 Wiki 整页
C  Wiki-native resolver：title/alias/index + page reading
D  C + link traversal
E  D + on-demand source audit
F  E + verified write-back and revisions
```

该实验不是为了证明“Wiki 在所有题上都胜过 RAG”，而是回答：

- 哪些问题 Wiki 编译后更快、更稳定、更省 token？
- 哪些精确问题必须回原文？
- 知识库使用一段时间后，累计维护能否减少重复推理？
- 新来源加入后，旧问题答案能否通过 revision 稳定改善？

建议构建 120-150 题 query benchmark，加 10-15 篇按时间进入的 compilation/evolution corpus，并保留从未参与 prompt 调整的 holdout papers。

## 12. 实施路线

### Milestone 0：主叙事与基线（已完成文档收敛）

- 对外只使用“Evidence-first、Wiki-native 科研知识编译系统”这一套项目定位。
- README 区分当前和目标能力。
- 固定 30 题结果与当前数据规模。

### Milestone 1：Wiki Resolver（已完成第一版）

- [x] 实现 title/alias/card-id 和元数据确定性解析。
- [x] 将当前全目录扫描改为 resolver-first。
- [x] 返回 bounded candidates、score、match reason 和 matched alias。
- [x] `wiki_open` query fallback 复用同一 Resolver，并在 tool observation 中保留解析轨迹；`wiki_card` 仅作兼容别名。
- [x] 打开页面时返回有上限的 incoming/outgoing Wiki links，允许控制器在下一步按关系打开页面。
- [ ] 将页面 FTS 从 title/summary 扩展为 title/alias/summary/heading/body 的独立字段权重。
- [ ] 增加 coverage check，自动决定是否展开关系或进入 source audit。

当前真实语料 smoke check：查询 `GRPO` 时，旧路径会向控制器序列化 56 张卡片、约 16,509 个字符；第一版 Resolver 返回 4 个候选、约 2,084 个字符。该结果只说明上下文收敛，不代表回答质量提升，仍需跑固定题集回归。

验收：明确名称问题不调用向量和 raw evidence；页面定位可复现；Top1 不低于当前基线。

### Milestone 2：Docling Evidence Layer（第一版已完成）

- [x] 请求并保存 `json_content`；旧服务不支持时明确记录 degraded 状态。
- [x] 建立 `source_documents/document_elements/document_tables/document_table_cells`。
- [x] 区分 text/table/figure/formula/reference/header_footer。
- [x] evidence 可定位 page、bbox、heading path 和 Docling ref。

已完成合成 Docling fixture 自动化测试，并已用 26 篇 originals PDF 完成 `docling-remote` 重建与复杂表格抽查；人工 gold benchmark 仍是独立的评测边界。

### Milestone 3：Hierarchical Wiki Compiler（第一版已完成）

- [x] section/table/figure 作为带 evidence ID 的分层编译输入。
- [x] 复用确定性 Wiki Resolver 和 alias/page type 去重决策。
- [x] 输出 add/strengthen/challenge/supersede claim 动作。
- [x] 生成 unified page patch 和 affected claims。

已完成 claim strengthen 自动化测试；真实论文时序语料上的 page-action/duplicate benchmark 仍待执行。

### Milestone 4：Claim Verifier + Revision（第一版已完成）

- [x] claim 绑定 paragraph/table-cell evidence ID。
- [x] Verifier 从持久化 evidence store 回读 source span，并检查引用、文本重合和数字一致性。
- [x] revision 保存 parent、完整快照、unified diff、验证结果和来源；提供查询与 rollback API。
- [x] unsupported patch 记录为 rejected revision，不覆盖 canonical Markdown。

当前实现：Verifier 由确定性引用/数字检查与独立 LLM 语义蕴含判断组成，支持跨语言证据；Merge 阶段的 Claim Relation Resolver 先按 entity/aspect/scope 召回候选 claim pair，再判断 equivalent/supports/complements/contradicts/supersedes。它们仍不等价于经过人工标注 benchmark 的形式化事实证明。前端已实现面向冲突对象与来源的 diff 审批，page/bbox 仅作为后端审计数据保留。

### Milestone 5：Source Audit + Table QA（5-7 天）

- `source_audit` 只在 coverage 不足或精确问题触发。
- paragraph/table/figure 定向检索。
- 表格 cell 引用和可选 DuckDB 只读计算。

### Milestone 6：Lint、Gap 与评测面板（5-7 天）

- orphan/duplicate/stale/contradiction/gap 检测。
- query、compilation、evolution 三类 benchmark。
- 展示修改前后消融、失败案例、延迟、token 和成本。

如果秋招时间不足，优先完成 Milestone 1-4。Wiki resolver、结构化 evidence、verified patch 和 revision 已足以形成完整且不同于普通 RAG 的技术故事。

## 13. 秋招展示

### 13.1 一句话介绍

> 我做的不是把论文切块后问答，而是一个 Wiki-native 科研知识编译系统。新论文进入后会更新现有概念和比较页面，所有修改以 Markdown patch 和 revision 保存，并绑定 Docling 原文证据；查询优先解析 Wiki 页面和关系，只有精确事实与表格问题才回原文核验。

### 13.2 五分钟 Demo

1. 展示同一概念已有两篇论文和当前 Wiki revision。
2. 上传一篇对该结论进行扩展或挑战的新论文。
3. 展示 Docling table/figure/page evidence。
4. 展示 compiler 生成的页面 patch、claim 变化和来源。
5. 批准后展示 revision diff 和新的 wikilinks。
6. 问概念题：只读取 Wiki，不检索原论文 chunks。
7. 问表格数值题：触发 source audit，引用具体表格、行列和页码。
8. 问资料中不存在的问题：拒答并写入 knowledge gap。

### 13.3 面试重点

- 为什么 Markdown Wiki 和搜索索引不是二选一：Wiki 是知识产物，索引是可重建导航层。
- 为什么不默认 dense retrieval：明确概念优先走可解释的 title/alias/link resolver。
- 为什么 Review 必须回读原文：候选自带 evidence 不能构成独立验证。
- 为什么表格不能按普通段落切分：行列语义、表头和 cell provenance 必须保留。
- 为什么 revision 比“自动写 Wiki”重要：错误更新必须可审查、可追踪、可回滚。
- 为什么评测新增 compilation/evolution：只看 QA 分数无法证明系统真的会维护知识。

### 13.4 简历要点模板

完成后用真实数据替换括号：

- 设计 Wiki-native 论文知识编译流水线，将新论文映射为可审查 Markdown patches，在 `[N]` 篇顺序入库语料上实现 `[X]%` page-action accuracy，并将重复概念页率降低至 `[Y]%`。
- 基于 DoclingDocument 构建 claim-to-element provenance，保留表格、图示、公式、页码与 bbox，使 citation-to-source accuracy 达到 `[X]%`。
- 实现 title/alias/link Wiki Resolver 与按需 Source Audit，使概念查询 `[X]%` 无需访问 raw chunks，并将平均 token 成本降低 `[Y]%`。
- 建立 Wiki revision、diff、rollback、contradiction 和 knowledge-gap 机制，在演进测试集上达到 `[X]%` contradiction recall 与 `[Y]%` rollback correctness。

## 14. 非目标

- 不把 BM25+dense+RRF 写成项目主叙事。
- 不让所有查询默认访问 raw chunks。
- 不为了“多 Agent”拆分可以确定性完成的步骤。
- 不默认引入 Neo4j；页面链接和 claim relations 先用 SQLite/Markdown 表达。
- 不复制 WeKnora 的多租户、RBAC、IM 渠道和多数据库矩阵。
- 不把图可视化当成知识图检索效果。
- 不在没有 benchmark 前写未经验证的提升百分比。
- 不让聊天回答未经证据审查自动写入正式 Wiki。

## 15. 完成定义

只有同时满足以下条件，项目才能使用新的秋招叙事：

- Wiki Chat 通过 resolver/read/audit 渐进访问，不扫描完整目录，不默认检索 raw chunks。
- Docling JSON 和 typed elements 可重放，table/figure/formula/page/bbox 可查询。
- 新论文能够对已有页面生成 claim-aware patch。
- Verifier 回读真实 source element，而不是检查候选自带文本。
- Wiki 页面支持 revision、diff、rollback 和 source-aware merge log。
- 概念问题能只依赖编译 Wiki，精确问题能按需回到原始证据。
- 至少完成 query、compilation、evolution 三类评测。
- Demo 能完整展示一次“新来源改变现有 Wiki”的生命周期。
- README、简历和架构图只陈述可由代码与评测复现的能力。

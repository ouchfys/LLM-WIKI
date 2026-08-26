# LLM-WIKI

> 将论文编译成可验证、可演进、可回滚的 Markdown Wiki。

LLM-WIKI 是一个面向技术论文与个人研究资料的知识编译 Agent。它把论文编译成干净的论文页与主题页；Claim、Evidence 和版本信息只用于写入核验、冲突判断、提交与回滚，不会作为第二套用户知识产品。

```text
source -> evidence -> claim -> merge -> verify -> revision -> wiki -> resolve
```

![Wiki 对话与工具调用](image/wiki-chat-current.png)

![Markdown 知识库](image/knowledge-vault-current.png)

## 为什么做 LLM-WIKI

技术资料不断增加时，单次问答无法解决三个长期问题：

- 同一个概念散落在多篇论文中，知识没有稳定身份。
- 新论文可能补充、限定或挑战旧结论，但普通文档检索不会维护知识版本。
- 答案可以引用文本，却很难精确处理表格数字、提交失败和来源回溯。

LLM-WIKI 将 Markdown Wiki 作为可读知识产物，将 SQLite/FTS 作为可重建导航索引，将 Docling JSON 与表格单元格作为不可伪造的证据层。查询优先读取已经编译的 Wiki；只有精确数字、表格计算和争议结论才回到原始 evidence。

## 核心架构

```text
PDF / Source URL
  |
  v
Docling Extraction
  |- Markdown / text
  |- typed elements
  `- table cells / page / bbox / heading path
  |
  v
Immutable SourcePacket
  |
  v
Distillation -> evidence-bound claims
  |
  v
Merge-time Claim Relation Resolver
  |- same entity?
  |- same aspect?
  |- overlapping scope?
  `- equivalent / supports / complements / contradicts / supersedes
  |
  v
Evidence Verifier -> Hierarchical Wiki Compiler
  |
  v
Frozen Markdown Revision + unified diff
  |- low risk: commit automatically
  `- conflict / verifier failure: Review Center
  |
  v
Canonical Markdown Wiki
  |- PaperPage / TopicPage
  |- revision / rollback
  `- section search units
  |
  v
Tool-use Wiki Chat
  |- section FTS + multilingual vector -> RRF
  |- wiki_search -> wiki_open
  |- table_query / evidence_lookup on demand
  `- web_search / web_fetch when local knowledge is insufficient
```

## 已实现能力

### Evidence-first 文档处理

- PDF 异步上传、任务进度和重复文件检测。
- Docling remote/local 解析与显式 fallback 状态。
- 保存 `DoclingDocument` JSON、段落、标题层级、公式、表格、单元格、页码、bbox 与 Docling ref。
- Source hash 缓存支持崩溃恢复和重复任务安全重放。

### Claim-aware Wiki 编译

- 生成 `PaperPage / TopicPage` 两类用户页面；历史 `ConceptPage / MethodPage` 仍可读，新写入统一为 TopicPage。
- claim 使用 `subject / aspect / predicate / value / scope` 描述可比较的知识单元。
- Merge 阶段解析新旧 claim 的关系，Compiler 只执行确定的 `add / strengthen / challenge / supersede` 动作。
- 页面正文与系统审计元数据分层：用户阅读知识，系统保留 claim ID、evidence binding、merge history 和 compiler 信息。

### 独立核验与表格计算

- 确定性检查 evidence ID、原文重合与数字归属。
- 独立语义 Verifier 判断 `entailed / contradicted / insufficient`。
- Table Resolver 按 caption/header/row/column/cell 定位真实表格。
- 只读 DuckDB 对选中的表格证据执行受限 `SELECT/CTE`，答案返回 table/page/cell provenance。

### Durable Agent Runtime

```text
QUEUED
 -> EXTRACTING
 -> DISTILLING
 -> VERIFYING
 -> COMPILING_PROPOSAL
 -> AWAITING_APPROVAL
 -> COMMITTING
 -> REINDEXING
 -> COMPLETED
```

- 每次状态转换、checkpoint、模型调用和工具调用写入统一 Agent Trace。
- 每轮 Wiki Chat 也建立独立 `QUEUED -> CHAT_RUNNING -> COMPLETED/FAILED` run；模型、检索、页面读取、表格、证据和 Web 工具复用同一事件表。
- Trace 汇总模型/工具耗时、供应商 Token usage、失败与重试；供应商未返回流式 usage 时明确标记为估算值。
- 只持久化输入输出的长度、哈希和必要结果摘要，不把模型私有思维链作为产品功能展示。
- SQLite lease + heartbeat 避免多个 worker 同时提交同一任务。
- 服务启动及周期恢复扫描可以接管过期 lease。
- 活跃节点通过不可变 PDF 和 Evidence Packet 重放，不恢复 Python 指令指针。
- 审批采用 `pending -> accepted -> committing -> approved`；失败进入可见的 `commit_failed` 并可幂等重试。

### 分层记忆与上下文管理

- 会话历史保留当前任务的短期上下文；稳定偏好、学习目标和兴趣信号作为长期画像持久化。
- 对话中出现的论文、主题和学习事件可形成情节记忆，后续按问题检索少量相关记录，而不是把全部历史重新塞回上下文。
- 画像与情节记忆由系统侧提取和召回，问答 Agent 再结合当前问题决定是否调用 Wiki、表格或 Web 工具。
- 当前实现不宣称完全自主的 Agentic Memory：模型尚不能直接调用 `memory_write / memory_forget`，记忆写入仍受确定性规则与数据结构约束。

### Human-in-the-loop

- 新页面、普通知识新增和证据加强在核验通过后自动提交。
- 只有同一 entity、同一 aspect、重叠 scope 下的高置信冲突/替代，或 Verifier 异常才进入审批。
- Review Center 直接展示冲突对象、双方结论、双方来源、系统建议和 Markdown diff。
- proposal 在审批前不会修改正式 Markdown、alias、source 或 Wiki link。

### Wiki-native 查询

- Wiki Resolver 在页面小节上并行执行 FTS5 精确召回与 Qwen3-Embedding-0.6B 跨语言语义召回，再用 RRF 无模型融合；不再全库扫描 2,000 张完整页面。
- 不强制加独立 Reranker，主 Agent 根据问题从少量候选中选页。
- 查询控制器通过原生 function calling 调用 `wiki_search / wiki_open / table_query / evidence_lookup / web_search / web_fetch / resource_recommend`；`wiki_card` 仅作为旧名兼容。
- 前端实时展示实际搜索、打开页面和表格查询过程，不展示模型私有思维链。

## 当前数据与评测

当前本地语料已经完成一次全量 Docling 重建：

| 数据 | 数量 |
| --- | ---: |
| 原始论文 PDF | 26 |
| Docling elements | 35,008 |
| 结构化表格 | 314 |
| table cells | 15,997 |
| Wiki pages | 64 |
| Wiki search units | 417 |
| embedded search units | 417 |
| supported claims | 155 |
| claim-evidence links | 530 |

30 题 Wiki Chat 回归基线：

| 指标 | 结果 |
| --- | ---: |
| final score | 0.8287 |
| citation grounding | 1.0000 |
| retrieval hit rate | 1.0000 |
| Top1 hit rate | 0.7667 |
| wrong web usage rate | 0.2667 |

240 条零人工 silver benchmark：

| 子任务 | 结果 |
| --- | ---: |
| Semantic Verifier accuracy | 93.57% |
| Table Resolver recall | 94% |
| Table QA end-to-end | 76% |
| Exact single-cell QA | 88.33% |

这些结果用于回归，不等价于人工 gold benchmark 或形式化事实证明。完整口径见 [项目审计](docs/audits/PROJECT_AUDIT_2026-08-13.md) 与 [Verifier/Table QA benchmark](docs/audits/EVIDENCE_WIKI_SILVER_BENCHMARK_2026-08-13.md)。

27 次非缓存、CPU-only Docling 入库记录的延迟基线：

| 阶段 | P50 | P95 | 最大值 |
| --- | ---: | ---: | ---: |
| Docling evidence extraction | 71.52 s | 288.07 s | 409.11 s |
| 完整入库（解析、提炼、核验、合并） | 153.32 s | 396.04 s | 473.82 s |

这是当前本地论文集合的小样本工程基线，不是 Docling 与 MinerU 的通用性能结论。普通论文的典型延迟可接受，但长论文的尾部延迟仍需通过 OCR 快慢路径、有界 worker 队列或 GPU 服务化继续优化。

## 技术栈

| 层 | 技术 |
| --- | --- |
| Frontend | Vue 3、TypeScript、Vite、Naive UI |
| API | FastAPI、Pydantic |
| Agent Runtime | SQLite state machine、checkpoint、lease、heartbeat、trace |
| Document AI | Docling remote/local、PyMuPDF fallback |
| LLM | SiliconFlow OpenAI-compatible Chat Completions |
| Knowledge source of truth | Markdown Wiki |
| Metadata / retrieval cache | SQLite、FTS5、Qwen3-Embedding-0.6B、RRF |
| Table analytics | Pandas、read-only DuckDB |
| Object storage | Local filesystem / Aliyun OSS |

## 快速启动

### 1. 安装依赖

```powershell
python -m pip install -r requirements.txt
cd frontend
npm install
cd ..
```

### 2. 配置环境

```powershell
Copy-Item .env.example .env
```

至少配置：

```env
SILICONFLOW_API_KEY=your-key
SILICONFLOW_SUMMARY_MODEL=your-summary-model
SILICONFLOW_REVIEW_MODEL=your-review-model
SILICONFLOW_MERGE_MODEL=your-merge-model
WIKI_EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B
WIKI_VECTOR_SEARCH_ENABLED=true

DOCLING_MODE=remote
DOCLING_BASE_URL=http://127.0.0.1:5001
DOCLING_TIMEOUT_SECONDS=900
```

### 3. 启动 Docling

```powershell
docker compose -f .\docker-compose.docling.yml up -d
```

### 4. 启动 API 与前端

```powershell
python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

另开一个终端：

```powershell
cd frontend
npm run dev
```

- Web：`http://127.0.0.1:5173`
- API：`http://127.0.0.1:8000`
- Health：`http://127.0.0.1:8000/api/health`

## 演示路径

不重新导入论文也可以完整展示：

1. 在“对话”中用中文询问英文论文主题，观察 `wiki_search -> wiki_open` 的实时工具轨迹与命中小节。
2. 在“知识库”打开 Paper/Topic 页面，查看只保留知识正文的 Markdown 页面。
3. 在“冲突审批”查看新旧 claim、冲突对象、来源和 frozen diff。
4. 在“评测”查看固定题集、失败样例和指标口径。
5. 通过 Agent Run API 展示 checkpoint、lease、恢复和 commit retry。

## 常用命令

```powershell
# 导入少量论文
python scripts/ingest_paper_corpus.py --limit 3 --no-maintenance

# 从 Markdown 重建 SQLite/FTS 索引
python scripts/reindex_wiki_markdown.py --wiki-dir wiki

# 检查/执行可扩展 Wiki 数据迁移（默认 dry-run）
python scripts/migrate_scalable_wiki.py
python scripts/migrate_scalable_wiki.py --apply --embeddings --vacuum

# 全量测试
python -m pytest -q

# 前端生产构建
cd frontend
npm run build
```

## 目录

```text
backend/                  FastAPI API、任务恢复与审批接口
frontend/                 Vue 研究工作台
system/agent_runtime/     状态机、checkpoint、lease、trace
system/document/          Docling 与结构化 evidence
system/wiki/              Resolver、Verifier、Compiler、Revision、Table QA
system/search/            Web Search、Web Fetch、资源推荐
system/storage/           本地/OSS 对象存储
scripts/                  入库、重建和 reindex 工具
test/                     Runtime、Wiki、pipeline 与 evaluation
docs/                     当前架构、审计与运行手册
```

## 已知边界

- 旧 claims 没有完整 `aspect/scope` 时使用页面标题和词面候选兼容；新入库数据直接生成结构化 claim。
- Verifier 和 Claim Relation Resolver 尚无大规模人工 gold benchmark。
- Table QA 尚缺单位自动换算与跨论文表头本体映射。
- 首次 CPU-only Docling 解析长 PDF 仍可能需要数分钟；重复任务可命中 Evidence Packet 缓存。
- Markdown、SQLite 与搜索索引不能共享全局 ACID 事务；系统通过 frozen revision、幂等副作用、`commit_failed` 和 retry 实现最终一致。
- 当前向量检索是 SQLite BLOB 上的单机精确余弦计算，适合当前单用户、中小规模知识库；扩展到大规模或多用户前需压测，并替换为 pgvector、Qdrant 或 OpenSearch 等 ANN 检索后端。
- 前端尚未提供 revision 历史与 rollback 操作，后端 API 已支持。

## 设计原则

- 原始资料不可变，结论必须能回到持久化 evidence。
- Markdown 是可读、可编辑、可 diff 的知识主产物。
- SQLite/FTS 是可重建派生索引，不是事实源。
- LLM 负责提议和关系判断，Python 负责验证、状态迁移和副作用。
- 低风险变更自动提交，高风险知识演进才进入人工审批。
- 只在代码和评测落地后声明能力。

## 文档

- [当前架构与设计边界](docs/design/EVIDENCE_FIRST_RESEARCH_WIKI_PLAN.md)
- [可扩展 Wiki 简化改造实施文档](docs/design/SCALABLE_WIKI_SIMPLIFICATION_IMPLEMENTATION.md)
- [论文编译运行手册](docs/runbooks/paper_pipeline_markdown_first_runbook.md)
- [Wiki Maintenance 运行手册](docs/runbooks/WIKI_MAINTENANCE_AGENT_RUNBOOK.md)
- [完整项目审计](docs/audits/PROJECT_AUDIT_2026-08-13.md)

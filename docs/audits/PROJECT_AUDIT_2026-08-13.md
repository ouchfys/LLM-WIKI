# Project Audit — 2026-08-13

## 结论

项目已完成 `source -> compile -> semantic verify -> revise -> resolve -> table compute` 的后端闭环，可以定位为 **Wiki-native evidence-grounded research compiler**，而不是普通的 PDF chunk RAG。

本次已启动 Docker Desktop 与 Docling，清除旧派生入库结果，并用 `sources/papers/originals` 的 26 篇 PDF 重建知识库。最终 26/26 个 source packet 均为 `docling-remote`，不存在 fallback parser 来源。

## 最终数据验收

| 项目 | 结果 |
| --- | ---: |
| 原始 PDF | 26 |
| Docling source documents | 26 |
| Docling elements | 33,232 |
| tables | 297 |
| table cells | 14,125 |
| Wiki pages | 63 |
| Wiki chunks | 4,328 |
| supported claims | 154 |
| claim-evidence links | 527 |
| revisions | 90 committed |
| Agent runs / ingestion jobs | 26 completed / 26 done |
| source hash duplicates | 0 |
| unlinked sources | 0 |
| active dangling evidence | 0 |

SQLite `integrity_check` 为 `ok`，`foreign_key_check` 无违规。最终 Wiki Validator 为 `0 errors / 0 warnings`。

## 本次解决的问题

### Semantic Verifier

- 在 evidence ID、文本重合和数字一致性检查后，调用独立 review model 输出 `entailed / contradicted / insufficient`、score 与 reason。
- 显式检查否定、条件、比较方向、作用域、因果关系及数字所属对象。
- SiliconFlow 结构化调用关闭 thinking，避免推理 token 用尽后 content 为空；非 SiliconFlow LLM 自动回退兼容接口。
- 支持中英跨语言 claim/evidence：只有存在语义 verifier 时才允许越过词面重合阈值。
- Revision 回读持久化 semantic verdict，同时重新检查 evidence 是否存在及数字是否来自原文。
- 部分 claim 不通过时仅剔除失败 claim并保留审计，不再让一个坏 claim 阻塞整篇论文。

当前重建库有 154 个 supported claims，全部具有 evidence link，且不存在 dangling evidence。另已建立 140 条零人工、独立强模型二次裁决的 semantic silver benchmark：当前 Verifier 门禁准确率 93.57%，false accept rate 8.49%，false reject rate 0%。这代表已有可复现回归基线，不代表通过公开 NLI benchmark 或达到形式化证明能力。

### Table Resolver、DuckDB 与 Table QA

- Table Resolver 对 source title、caption、section、headers、cells 加权定位表格，可按 Wiki card 或 source packet 限定范围。
- 把 Docling cell 规范化为 `row_label / column_label / value / numeric_value / page / bbox / cell_id`。
- 只读内存 DuckDB 只接受 SELECT/CTE，关闭 external access，并拦截写操作、ATTACH/COPY 与 `read_*` 外部读取函数。
- SQL planner 使用真实规范化 row/column labels，要求保留 provenance 列；空结果或非法 SQL 自动回退到 cell ranking。
- Wiki Chat 已接入 `table_query` 工具，并提供 `POST /api/wiki/evidence/table-query`。

真实 golden：在《Attention Is All You Need》Table 2 中查询 Transformer (big) 的 WMT 2014 EN-DE BLEU，返回 `28.4`，引用定位到 page 8、row 11、column 1 和具体 cell ID。跨论文 DuckDB 审计一次解析 20 张相关表，覆盖 10 个 source packets。

另已建立 100 条由持久化 Docling cell 确定答案的 Table QA silver benchmark。当前端到端通过率 76%，Table Resolver recall 94%，cell citation recall 80%；精确单元格通过率 88.33%，跨论文比较仅 10%，后者是当前明确短板。完整方法和逐项结果见 `docs/audits/EVIDENCE_WIKI_SILVER_BENCHMARK_2026-08-13.md`。

### 重入库与迁移安全

- 清理前保存可恢复备份：`backups/pre-evidence-reingest-20260813-091613`。
- 仅删除 Wiki、索引、paper blocks、source packets 和 evidence 等派生结果；26 个 originals PDF 未删除。
- 修复 source-hash upsert 时 relational ID 与 `packet_json.source_id` 不一致的问题。
- 同一来源从 fallback parser 重编译为 Docling 时，替换旧 claims，避免保留失效 evidence IDs。
- GPT-3 论文在 CPU Docling 上超过 360 秒；compose 的 `DOCLING_SERVE_MAX_SYNC_WAIT` 已提升到 900 秒，最终解析为 `docling-remote`。

## 回归结果

```text
pytest:                 32 passed
Python compileall:      passed
backend import/OpenAPI: passed, 66 routes
required evidence APIs: all present
frontend production:   passed, 2874 modules transformed
git diff --check:       passed（仅 CRLF 提示）
Wiki Validator:         0 errors, 0 warnings
```

## 仍需诚实保留的边界

- Semantic Verifier 已有 140 条双模型生成/裁决的 silver benchmark，但仍无人工标注或公开 NLI/claim verification benchmark；93.57%不能写成人工 gold accuracy。
- Table QA 已有 100 条真实 cell-derived silver benchmark，但还没有表头本体映射、单位自动换算和大规模人工 golden 集；跨论文比较通过率仅 10%。
- Markdown/OSS 与 SQLite 无法共享单个 ACID 事务；当前以 frozen revision、幂等 post-commit effects、`commit_failed` 和重试/reconciliation 保持最终一致，不能表述成跨介质数据库事务。
- 后端保留 page/bbox Evidence Layer；审批前端按产品定位展示冲突对象、双方结论/来源和 diff，不要求用户回读 PDF，并支持编辑后重新验证和 Agent Trace；revision 历史与 rollback 尚未接入前端。
- Agent 运行状态和 checkpoint 已持久化，并实现 renewable worker lease、heartbeat、启动/周期恢复扫描和 resume API。活跃节点崩溃后从不可变 PDF 安全重放，而不是恢复 Python 函数的指令指针；已持久化的 Docling Evidence Packet 会按 source hash 命中缓存。多 proposal 仍不构成跨 Markdown/SQLite 的全局 ACID 事务，但失败不会伪装成 approved，而会保留为可见、可幂等重试的 `COMMIT_FAILED`。
- 当前批处理约 75 分钟，CPU-only Docling 对首次解析超长 PDF 的延迟仍高；重复的同文件恢复可使用 Docling Evidence Packet 缓存，首次解析若要进一步降时延仍需异步 Docling job 或 GPU 服务化。

## Agent 化增量验收

- 新增显式状态迁移、checkpoint、approval 和 event 四类持久化对象。
- 默认手工模式在 `AWAITING_APPROVAL` 停止；审批前不修改正式 Wiki，所有 proposal 保存 frozen Markdown 与 parent revision。
- 审批中心展示 structured conflict object、冲突双方来源、Verifier verdict 和 unified diff，支持批准、拒绝、编辑后重新验证；page/bbox 作为后端证据审计数据保留。
- 模型调用、节点、工具、审批与状态事件统一写入顺序化 Agent Trace；完整 prompt 不入库，只保存哈希、长度、模型和耗时。
- 新增并发审批写锁与 stale-parent 检查，避免重复决策和覆盖较新的 Wiki revision。
- `link_only / skip_duplicate` 的 aliases/source/link 也冻结到 proposal，审批前 Markdown 和关系元数据均不变化。
- 人工批准先记录为 `accepted`；只有 revision、post-commit effects 与索引全部成功才转为 `approved`。失败进入 `commit_failed`，审批中心保留错误和“重试提交”。
- worker 使用 SQLite lease + heartbeat；服务启动及每 30 秒扫描过期 lease，支持 `/api/agent-runs/{run_id}/resume`，并对同一 Docling source hash 复用 Evidence Packet。
- 日常入库默认采用风险分级审批：证据闭合的新页面、普通新增和加强自动提交；只有 Merge 阶段确认同一 entity/aspect/重叠 scope 下的高置信 challenge/supersede、无 verified claim 或 Verifier 异常才进入 Review Center。
- 聊天回答框选后加入 Wiki 的入口和 `/api/wiki/maintenance/query-insights/capture-selection` 后端链路保持可用。
- 全量重建采用显式 Agent run/job：26/26 `COMPLETED`，总耗时 4,741.85 秒；最慢 GPT-3 为 474.57 秒。
- 修复整表证据 rebinding：`document_tables` 本身可作为 page/bbox evidence，兼容 Docling 的 `50 . 3 %` PDF 布局空格；Minerva Table 3 数字 claim 已通过重跑。
- 编译结果 `ok=false` 不再伪装为 `COMPLETED`，会进入 `REJECTED` 并保留审计。
- 全量项目回归：`32 passed`；Python `compileall`、FastAPI 路由导入和前端 production build 均通过。

## 秋招叙事

> 我把早期论文问答项目升级为 Wiki-native 科研知识编译系统。Docling 不只输出 Markdown，而是建立 page/bbox/table-cell 级证据层；新论文先生成 claim-aware Markdown patch，再由独立语义 Verifier 回读原文后提交 revision，并支持 diff 与 rollback。查询侧优先解析已经编译的 Wiki；遇到精确数字或跨论文比较时，Table Resolver 把表格单元格映射成只读 DuckDB 视图，答案携带 page/table/cell provenance。

不要宣称“完全消除幻觉”“形式化证明了事实”或“通用表格理解已解决”。可展示的是可复现链路、真实 26 篇重入库数据、拒绝记录、revision 和精确 evidence provenance。

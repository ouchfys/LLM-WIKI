# 可扩展论文 Wiki 简化改造实施文档

状态：主链已实施，旧 Docling JSON 外置等待 OSS 恢复  
日期：2026-08-24  
范围：论文入库、Markdown Wiki、检索、Agent 工具、存储迁移与评测

## 1. 改造目标

本次改造不把项目继续扩张成通用 RAG 平台，而是收敛为一个“有证据、可演化的论文 Wiki Agent”：

- 用户只阅读论文页与主题页，不需要理解 Claim、Evidence、Chunk 等内部对象。
- 日常问答只搜索编译后的 Wiki 页面及章节，不默认搜索原始 PDF 切块。
- Claim 和 Evidence 只服务于写入核验、冲突判断、审批、版本提交与回滚。
- FTS5 负责精确词，跨语言向量负责语义召回，RRF 负责无模型融合；主 Agent 负责最终选择页面，不增加独立 Reranker。
- 大文件进入对象存储，SQLite 保存可重建的索引、关系和运行状态。

## 2. 最终数据边界

### 2.1 用户知识层

仅保留两类页面：

- `PaperPage`：单篇论文的背景、方法、结果、局限与来源。
- `TopicPage`：跨论文主题综合。历史 `ConceptPage` 与 `MethodPage` 兼容读取，新写入统一使用 `TopicPage`。

页面 Markdown 只展示知识正文、相关页面和来源。Claim、Verifier 输出、Merge History、Import Impact、Compiler 信息不再作为可见章节。

### 2.2 维护控制层

以下对象继续保存在 SQLite，但不作为普通问答的检索单元：

- `wiki_claims`
- `claim_evidence`
- `wiki_revisions`
- `wiki_merge_audit`
- `agent_runs` / `agent_events` / `agent_approvals`

Claim 是“可核验的最小结论”，不是第二套 Wiki 页面。Merge 时先定位相关主题页，再比较该页及相邻页中的 Claim，避免全库两两比较。

### 2.3 原始证据层

对象存储保存：

- 原始 PDF
- Docling JSON
- Docling Markdown
- 编译后的 Wiki Markdown 及版本快照
- 未来大规模表格的 Parquet 文件

SQLite 中 `source_documents` 只保留 Docling JSON 的 URI、hash、大小、解析器版本和状态。`document_elements` 与表格投影保留为可查询证据。

## 3. 检索设计

### 3.1 检索单元

新增 `wiki_search_units`，只索引：

- 页面标题、摘要、alias
- Wiki 正文章节

不把 Claim 和 Docling 原文 Chunk 放入普通 Wiki 主索引。每个单元记录 `page_id`、章节、正文、content hash、embedding model 与向量。

### 3.2 查询流程

1. FTS5 召回精确词候选。
2. Qwen3-Embedding-0.6B 召回跨语言语义候选。
3. RRF 合并两路排名。
4. 按 `page_id` 聚合，返回最多 12 张页面及命中章节。
5. 主 Agent 从候选中选择 1～5 张并调用 `wiki_open`。
6. 数字、表格和来源核验才调用 `table_query` 或 `evidence_lookup`。

向 Agent 只返回有界的候选页面与命中小节；不提供全库完整 Markdown 目录兜底。

### 3.3 不引入独立 Reranker

主 Agent 已经需要理解对话并选择页面，因此它同时承担候选精排。只有同时满足以下条件之一，才重新评估专用 Reranker：

- 混合召回 Recall@10 足够高，但 MRR/Top1 长期不达标；
- 页面或检索单元达到万级后，主模型候选选择成本过高；
- 高并发使专用小模型比主模型选择明显更便宜。

## 4. Agent 工具边界

模型只感知：

- `wiki_search`：返回页面候选和命中章节。
- `wiki_open`：打开少量干净 Wiki 页面；兼容旧名 `wiki_card`。
- `table_query`：执行数值、排行与跨论文表格比较。
- `evidence_lookup`：按需回到论文原文证据。
- `web_search` / `web_fetch`：Wiki 缺失或问题要求最新信息时使用。

Web 结果是临时证据，除非用户明确要求导入，否则不能直接写入 Wiki。

## 5. 数据迁移

迁移必须先备份 `sessions.db`，并按以下顺序执行：

1. 为 `source_documents` 添加 Docling URI/hash/size 字段。
2. 将非空 `docling_json` 上传到对象存储，写回 URI 与校验信息。
3. 校验对象可读且 hash 一致后，将内联 JSON 置为 `{}`。
4. 使用当前 `content_json` 重新渲染干净 Wiki Markdown，并更新 Markdown URI。
5. 重建 `wiki_search_units` 和 FTS。
6. 批量生成缺失向量。
7. 核对页面、Claim、Evidence、Revision 数量。
8. 最后执行 `VACUUM` 回收 SQLite 空间。

迁移脚本默认 dry-run；只有显式传入 `--apply` 才写入。每条 Docling 记录必须在对象存储校验成功后才能清空 SQLite 内容。

## 6. 兼容与回滚

- 历史 `ConceptPage`、`MethodPage` 继续可读；新编译结果规范化为 `TopicPage`。
- `wiki_card` 在 API 内部作为 `wiki_open` 的兼容别名保留一个版本周期。
- 历史 Markdown 中的 `wiki-claim` 与 `wiki-system` 注释继续可解析；新渲染页面只保留一个隐藏维护快照，聊天上下文必须过滤该注释。
- 向量服务不可用时自动退化为 FTS5 + alias，不能阻断问答或 Wiki 提交。
- Wiki 索引是派生数据，任何时候都可以从 Markdown 重建。

## 7. 规模路线

### 单用户、十万级检索单元以内

- OSS：文件资产
- SQLite FTS5：关键词索引和元数据
- SQLite BLOB + 归一化 float array：缓存向量和精确余弦检索

### 多用户或百万级检索单元

保持 `WikiSearchIndex` 接口不变，替换为 PostgreSQL/pgvector、Qdrant 或 OpenSearch。触发条件是多实例写入、租户隔离、SQLite 写锁竞争或向量内存超过单机预算，而不是为了简历预先部署。

## 8. 验收标准

### 功能

- 中文问题能够召回英文标题/英文正文页面。
- 精确论文名、模型名、DOI、数字仍可由 FTS 命中。
- 查询结果包含命中章节和 `fts` / `vector` / `alias` 等可解释原因。
- 普通问答不加载 PDF Chunk；表格与证据仅按需调用。
- 同一 PDF 再次上传直接返回“已存在”。
- 只有冲突、覆盖旧结论或 Verifier 异常进入审批。

### 数据

- 迁移前后 Wiki 页面、Claim、Evidence 和 Revision 数量一致。
- Docling JSON 已有可读 URI 和一致 hash 后，SQLite 内联值才可清空。
- 迁移后 SQLite 文件显著缩小。

### 评测

新增独立 Wiki Retrieval benchmark，至少覆盖精确术语、中文问英文、同义改写、跨论文比较、表格问题和无答案问题。比较：

- FTS only
- Vector only
- FTS + Vector + RRF
- FTS + Vector + RRF + 主 Agent 选择

记录 Page Recall@5/10、MRR、跨语言 Recall、平均打开页面数、输入 token、延迟和引用正确率。现有 240 条 Verifier/Table QA benchmark 继续独立保留，不能混用为检索指标。

## 9. 本次实施清单

- [x] Docling JSON 外置 schema、读写兼容和安全迁移脚本
- [x] 干净 Markdown 渲染与聊天上下文过滤
- [x] `TopicPage` 规范化与历史类型兼容
- [x] `wiki_search_units`、章节 FTS、向量缓存和 RRF
- [x] Resolver 改为索引优先，不再读取 2,000 张完整页面逐一评分
- [x] `wiki_open` 与 `evidence_lookup`
- [x] 后台增量向量补全
- [x] Wiki Chat 接入统一 Agent Runtime 事件模型，记录模型/工具 span、Token、耗时、失败和重试
- [ ] 旧 Docling JSON 对象迁移与 SQLite `VACUUM`（当前 OSS 账号返回 `UserDisable`，未清空任何旧 JSON）
- [x] 单元测试、构建、检索 smoke test 和 README 对齐

## 10. 2026-08-24 实施记录

- 迁移前基线：64 个 Wiki 页面、28 份 source document、734,334,818 个内联 Docling JSON 字符，SQLite 约 845 MB。
- 已在库外生成时间戳备份，再执行加法 schema 与派生索引迁移。
- 已将 64 个当前 Markdown 页面重渲染为干净正文；可见 Claim/Review/Compiler 章节数为 0，内部状态保留在隐藏快照和 SQLite。
- 已生成 417 个页面小节检索单元，417 个均已用 `Qwen/Qwen3-Embedding-0.6B` 生成向量。
- 真实 smoke test 中，中文查询“什么是思维树”的 Top1 为英文 `Tree of Thoughts` PaperPage；命中路径同时包含 `section_fts` 与 `multilingual_vector`。
- 当前全量后端测试 `48 passed`（本地对象存储模式），前端 `vue-tsc -b && vite build` 通过。
- OSS 写入验证返回 `UserDisable`。安全迁移脚本因此不会清空 28 份旧 `docling_json`；待 OSS 恢复后执行 `python scripts/migrate_scalable_wiki.py --apply --embeddings --vacuum`。

## 11. 2026-08-25 Wiki Chat Trace 统一

- 一轮问答现在对应一个 `wiki_chat` run，状态为 `QUEUED -> CHAT_RUNNING -> COMPLETED/FAILED`。
- Function calling、最终回答以及回退调用记录为 model span；Wiki、Table、Evidence、Web 等调用记录为 tool span；对话读取和画像更新记录为 memory span。
- OpenAI-compatible 响应包含 usage 时保存精确 Token；流式响应未返回 usage 时保存带 `estimated=true` 的可辨认估算，不混淆为精确计费数据。
- Provider 限流/网络重试与 Web Fetch 候选 URL 重试写入同一追加式事件流，前端只显示紧凑汇总，不展示私有 chain-of-thought。
- 每条消息 metadata 保存对应 runtime run ID 和聚合指标，可通过 `/api/agent-runs/{run_id}/events` 回查完整执行事件。

# Evidence-first Paper Compiler Runbook

## 数据流

```text
PDF
 -> Docling Markdown/Text/JSON
 -> source_documents + typed elements + table cells
 -> distill candidates with evidence IDs
 -> deterministic checks + semantic entailment verifier
 -> resolve target Wiki page
 -> compile add/strengthen/challenge/supersede claims
 -> render Markdown proposal + unified diff
 -> verify affected claims
 -> persist AWAITING_APPROVAL checkpoint
 -> human reviews the conflict object, both conclusions/sources, and diff
 -> approve / reject / edit-and-reverify
 -> commit approved revision + canonical Markdown
 -> rebuild SQLite/chunk navigation cache
 -> Wiki Resolver / Table Resolver / read-only DuckDB
```

## 启动 Docling

```powershell
docker compose -f .\docker-compose.docling.yml up -d
Invoke-RestMethod http://127.0.0.1:5001/health
```

CPU-only 的长论文可能超过 360 秒。compose 已设置 `DOCLING_SERVE_MAX_SYNC_WAIT=900`；客户端运行环境也应设置 `DOCLING_TIMEOUT_SECONDS=900`。

## 安全重建论文语料

先 dry-run：

```powershell
python scripts/reset_paper_corpus_results.py
```

确认 originals 数量与删除范围后执行：

```powershell
python scripts/reset_paper_corpus_results.py --execute
python scripts/ingest_paper_corpus.py --continue-on-error --no-maintenance
```

reset 脚本会备份数据库与本地派生目录，只删除可重建结果，不删除 `sources/papers/originals`。

## Table QA API

```text
GET  /api/wiki/evidence/sources/{source_packet_id}/tables
POST /api/wiki/evidence/table-query
```

Table QA 返回 answer、执行 SQL、命中 tables、rows 和 citations。citation 至少包含 source title、table ID、caption、page、row/column 与 cell ID。

## Revision API

```text
GET  /api/wiki/{card_id}/revisions
GET  /api/wiki/{card_id}/claims
POST /api/wiki/{card_id}/revisions/{revision_id}/rollback
```

只有 committed revision 可以 rollback。原始 PDF、Docling JSON 和 evidence projection 不由 Wiki revision 修改。

## Agent 状态、审批与 Trace API

```text
GET  /api/agent-runs
GET  /api/agent-runs/{run_id}
GET  /api/agent-runs/{run_id}/events
GET  /api/agent-runs/{run_id}/events/stream
GET  /api/agent-runs/approvals?status=pending
GET  /api/agent-runs/approvals/{approval_id}
POST /api/agent-runs/approvals/{approval_id}/approve
POST /api/agent-runs/approvals/{approval_id}/reject
POST /api/agent-runs/approvals/{approval_id}/edit
POST /api/agent-runs/approvals/{approval_id}/retry
POST /api/agent-runs/{run_id}/resume
GET  /api/agent-runs/evidence/{element_id}
```

默认论文入库使用 `approval_mode=risk`：全新页面、普通新增和证据加强在 Verifier/evidence 闭合时自动提交；只有 Merge 阶段确认同一 entity、同一 aspect、重叠 scope 下存在高置信 challenge/supersede，或者缺少 verified claim、Verifier 非干净时，才停在 `AWAITING_APPROVAL`。普通的 `update_existing` 不触发审批。`manual` 仍可用于要求逐项审批的严格场景，`auto` 仅用于受控批量重建。进入等待态前，系统只保存冻结 revision、diff、Verifier 结果和 checkpoint，不写待审的正式 Markdown。前端 `/reviews` 用于处理知识冲突、覆盖旧知识和异常证据，而不是要求用户通读每篇论文；编辑 proposal 会创建新 revision 并重新运行 Verifier。

批准采用乐观并发控制：proposal 的 parent revision 与当前 Wiki revision 不一致时拒绝提交；同一 approval 的并发决策由 SQLite 写锁保证只有一个成功。

## 回归命令

```powershell
python -m pytest -q
python -m compileall -q backend system scripts
cd frontend
npm run build
```

入库后还应检查：

- 26 个 source packets 是否全部为 `docling-remote`；
- `source_documents.docling_json` 是否非空；
- 是否存在 unlinked source、active dangling evidence 或重复 source hash；
- Wiki Validator 是否为 0 errors / 0 warnings；
- 真实表格问题能否返回 page/table/cell provenance；
- 跨论文 DuckDB 查询是否只使用 SELECT/CTE 且外部访问被禁用。

## 仍未完成

- 人工标注的 semantic verifier benchmark；
- 大规模 table QA golden、单位归一化与表头本体对齐；
- 前端 revision 历史与 rollback 操作；
- Markdown/OSS 与 SQLite 不能共享一个 ACID 事务；系统以 frozen revision、幂等 effects、`commit_failed` 和 retry/reconciliation 实现最终一致；
- CPU Docling 的异步作业和性能优化。

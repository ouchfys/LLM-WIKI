# Wiki Maintenance Agent Runbook

## Objective

Build a self-maintaining LLM wiki on top of the existing ingestion pipeline.
The goal is not to turn the system back into ordinary RAG. Raw sources are
compiled into wiki pages, query indexes, cross-links, and reusable knowledge
artifacts. Maintenance agents then keep that compiled wiki healthy over time.

Existing ingestion pipeline:

```text
extract -> distill -> review -> merge
```

Target lifecycle:

```text
source ingestion:
  extract -> distill -> review -> merge -> validate -> generate_indices

maintenance loop:
  validate -> plan -> repair/distill/update -> review -> merge
           -> validate -> generate_indices

query feedback loop:
  chat answer -> archive useful query -> distill insight -> review -> merge
              -> validate -> generate_indices
```

The important rule: automatic maintenance can propose and repair knowledge, but
official wiki pages are only changed after the same review and merge gates used
by paper ingestion.

## Current Storage Target

Use one canonical OSS layout:

```text
users/admin/
  sources/
    papers/
    xiaohongshu/
    web/
  wiki/
    papers/
    concepts/
    methods/
    claims/
    benchmarks/
    interview_notes/
  queries/
    by-paper.md
    by-concept.md
    by-method.md
    by-claim.md
    by-benchmark.md
    by-source.md
    by-interview-topic.md
    orphan-cards.md
    weak-evidence.md
    answered/
  maintenance/
    validation_reports/
    repair_tasks/
    repair_runs/
    candidates/
    quarantined/
    scheduled_runs/
```

Local folders should mirror this layout only as a developer cache. The system
must not depend on `data/raw_sources` or `data/generated/wiki` as durable
storage.

## Model Selection

The project uses SiliconFlow-compatible OpenAI-style chat completions. Based on
the current SiliconFlow model catalog and pricing page, the useful model tiers
for this system are:

- `deepseek-ai/DeepSeek-V4-Flash`: long context, low cost, tool calling support.
- `Qwen/Qwen3.5-9B`: cheap fast model for routing and controlled JSON planning.
- `Qwen/Qwen3.6-27B`: stronger 27B model for review, repair, and merge decisions.
- Optional escalation: `Qwen/Qwen3.5-35B-A3B` or `deepseek-ai/DeepSeek-V4-Pro`
  only for hard scheduled maintenance, not for the default path.

Recommended `.env` routing:

```text
SILICONFLOW_CHAT_MODEL=deepseek-ai/DeepSeek-V4-Flash
SILICONFLOW_FAST_MODEL=Qwen/Qwen3.5-9B
SILICONFLOW_SUMMARY_MODEL=deepseek-ai/DeepSeek-V4-Flash
SILICONFLOW_REVIEW_MODEL=Qwen/Qwen3.6-27B
SILICONFLOW_MERGE_MODEL=Qwen/Qwen3.6-27B
SILICONFLOW_MAINTENANCE_MODEL=Qwen/Qwen3.6-27B
SILICONFLOW_MAINTENANCE_FAST_MODEL=Qwen/Qwen3.5-9B
```

Rationale:

| Component | Model | Why |
| --- | --- | --- |
| Extract Agent | Docling remote service | deterministic document parsing, no LLM |
| Distill Agent | `deepseek-ai/DeepSeek-V4-Flash` | cheap long-context source-to-structure distillation |
| Reviewer Agent | `Qwen/Qwen3.6-27B` | evidence and schema quality gate |
| Merge Agent | `Qwen/Qwen3.6-27B` | card creation/update/link planning |
| Wiki Chat Agent | `deepseek-ai/DeepSeek-V4-Flash` | long-context answers with tool observations |
| Maintenance Planner | `Qwen/Qwen3.5-9B` | cheap task routing |
| Repair Agent | `Qwen/Qwen3.6-27B` | semantic repair of weak cards and links |
| Query Insight Distiller | `deepseek-ai/DeepSeek-V4-Flash` | distill high-quality chat turns back into candidates |
| Web Update Agent | `Qwen/Qwen3.6-27B` | decide whether web findings deserve ingestion |
| Validator | none | deterministic Python checks |
| Index Generator | none | deterministic markdown/index generation |

Do not introduce more models until metrics show a clear bottleneck.

## Agent Roles

### 1. Extract Agent

Driver: Docling.

Responsibilities:

- Parse PDFs into text, tables, structure, and source metadata.
- Store original files under `sources/papers/originals/`.
- Store parsed artifacts under `sources/papers/parsed/`.
- Never write official wiki pages.

Output:

```text
source packet + parsed markdown/text + source metadata
```

### 2. Distill Agent

Driver: `deepseek-ai/DeepSeek-V4-Flash`.

Responsibilities:

- Convert parsed source packets into structured paper cards.
- Extract concepts, methods, claims, benchmarks, limitations, and evidence.
- Produce candidate wiki payloads, not final wiki pages.

Output:

```json
{
  "paper": {},
  "concepts": [],
  "methods": [],
  "claims": [],
  "benchmarks": [],
  "evidence": []
}
```

### 3. Reviewer Agent

Driver: `Qwen/Qwen3.6-27B`.

Responsibilities:

- Check whether candidate content is supported by source evidence.
- Reject hallucinated claims.
- Mark weak summaries for repair.
- Enforce schema and confidence labels.

Allowed decisions:

```text
accepted
needs_repair
quarantined
excluded_from_wiki
```

Avoid using `needs_human_review` as the normal state. Human review is only for
debugging or demo inspection, not for routine wiki generation.

### 4. Merge Agent

Driver: `Qwen/Qwen3.6-27B`.

Responsibilities:

- Decide whether a candidate creates a new card or updates an existing card.
- Maintain aliases and related-card links.
- Merge duplicate concepts/methods.
- Preserve source evidence and confidence labels.

The Merge Agent writes only after Reviewer approval.

### 5. Deterministic Validator

Driver: Python only.

Responsibilities:

- Check schema, links, aliases, source paths, markdown paths, and FTS tables.
- Detect legacy `data/` dependencies.
- Emit machine-readable findings.
- Create repair tasks when requested.

Checks:

- Required fields exist.
- `page_type` is controlled.
- `confidence` is controlled.
- `source_packet_id`, `paper_id`, `raw_source_path`, `pdf_storage_uri`, and
  `markdown_path` resolve.
- Related cards exist.
- Aliases do not collide across unrelated cards.
- Markdown lives under `wiki/`.
- Source artifacts live under `sources/`.
- Query artifacts live under `queries/`.
- FTS rows match official wiki tables.

### 6. Maintenance Planner Agent

Driver: `Qwen/Qwen3.5-9B`.

Responsibilities:

- Convert validator findings into typed repair tasks.
- Prioritize high-risk tasks first.
- Route tasks to deterministic repair, Repair Agent, Distill Agent, Merge Agent,
  or source re-extraction.

Optimization:

- If validation has zero errors and zero warnings, skip the LLM call and return
  an empty plan.

Planner output:

```json
{
  "tasks": [
    {
      "task_type": "repair_broken_link",
      "priority": "high",
      "target_entity_type": "wiki_page",
      "target_entity_id": "concept-rmsnorm",
      "repair_target": "merge_agent",
      "reason": "related target does not exist",
      "evidence": ["validator:error:broken_related_link"]
    }
  ]
}
```

### 7. Deterministic Repair Processor

Driver: Python only.

Responsibilities:

- Rebuild stale FTS tables.
- Regenerate query indexes.
- Normalize obvious legacy paths when there is a deterministic mapping.
- Fill safe optional defaults.

It must not rewrite semantic content.

### 8. Repair Agent

Driver: `Qwen/Qwen3.6-27B`.

Responsibilities:

- Repair malformed concept/method/card payloads.
- Rewrite weak summaries using existing evidence.
- Remove unsupported claims.
- Propose missing links or duplicate merges.
- Produce a repair candidate that returns to Reviewer and Merge Agent.

Hard rule:

```text
Repair Agent output -> Reviewer Agent -> Merge Agent -> Validator
```

The Repair Agent never writes directly to official wiki tables.

Repair output:

```json
{
  "status": "candidate_ready",
  "candidate_type": "concept_update",
  "target_card_id": "concept-rmsnorm",
  "changes": {
    "summary": "...",
    "content_json": {},
    "related": ["concept-layernorm"],
    "evidence_basis": [
      {
        "source_id": "...",
        "quote_or_fact": "...",
        "confidence": "source_reported"
      }
    ]
  },
  "reason": "..."
}
```

### 9. Query Insight Distiller

Driver: `deepseek-ai/DeepSeek-V4-Flash`.

Escalate to `Qwen/Qwen3.6-27B` when the chat answer contains cross-paper claims,
web evidence, or contradictions between cards.

Responsibilities:

- Archive useful chat turns under `queries/answered/YYYY-MM-DD/`.
- Extract reusable concepts, comparisons, interview notes, and open questions.
- Generate candidate payloads for review and merge.

Trigger only when:

- The answer used `wiki_card`, `wiki_search`, `web_search`, or `web_fetch`.
- The answer contains reusable cross-paper synthesis.
- The user explicitly asks to remember, summarize, or save the result.
- The answer improves a weak card or resolves an ambiguity.

Do not trigger for:

- Short factual replies.
- Failed answers.
- UI/debug questions.
- Answers without reusable wiki knowledge.

### 10. Web Update Agent

Driver: `Qwen/Qwen3.6-27B`.

Tools:

```text
web_search
web_fetch
wiki_search
wiki_card
```

Responsibilities:

- Discover new papers, benchmarks, repos, or explanatory articles for important
  topics.
- Decide whether a finding should enter `sources/`.
- Create source candidates only.

Hard rule:

```text
web finding -> source candidate -> extract/distill/review/merge
```

The Web Update Agent must not patch official wiki pages directly.

## Error Routing

| Error type | First handler | Escalation |
| --- | --- | --- |
| `schema_error` | deterministic repair | Repair Agent |
| `unsupported_tag` | fast tag normalizer | Reviewer Agent |
| `missing_source` | source packet repair | Extract Agent |
| `weak_evidence` | Distill Agent | Reviewer Agent |
| `hallucinated_claim` | Distill Agent | Reviewer Agent |
| `duplicate_card` | Merge Agent | Reviewer Agent |
| `broken_related_link` | deterministic repair or Merge Agent | Repair Agent |
| `legacy_path` | deterministic path repair | quarantine if unresolved |
| `index_stale` | Index Generator | none |
| `fts_mismatch` | deterministic FTS rebuild | none |
| `web_candidate_uncertain` | Web Update Agent | quarantine |

Retry policy:

```text
max_repair_attempts = 2
if still invalid:
  status = quarantined
  exclude from official wiki search and normal chat context
```

## Database Tables

Use these maintenance tables:

```sql
CREATE TABLE IF NOT EXISTS wiki_validation_runs (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  error_count INTEGER DEFAULT 0,
  warning_count INTEGER DEFAULT 0,
  report_json TEXT NOT NULL,
  artifact_uri TEXT DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wiki_repair_tasks (
  id TEXT PRIMARY KEY,
  validation_run_id TEXT DEFAULT '',
  task_type TEXT NOT NULL,
  target_entity_type TEXT DEFAULT '',
  target_entity_id TEXT DEFAULT '',
  repair_target TEXT NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER DEFAULT 0,
  payload_json TEXT NOT NULL,
  result_json TEXT DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wiki_query_insights (
  id TEXT PRIMARY KEY,
  session_id TEXT DEFAULT '',
  message_id TEXT DEFAULT '',
  question TEXT NOT NULL,
  answer_excerpt TEXT DEFAULT '',
  insight_json TEXT NOT NULL,
  status TEXT NOT NULL,
  candidate_id TEXT DEFAULT '',
  created_at TEXT NOT NULL
);
```

Reuse existing official wiki tables:

- `wiki_pages`
- `wiki_chunks`
- `wiki_card_links`
- `wiki_card_sources`
- `wiki_merge_audit`
- `papers`
- `source_packets`
- `ingestion_jobs`

## API Surface

Maintenance endpoints:

```text
POST /api/wiki/maintenance/validate
GET  /api/wiki/maintenance/validation-runs
GET  /api/wiki/maintenance/validation-runs/{run_id}
GET  /api/wiki/maintenance/repair-tasks
POST /api/wiki/maintenance/repair-tasks/process
POST /api/wiki/maintenance/generate-indices
POST /api/wiki/maintenance/run-once
GET  /api/wiki/maintenance/query-insights
POST /api/wiki/maintenance/query-insights/distill
```

Add later:

```text
POST /api/wiki/maintenance/repair-agent/process
POST /api/wiki/maintenance/web-update/discover
POST /api/wiki/maintenance/web-sources/process
POST /api/wiki/maintenance/candidates/review-merge
```

Implemented candidate endpoints:

```text
GET  /api/wiki/maintenance/candidates
GET  /api/wiki/maintenance/candidates/{candidate_id}
POST /api/wiki/maintenance/candidates/process
```

## CLI Surface

Required:

```text
python -m system.wiki.maintenance.validate
python -m system.wiki.maintenance.generate_indices
python -m system.wiki.maintenance.run_once
python -m system.wiki.maintenance.process_repairs
python -m system.wiki.maintenance.distill_query_insights
```

Add later:

```text
python -m system.wiki.maintenance.process_repair_agent --limit 5
python -m system.wiki.maintenance.web_update --topic "LLM difficulty perception"
python -m system.wiki.maintenance.process_web_sources --limit 5
python -m system.wiki.maintenance.review_candidates --limit 5
python -m system.wiki.maintenance.review_candidates --limit 5 --use-llm
```

`review_candidates --use-llm` uses `SILICONFLOW_MAINTENANCE_MODEL` as the
maintenance Reviewer/Merge Planner. Python still enforces the final whitelist
for allowed create/update operations.

Full manual loop:

```text
python -m system.wiki.maintenance.run_once \
  --process-llm-repairs \
  --distill-query-insights \
  --process-web-sources \
  --process-candidates
```

## Implementation Plan

### Phase 0: Lock Current Foundation

1. Keep `.env` as the only root env file.
2. Ensure storage routes to:

```text
STORAGE_BACKEND=oss
STORAGE_ROOT_PREFIX=users/admin
```

3. Ensure `SILICONFLOW_FAST_MODEL=Qwen/Qwen3.5-9B`.
4. Ensure ingestion still runs:

```text
extract -> distill -> review -> merge
```

Acceptance:

- `python -m compileall system backend` passes.
- Empty wiki validates.
- Generated query indexes exist locally and in OSS.

### Phase 1: Deterministic Maintenance

1. Implement validator.
2. Implement query index generator.
3. Implement validation run persistence.
4. Implement repair task persistence.
5. Implement deterministic repair processor.
6. Trigger light maintenance after successful paper ingestion.

Acceptance:

- `validate -> generate_indices -> run_once` works from CLI.
- `run_once` produces validation artifacts and query indexes.
- Stale FTS/index issues are repaired without LLM calls.

### Phase 2: LLM Repair Agent

1. Add `system/wiki/maintenance/repair_agent.py`.
2. Process tasks where `repair_target` is:

```text
repair_agent
distill_agent
merge_agent
reviewer_agent
```

3. Generate candidate payloads only.
4. Route candidates through Reviewer and Merge Agent.
5. Re-run Validator and Index Generator.

Acceptance:

- A deliberately weak concept card becomes a candidate update.
- A broken related link is repaired or quarantined.
- Invalid repair output never enters official wiki.

### Phase 3: Query Knowledge Feedback

1. Archive high-value chat answers under `queries/answered/`.
2. Distill archived turns into candidate payloads.
3. Route candidate insights through Reviewer and Merge Agent.
4. Mark each insight as:

```text
archived
candidate_ready
merged
quarantined
```

Acceptance:

- A cross-paper chat answer can produce a reusable concept/method relation.
- The relation appears in `queries/by-concept.md` or `queries/by-method.md`
  only after review and merge.

### Phase 4: Web Update Agent

1. Add `web_fetch` as a first-class chat and maintenance tool if not already
   wired end-to-end.
2. Add Web Update Agent.
3. Limit default runs to selected topics or manual invocation.
4. Store discovered items as source candidates.
5. Send accepted sources through normal ingestion.

Acceptance:

- Web findings create source candidates, not direct wiki edits.
- New sources are traceable under `sources/web/`.
- Validator can verify source links and candidate status.

### Phase 5: Scheduled Self-Maintenance

1. Add a manual button first.
2. Add a scheduled job only after manual runs are stable.
3. Run:

```text
validate -> plan -> deterministic_repair -> llm_repair_candidates
         -> review -> merge -> validate -> generate_indices
```

Acceptance:

- Maintenance can run without user edits.
- Quarantined items are excluded from normal search.
- Metrics show validation errors, repair success rate, and query insight
  acceptance rate.

## Prompt Contracts

### Repair Agent System Contract

The Repair Agent is not allowed to invent facts. It may only use supplied source
evidence, existing wiki cards, and tool observations.

Return strict JSON:

```json
{
  "status": "candidate_ready",
  "candidate_type": "card_update",
  "target_card_id": "string",
  "risk": "low|medium|high",
  "changes": {
    "title": "optional string",
    "summary": "optional string",
    "content_json": {},
    "related_topics": [],
    "source_ids": []
  },
  "evidence_basis": [
    {
      "source_id": "string",
      "fact": "string",
      "supports_change": "string"
    }
  ],
  "review_notes": "string"
}
```

If evidence is insufficient:

```json
{
  "status": "quarantined",
  "reason": "insufficient evidence",
  "missing_evidence": ["..."]
}
```

### Web Update Agent System Contract

Return strict JSON:

```json
{
  "topic": "string",
  "candidates": [
    {
      "source_type": "paper|repo|blog|benchmark|documentation",
      "title": "string",
      "url": "string",
      "why_relevant": "string",
      "expected_wiki_impact": "new_card|update_card|background_only",
      "confidence": "high|medium|low"
    }
  ],
  "rejected": [
    {
      "url": "string",
      "reason": "string"
    }
  ]
}
```

## Metrics

Track these in maintenance reports:

- validation error count
- validation warning count
- repair task count
- deterministic repair success rate
- LLM repair success rate
- quarantined item count
- duplicate card rate
- broken link count
- index freshness
- query insight archive count
- query insight merge acceptance rate
- retrieval hit rate on benchmark questions
- final answer correctness and confidence on benchmark questions

## Demo Narrative

Use this short explanation in interviews:

> This project is not a one-shot RAG system. I store raw sources in OSS, compile
> them into a structured LLM wiki through extract, distill, review, and merge
> agents, then run a maintenance loop over the compiled knowledge. The
> validator checks schema, evidence, links, aliases, and storage paths. The
> planner turns problems into repair tasks. Repair and query-insight agents
> propose candidate updates, but those candidates still go through reviewer and
> merger before entering the official wiki. This is closer to a self-maintained
> LLM wiki: knowledge can accumulate from sources and useful queries instead of
> disappearing after a chat session.

## Near-Term Build Checklist

1. Finish deterministic validator/index/repair loop.
2. Add LLM Repair Agent with `Qwen/Qwen3.6-27B`.
3. Route repair candidates through existing Reviewer and Merge Agent.
4. Turn query insight candidates into reviewable merge candidates.
5. Add Web Update Agent after `web_fetch` is fully wired.
6. Run a clean re-ingestion of seed papers.
7. Run `python -m system.wiki.maintenance.run_once --check-storage`.
8. Inspect `queries/*.md`, validation reports, and benchmark answer quality.

# Agentic Research Memory Execution Plan

## Objective

Turn the current paper/Xiaohongshu ingestion and Wiki chat system into a resume-grade agent project:

> An Agentic Research Memory System that ingests papers and lightweight social notes, distills them into an evolving Wiki memory, retrieves evidence through explicit tools, and evaluates answer quality with reproducible benchmarks.

The project should be demonstrable in an interview as more than a normal RAG application. The main differentiator is an observable agent loop:

1. Parse and extract source content.
2. Distill source content into structured cards.
3. Review factual consistency.
4. Merge reviewed knowledge into an evolving Wiki memory.
5. Answer with explicit tools: `wiki_search`, `wiki_card`, `web_search`, `resource_recommend`.
6. Evaluate retrieval and answer quality with benchmark reports.

## Current Baseline

The system is already around 80/100 on ingestion and retrieval.

Implemented:

- Paper ingestion with Docling-backed parsing.
- Four-agent paper pipeline concept: extraction, distillation, review, merge.
- OSS-backed Wiki storage under `users/admin/...`.
- PaperPage / ConceptPage / MethodPage cards in SQLite and OSS markdown.
- Wiki chat with agentic tool plan output.
- 30-question paper benchmark under `test/evaluation`.
- Focused regression run for `paper_018` and `paper_028`.

Recent focused validation:

```text
Run: test/evaluation/runs/current30_focus_018_028_v2
paper_018 final_score: 0.9775
paper_028 final_score: 0.9750
overall_final_score: 0.9763
answer_confidence: 1.0
citation_grounding: 1.0
retrieval_hit_rate: 1.0
top1_hit_rate: 1.0
```

Important scoring decision:

- `wrong_web_usage_rate` is now diagnostic only.
- Web usage should not be penalized if answer quality, grounding, and usefulness are good.

## Resume-Grade Gaps

The system still needs four pieces to become a strong agent development project:

1. Agent Trace UI
2. Wiki Merge Hardening
3. Evaluation Dashboard
4. Async Ingestion Pipeline

These should be implemented in this order because each step improves interview demonstrability.

## Milestone 1: Agent Trace UI

### Goal

Make every Wiki chat answer explainable:

- Which tools were planned.
- Which tools actually ran.
- Which cards were retrieved.
- Which matched chunks were used.
- Whether Web Search was used.
- Which citations support the final answer.

This is the clearest interview differentiator: the system is not a hidden RAG call; it is an observable agent.

### Backend Tasks

1. Extend Wiki chat response payload if needed.
   - Current payload already includes `tool_plan`, `citations`, `resources`.
   - Add optional trace fields only if missing:
     - `retrieved_cards`
     - `matched_chunks`
     - `web_results`
     - `tool_events`
     - `answer_evidence_map`

2. Keep payload compact.
   - Include chunk excerpts, not full documents.
   - Limit to top 3 matched chunks per card.

3. Preserve streaming compatibility.
   - Existing stream events already include `tool_plan`, `tool_status`, `card_list`, `resource_list`.
   - The non-streaming API should expose equivalent trace information for evaluation/debugging.

### Frontend Tasks

Add a trace panel to the Wiki chat page:

- Tool plan row:
  - `wiki_search`
  - `wiki_card`
  - `web_search`
  - `resource_recommend`
- Retrieved cards list:
  - card title
  - page type
  - summary
  - matched chunk excerpt
- Evidence/citation panel:
  - map `[1]`, `[2]` to card titles
  - show source path / OSS path
- Web diagnostic:
  - show whether Web was used
  - show titles/URLs if available

Design direction:

- Dense, technical, dashboard-like.
- No explanatory marketing copy.
- Use compact tabs or collapsible panels.
- The first screen should remain the chat/workspace, not a landing page.

### Acceptance Criteria

- Asking `Why is estimating difficulty without generating output tokens useful?` shows:
  - tool plan
  - top card: `The LLM Already Knows`
  - matched chunk with no-token/hidden-state/value-function evidence
  - citations used in final answer

- Asking the lawyer-system tech-stack question shows:
  - top card: `律师推荐和服务小程序的设计与实现`
  - matched chunk containing Vue / Spring Boot / MySQL / WebSocket / Ollama

### Suggested Verification

```powershell
python -m py_compile system/wiki/wiki_chat.py backend/api/wiki.py
python -u test/evaluation/scripts/run_agentic_wiki_eval.py `
  --dataset test/evaluation/datasets/wiki_chat/current_papers_30.csv `
  --ids paper_018 paper_028 `
  --output-dir test/evaluation/runs/trace_ui_regression `
  --timeout-seconds 120 `
  --disable-review-llm
```

Then verify the UI manually at:

```text
http://127.0.0.1:5173
```

## Milestone 2: Wiki Merge Hardening

### Goal

Make the Wiki memory evolve instead of accumulating isolated paper notes.

When a new paper is ingested:

- PaperPage should summarize the paper.
- ConceptPage should be created or updated for reusable concepts.
- MethodPage should be created or updated for reusable methods.
- Existing related cards should be strengthened, not duplicated.

### Backend Tasks

1. Improve merge planner behavior.
   - For each distilled paper, classify proposed updates:
     - `create_card`
     - `update_card`
     - `link_card`
     - `skip_duplicate`

2. Add deterministic merge rules before LLM merge.
   - Normalize titles and aliases.
   - Match by canonical title, aliases, source URLs, and related topics.
   - Prefer updating existing ConceptPage/MethodPage if semantic overlap is high.

3. Store merge evidence.
   - Each update should include:
     - source paper id
     - source packet id
     - evidence excerpt
     - reviewer status
     - merge action

4. Add merge audit log.
   - Store in SQLite or a small JSONL table/file.
   - Make it queryable from backend or scripts.

### Card Quality Requirements

PaperPage should include:

- `problem`
- `key_idea`
- `method`
- `results`
- `limitations`
- `key_contributions`
- `key_takeaways`
- `aliases`

ConceptPage should include:

- `definition`
- `mechanism`
- `why_it_matters`
- `examples`
- `related_papers`

MethodPage should include:

- `description`
- `steps`
- `when_to_use`
- `comparison_to_alternatives`
- `limitations`
- `related_papers`

### Acceptance Criteria

Ingesting `The LLM Already Knows` and `Probing the Difficulty Perception Mechanism of Large Language Models` should produce or update shared cards such as:

- `LLM-Perceived Question Difficulty`
- `Hidden representations`
- `Value-based Difficulty Estimation from Hidden Representations`
- `Linear Probing for Difficulty Perception`

The system should not create duplicate cards for the same concept under slightly different names.

### Suggested Verification

```powershell
python test/evaluation/scripts/build_agentic_wiki_eval_dataset.py
python -u test/evaluation/scripts/run_agentic_wiki_eval.py `
  --dataset test/evaluation/datasets/wiki_chat/current_papers_30.csv `
  --output-dir test/evaluation/runs/wiki_merge_regression `
  --timeout-seconds 120 `
  --disable-review-llm
```

Target:

```text
overall_final_score >= 0.85
answer_confidence >= 0.88
retrieval_hit_rate >= 0.95
top1_hit_rate >= 0.80
```

## Milestone 3: Evaluation Dashboard

### Goal

Make benchmark results visible in the app so the project demonstrates engineering rigor.

### Backend Tasks

Add an API for evaluation runs:

- List available runs under `test/evaluation/runs`.
- Read `summary.json`.
- Read `queries_details.csv`.
- Return lowest-scoring cases.
- Return per-paper split metrics.

Suggested endpoints:

```text
GET /api/wiki/evaluations
GET /api/wiki/evaluations/{run_id}
GET /api/wiki/evaluations/{run_id}/cases
```

### Frontend Tasks

Add an Evaluation view or tab:

- Overall score cards:
  - final score
  - answer confidence
  - citation grounding
  - retrieval hit rate
  - top1 hit rate
  - latency
- Table of benchmark cases:
  - id
  - query
  - source
  - final score
  - answer confidence
  - top1 hit
  - web used
  - failure bucket
- Case detail drawer:
  - answer
  - citations
  - tool plan
  - reviewer reason

### Acceptance Criteria

- The latest focused run `current30_focus_018_028_v2` is visible.
- The 30-question benchmark can be selected if available.
- Low-scoring cases can be inspected without opening CSV files manually.

## Milestone 4: Async Ingestion Pipeline

### Goal

Make paper import demo-safe.

The UI should not block for several minutes while Docling and LLM distillation run.

### Backend Tasks

1. Introduce ingestion job table.

Suggested fields:

```text
id
source_type
source_uri
status
stage
progress
error
source_packet_id
paper_card_id
created_at
updated_at
```

2. Split ingestion into stages:

```text
queued
uploading_to_oss
docling_extracting
distilling
reviewing
merging
indexing
done
failed
```

3. API endpoints:

```text
POST /api/wiki/ingest
GET /api/wiki/ingest/jobs
GET /api/wiki/ingest/jobs/{job_id}
```

4. Keep first artifact fast.
   - Create SourceNote or raw source record immediately.
   - Compile PaperPage asynchronously.
   - Reindex chunks after merge.

### Frontend Tasks

Add ingestion status UI:

- Job list
- Current stage
- Progress indicator
- Link to generated card once available
- Error message if failed

### Acceptance Criteria

- Importing one paper returns a job id quickly.
- UI remains responsive.
- Job status reaches `done`.
- Generated card appears in Wiki.
- Evaluation can query the new card.

## Recommended Goal-Mode Execution Order

Run these as separate goal-mode objectives.

### Goal 1

```text
Implement Agent Trace UI for Wiki chat, including backend trace payload if needed, frontend trace panel, and focused verification on paper_018 and paper_028.
```

### Goal 2

```text
Harden Wiki merge behavior so new paper ingestion updates existing ConceptPage/MethodPage cards with evidence instead of creating duplicates; add merge audit logs and run the 30-question benchmark.
```

### Goal 3

```text
Build an Evaluation Dashboard that reads test/evaluation run artifacts and displays summary metrics, case-level answers, citations, tool plans, and low-score failure cases.
```

### Goal 4

```text
Refactor paper ingestion into an async job pipeline with visible stage progress, fast initial source registration, OSS-backed artifacts, background distillation/review/merge, and card reindexing.
```

## Demo Script For Interviews

Use this flow:

1. Show ingestion page.
   - Explain Docling extraction and OSS storage.
2. Show one PaperPage.
   - Explain structured distillation fields.
3. Ask a targeted Wiki question:
   - `Why is estimating difficulty without generating output tokens useful?`
4. Open Agent Trace panel.
   - Show `wiki_search`, `wiki_card`, matched card, matched chunks, citations.
5. Show Evaluation Dashboard.
   - Show benchmark score and two fixed cases.
6. Explain Wiki merge.
   - New papers update shared concepts/methods instead of becoming isolated chunks.

## Resume Bullets

Use bullets like these:

- Built an agentic research memory system that ingests papers with Docling, stores artifacts in OSS, distills sources into structured Wiki cards, and retrieves evidence through explicit tools.
- Implemented a four-stage paper knowledge pipeline: extraction, LLM distillation, factual review, and Wiki merge into PaperPage/ConceptPage/MethodPage memory.
- Designed tool-based Wiki chat with observable planning across `wiki_search`, `wiki_card`, `web_search`, and `resource_recommend`, including citation-grounded answer generation.
- Built a reproducible evaluation benchmark measuring retrieval hit rate, top-1 hit rate, citation grounding, answer confidence, and case-level failure modes.
- Improved answer quality through benchmark-driven iteration, raising focused regression cases for paper-tech-stack and hidden-state difficulty-estimation questions to approximately 0.98 final score.

## Non-Goals

Do not spend the next iteration on:

- Large frontend redesign unrelated to trace/evaluation.
- Adding more content sources before async ingestion is stable.
- Over-optimizing strict JSON reviewer output if answer quality evaluation is already useful.
- Rebuilding the whole storage system again.

## Final Acceptance For The Whole Project

The project is resume-ready when:

```text
Paper ingestion works asynchronously.
Wiki cards are structured and merge into existing concepts/methods.
Wiki chat shows agent trace and grounded citations.
Evaluation dashboard shows benchmark metrics and failure cases.
30-question benchmark overall_final_score >= 0.85.
Focused paper_018 and paper_028 regression stays >= 0.95.
Demo can be completed in under 5 minutes.
```


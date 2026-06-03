# Paper Pipeline Docling + 27B Rebuild Plan

## Background

The paper Wiki pipeline already has a working four-stage skeleton:

1. Extraction Agent: the code can call Docling, but the verified run often fell back to `PaperIndexParser`.
2. Distillation Agent: the summary model generates `PaperPage`, `ConceptPage`, and `MethodPage` candidates.
3. Reviewer Agent: currently implemented as deterministic Python rules.
4. Merge Agent: currently implemented as deterministic Python merge logic.

The current pipeline has proven that a second related paper can update a core card created from the first paper. However, it still has several issues:

- Docling is not yet guaranteed to be the primary extraction path.
- Reviewer and merge decisions rely too heavily on hard rules and alias matching.
- The frontend card detail page still exposes schema-like fields and needs a more polished reading layout.
- Paper cards are too concise. Fields such as `key_idea` and `method` should be expanded into 5-6 display-ready lines for interview demos.

This rebuild should clear the existing generated knowledge base, connect Docling Docker as the real extraction agent, add Qwen 27B for reviewer and merge-planner stages, add keyword navigation, and improve the frontend reading experience.

## Target Architecture

Final target pipeline:

```text
PDF / Paper source
  -> Extraction Agent: Docling Docker
  -> Distillation Agent: summary LLM
  -> Reviewer Agent: Qwen/Qwen3.6-27B
  -> Merge Planner Agent: Qwen/Qwen3.6-27B
  -> Merge Executor: deterministic Python
  -> Wiki card + evidence + links + keyword jumps
```

Core rules:

- LLMs only judge, distill, and plan. They must not write directly to the database.
- Python executors handle database writes, OSS writes, indexing, and rollback-safe operations.
- Every stage must persist its artifact so failures can be inspected and rerun.
- Cleanup should remove generated knowledge, not original paper PDFs or source-only references.

## Phase 1: Reset the Existing Knowledge Base

### Goal

Start from a clean demo state so old test data, fallback parser output, and duplicate cards do not affect the next implementation or interview demo.

### New Script

Create:

```text
scripts/reset_demo_knowledge_base.py
```

### Database Cleanup Scope

Clear the following tables:

- `wiki_pages`
- `wiki_chunks`
- `papers`
- `paper_blocks`
- `source_packets`
- `distilled_candidates`
- `review_reports`
- `wiki_card_sources`
- `wiki_card_links`
- `wiki_aliases`

### Local Filesystem Cleanup Scope

Remove generated local artifacts:

- `data/generated/wiki/**`
- `data/wiki/**`
- `data/raw_sources/**` if a full raw-source replay is required

### OSS Cleanup Scope

Remove generated OSS artifacts:

- `users/admin/data/generated/wiki/**`
- `users/admin/data/raw_sources/**`
- Optionally remove uploaded test PDFs under `users/admin/data/*.pdf` so the import flow uploads them again

### Preserve

Do not delete:

- `data/*.pdf`
- `.env`
- original Xiaohongshu links or source-only records, if they should remain as raw source references

### Suggested Script Interface

```bash
python scripts/reset_demo_knowledge_base.py --dry-run
python scripts/reset_demo_knowledge_base.py --execute --keep-pdfs --clear-oss-generated --clear-pipeline-db
```

### Acceptance Criteria

After cleanup:

- `/api/wiki` returns an empty list.
- The target DB tables are empty or reinitialized.
- `users/admin/data/generated/wiki` is empty in OSS.
- The original test PDFs still exist under `data/`.

## Phase 2: Make Docling Docker the Primary Extraction Agent

### Goal

Define Docling Docker as the official paper extraction agent. `PaperIndexParser` should remain only as a fallback path.

### Docker Service

The project already has:

```text
docker-compose.docling.yml
```

Start it with:

```powershell
docker compose -f docker-compose.docling.yml up -d
```

Health check:

```text
http://127.0.0.1:5001/health
```

Expected response:

```json
{"status":"ok"}
```

### Environment Variables

Set these in `.env`:

```env
DOCLING_MODE=remote
DOCLING_BASE_URL=http://127.0.0.1:5001
DOCLING_TIMEOUT_SECONDS=360
```

### Code Requirements

Review and update:

```text
system/document/docling_parser.py
system/wiki/paper_pipeline/extractor.py
backend/api/papers.py
```

Required behavior:

- `extractor.py` tries `DoclingParser` first.
- On success, `parser_used = "docling-remote"`.
- On failure, fallback to `PaperIndexParser`.
- API responses keep the `parser` field.
- `SourcePacket` stores:
  - `raw_source_path`
  - `pdf_storage_uri`
  - `parser_used`
  - `sections`
  - `blocks`

### Acceptance Criteria

After importing a test PDF:

- The API returns `parser: docling-remote`.
- `source_packets.packet_json.sections` contains stable section-level data.
- The PDF is uploaded to:

```text
oss://.../users/admin/data/<paper>.pdf
```

- The raw markdown source is uploaded to:

```text
oss://.../users/admin/data/raw_sources/markdown/paper_pdf/<slug>.md
```

## Phase 3: Add Qwen 27B Reviewer Agent

### Goal

Upgrade the reviewer from pure deterministic rules to:

```text
rule precheck -> Qwen 27B reviewer -> JSON validation -> deterministic fallback
```

### Model Configuration

Add:

```env
SILICONFLOW_REVIEW_MODEL=Qwen/Qwen3.6-27B
```

Add a dependency helper:

```python
get_review_llm()
```

Recommended generation settings:

```text
temperature = 0
max_tokens = 1500-2500
```

### Reviewer Input

The reviewer prompt must include:

- current `DistilledCandidate`
- candidate claims
- candidate evidence
- source title
- source section id
- top similar existing cards
- alias match result
- deterministic schema check result

### Reviewer Output Schema

The model must return strict JSON:

```json
{
  "status": "approved",
  "schema_errors": [],
  "unsupported_claims": [],
  "evidence_quality": "good",
  "duplicate_candidates": [
    {
      "existing_card_id": "",
      "existing_title": "",
      "confidence": 0.0,
      "reason": ""
    }
  ],
  "merge_recommendation": {
    "action": "create_new",
    "target_card_id": "",
    "confidence": 0.0,
    "reason": ""
  }
}
```

Allowed `status` values:

- `approved`
- `needs_revision`
- `rejected`

Allowed `merge_recommendation.action` values:

- `create_new`
- `update_existing`
- `link_only`
- `needs_human_review`

### Fallback Rules

Fallback to deterministic review or `needs_human_review` when:

- the model returns invalid JSON
- `target_card_id` does not exist
- evidence is empty but the model approves the candidate
- confidence is below the threshold
- action is not in the allowlist

### Acceptance Criteria

When importing the second paper, the reviewer should understand that the following are related and should not always become isolated new cards:

- `LLM-Perceived Question Difficulty`
- `Difficulty Perception in Large Language Models`
- `Question difficulty perception`

At least one candidate from the second paper should be recommended for merge or link with a core card created by the first paper.

## Phase 4: Add Qwen 27B Merge Planner Agent

### Goal

Split the current merger into:

```text
Merge Planner Agent: Qwen 27B produces merge_plan
Merge Executor: Python executes merge_plan
```

### Model Configuration

Add:

```env
SILICONFLOW_MERGE_MODEL=Qwen/Qwen3.6-27B
```

Add a dependency helper:

```python
get_merge_llm()
```

Recommended generation settings:

```text
temperature = 0
max_tokens = 2500-4000
```

### Merge Planner Input

The merge planner prompt must include:

- approved candidate
- reviewer report
- target existing card, if any
- existing card `content_json`
- existing evidence summaries
- incoming evidence
- source paper title
- existing aliases

### Merge Planner Output Schema

The model must return strict JSON:

```json
{
  "action": "update_existing",
  "target_card_id": "",
  "field_updates": {
    "definition": {
      "mode": "keep",
      "text": ""
    },
    "mechanism": {
      "mode": "append",
      "text": ""
    },
    "method": {
      "mode": "append",
      "text": ""
    },
    "findings": {
      "mode": "append",
      "text": ""
    },
    "limitations": {
      "mode": "append",
      "text": ""
    },
    "key_takeaways": {
      "mode": "append_list",
      "items": []
    }
  },
  "aliases_to_add": [],
  "links_to_add": [
    {
      "to_card_id": "",
      "relation_type": "introduces",
      "reason": ""
    }
  ],
  "reason": "",
  "confidence": 0.0
}
```

Allowed `action` values:

- `create_new`
- `update_existing`
- `link_only`
- `needs_human_review`

Allowed field update modes:

- `keep`
- `replace`
- `append`
- `append_list`

### Merge Executor Requirements

The deterministic Python executor remains responsible for:

- creating or updating `wiki_pages`
- writing `wiki_card_sources`
- writing `wiki_card_links`
- writing `wiki_aliases`
- updating `PaperPage.content_json.import_impact`
- rebuilding `wiki_chunks`
- writing generated Wiki markdown to OSS

The model must not write to the database directly.

### Acceptance Criteria

After importing the second paper:

- At least one existing core concept card is updated.
- `PaperPage.content_json.import_impact.updated_cards` is not empty.
- `wiki_card_sources` shows evidence from both papers for the shared core card.
- `wiki_card_links` shows both PaperPages pointing to the shared core card.

## Phase 5: Expand Distilled Card Content

### Goal

Make generated cards useful for interview demos. `key_idea` and `method` must not be one-line summaries.

### Distillation Prompt Requirements

Update:

```text
system/wiki/paper_pipeline/distiller.py
```

PaperPage field requirements:

- `problem`: 3-4 lines
- `key_idea`: 5-6 lines
- `method`: 5-6 lines
- `results`: 3-5 lines
- `limitations`: 2-4 lines
- `key_takeaways`: 3-6 items

ConceptPage field requirements:

- `definition`: 3-5 lines
- `mechanism`: 4-6 lines
- `findings`: 2-4 lines
- `limitations`: may be empty, but must not be invented
- `key_takeaways`: 2-5 items

MethodPage field requirements:

- `definition`: 2-4 lines
- `method`: 5-7 lines
- `mechanism`: 4-6 lines
- `findings`: 2-4 lines
- `limitations`: 2-4 lines
- `key_takeaways`: 2-5 items

### Prompt Constraints

Keep these hard rules:

- Do not invent authors, datasets, metrics, numbers, or results.
- Every claim must have evidence.
- If evidence is insufficient, leave the field empty instead of guessing.
- Write explanations in Chinese if the product UI remains Chinese.
- Keep paper titles, method names, model names, and metric names in English.

### Acceptance Criteria

On the frontend PaperPage:

- `key_idea` displays at least 5 lines of meaningful content.
- `method` displays at least 5 lines of meaningful content.
- `results` is not a single vague sentence.
- The content is suitable for an interview explanation.

## Phase 6: Add Keyword Jump Navigation

### Goal

Important concepts in Wiki card text should be clickable and navigate to their corresponding cards.

### Backend Option

Add:

```text
GET /api/wiki/aliases
```

Response:

```json
{
  "items": [
    {
      "card_id": "...",
      "title": "LLM-Perceived Question Difficulty",
      "alias": "question difficulty perception",
      "normalized_alias": "question difficulty perception",
      "page_type": "ConceptPage"
    }
  ]
}
```

Alternatively, include `keyword_links` in:

```text
GET /api/wiki/{card_id}
```

### Keyword Sources

Keywords should come from:

- `wiki_pages.title`
- `wiki_aliases.alias`
- `content_json.aliases`
- `related_topics`

### Frontend Behavior

Update:

```text
frontend/src/pages/KnowledgeVault.vue
```

Rendering rules:

- Perform keyword matching on long text fields.
- Prefer longest match first.
- Do not link the current card to itself.
- Clicking a keyword opens the target card detail view.

### Visual Requirements

Keyword links should:

- avoid heavy underlines
- use a subtle color or background
- show a hover state
- remain readable in the current dark UI

### Acceptance Criteria

On a paper or concept page:

- `Hidden representations` jumps to its concept card.
- `LLM-Perceived Question Difficulty` jumps to its concept card.
- `Difficulty estimation` jumps to its method card.

## Phase 7: Improve Frontend Typography and Card Layout

### Goal

Improve readability and demo quality. The current detail page has oversized titles, schema-like field rendering, and weak hierarchy.

### Main Files

Update:

```text
frontend/src/pages/KnowledgeVault.vue
frontend/src/pages/CaptureNote.vue
```

### Typography Guidelines

Recommended ranges:

- Paper title: `32-40px`; avoid taking half the viewport.
- Section headings: `20-24px`.
- Body text: `16-18px`.
- Small labels: `13-14px`.
- Line height: `1.65-1.8`.

### Fields to Hide or De-emphasize

Do not prominently display internal fields:

- `schema_version`
- `compile_status`
- `source_packet_id`
- `raw_source_path`
- `pdf_storage_uri`
- `compiler_model`
- internal pipeline fields

### Fields to Prioritize

Prioritize:

- Problem
- Key idea
- Method
- Results
- Limitations
- Key takeaways
- Paper knowledge merge impact
- Source evidence
- Related cards

### Import Impact Layout

Display as a compact module:

```text
Paper knowledge merge
Created 4    Updated 1    Linked 4    Rejected 0
```

The list should distinguish:

- created
- updated
- linked

The same card id should not appear repeatedly in one impact list.

### Suggested Detail Layout

```text
Paper title
primary / paper

Paper knowledge merge
created / updated / linked / rejected

Problem
...

Key idea
...

Method
...

Results
...

Limitations
...

Key takeaways
...
```

## Phase 8: Final Acceptance Run

### Startup

Start Docling:

```powershell
docker compose -f docker-compose.docling.yml up -d
```

Start backend:

```powershell
python -m uvicorn backend.app:app --app-dir <repo-root> --host 127.0.0.1 --port 8000
```

Start frontend:

```powershell
cd frontend
npm run dev -- --host 0.0.0.0
```

Reset the demo knowledge base:

```powershell
python scripts/reset_demo_knowledge_base.py --execute --keep-pdfs --clear-oss-generated --clear-pipeline-db
```

### Test Papers

Use exactly:

```text
data/The LLM Already Knows_ Estimating LLM-Perceived Question Difficulty via Hidden Representations.pdf
data/Probing the Difficulty Perception Mechanism of Large Language Models.pdf
```

### First Paper Acceptance Criteria

After importing the first paper:

- `parser = docling-remote`
- one `PaperPage` is created
- multiple `ConceptPage` / `MethodPage` cards are created
- `review_rejections = 0`, or only clearly justified rejections
- `key_idea` and `method` have 5-6 display-ready lines

### Second Paper Acceptance Criteria

After importing the second paper:

- `parser = docling-remote`
- at least one card from the first paper is updated
- `import_impact.updated_cards.length >= 1`
- `LLM-Perceived Question Difficulty` has evidence from both papers
- both PaperPages link to the shared core card

### Frontend Acceptance Criteria

- `/vault` shows both PaperPages and the generated knowledge cards.
- Opening the second PaperPage shows created/updated/linked/rejected impact.
- Clicking keywords navigates to the correct card.
- Internal schema fields are hidden or de-emphasized.
- Typography and spacing are suitable for an interview demo.

### Code Verification

Run:

```powershell
python -m py_compile system/wiki/paper_pipeline/*.py system/wiki/markdown_vault.py backend/api/papers.py backend/api/wiki.py
cd frontend
npm run build
```

## Risks and Fallbacks

### Docling Risks

Risks:

- Docker is not running.
- Docling times out.
- Docling markdown structure is unstable.

Fallbacks:

- Keep `PaperIndexParser` fallback.
- Always return the `parser` field in API responses.
- Start Docker and run the health check before the interview demo.

### Qwen 27B Risks

Risks:

- the model id is unavailable
- JSON output is invalid
- latency increases

Fallbacks:

- Use deterministic reviewer/merger when JSON parsing fails.
- Mark low-confidence cases as `needs_human_review`.
- Keep the current deterministic path as a safe fallback.

### Merge Risks

Risks:

- The model incorrectly merges different concepts.
- The model recommends a non-existing target card.

Fallbacks:

- The Python executor must validate target cards.
- Do not auto-merge below a confidence threshold, such as `0.8`.
- Prefer append-only updates for evidence-backed fields.

## Recommended Implementation Order

1. Implement the reset script.
2. Finalize Docling remote config and health check.
3. Ensure paper extraction returns `docling-remote`.
4. Add the Qwen Reviewer Agent.
5. Add the Qwen Merge Planner Agent.
6. Expand the distillation prompt.
7. Add keyword navigation API and frontend rendering.
8. Improve frontend typography, field display, and import-impact layout.
9. Reset the knowledge base and rerun both test papers.
10. Record final API output, DB counts, OSS file list, and frontend screenshots.

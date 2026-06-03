# Paper four-agent Wiki implementation plan

## Goal

Refactor the paper ingestion path into four strict stages:

1. Extraction agent: parse a paper into source-grounded structured text.
2. Distillation agent: turn extracted text into candidate Wiki knowledge.
3. Review agent: reject or repair unsupported, malformed, or duplicate-prone candidates.
4. Merge agent: merge reviewed candidates into the existing Wiki structure or create new cards.

Scope for the first implementation is paper-only. Xiaohongshu, image notes, and generic text notes should keep using the current path until the paper pipeline proves useful.

The user-visible effect should be:

- Importing the first paper creates a PaperPage plus reusable ConceptPage/MethodPage cards.
- Importing a related second paper reuses and updates existing concept/method cards instead of creating isolated summaries only.
- The paper page shows what was newly created, what was updated, and what existing knowledge it linked to.

## Test papers

Use these two local PDFs as the first acceptance test:

- `data/The LLM Already Knows_ Estimating LLM-Perceived Question Difficulty via Hidden Representations.pdf`
- `data/Probing the Difficulty Perception Mechanism of Large Language Models.pdf`

Expected theme overlap: LLM difficulty perception, hidden representations, question difficulty estimation, probing/internal mechanism analysis. The second import should update or link to concepts created by the first import instead of duplicating all of them.

## Current code mapping

Current useful pieces to keep:

- `system/document/docling_parser.py`: PDF parsing.
- `backend/api/papers.py`: paper upload/index endpoints.
- `system/wiki/raw_source_vault.py`: raw Markdown storage in OSS/local object storage.
- `system/wiki/wiki_builder.py`: current LLM compilation logic, useful as a starting point for distillation prompts.
- `system/wiki/wiki_store.py`: card storage and Markdown generation.
- `system/wiki/chunk_index.py`: chunk indexing for Wiki Chat.
- `system/storage/object_storage.py`: OSS path handling under `users/admin`.

Current missing pieces:

- No durable source packet table.
- No candidate knowledge table.
- No review gate.
- No card-to-source evidence table.
- No card-to-card link table.
- No alias table for merge resolution.
- Merge currently means mostly `create_card` / dedupe by title + URL, not knowledge-level merge.

## Core principle

Do not implement this as four unconstrained chat agents. Implement it as four services/workers with strict Pydantic models and JSON-only LLM calls.

Every stage should write its artifact to the database so failures can be inspected and rerun without reparsing the PDF whenever possible.

## Data model

Add tables in `sessions.db` through `WikiStore` or a new `PaperWikiPipelineStore`.

### `source_packets`

Stores the extraction result for one source.

Fields:

- `id TEXT PRIMARY KEY`
- `source_type TEXT NOT NULL` -- first version: `paper_pdf`
- `title TEXT NOT NULL`
- `source_urls_json TEXT DEFAULT '[]'`
- `raw_source_path TEXT DEFAULT ''`
- `pdf_storage_uri TEXT DEFAULT ''`
- `parser_used TEXT DEFAULT ''`
- `packet_json TEXT NOT NULL`
- `source_hash TEXT DEFAULT ''`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

`packet_json` shape:

```json
{
  "source_id": "...",
  "source_type": "paper_pdf",
  "title": "...",
  "abstract": "...",
  "metadata": {
    "authors": [],
    "year": "",
    "venue": "",
    "arxiv_id": ""
  },
  "raw_source_path": "oss://...",
  "pdf_storage_uri": "oss://...",
  "sections": [
    {
      "section_id": "abstract",
      "heading": "Abstract",
      "text": "...",
      "page_start": 1,
      "page_end": 1
    }
  ],
  "figures": [],
  "tables": []
}
```

### `distilled_candidates`

Stores candidate knowledge before review.

Fields:

- `id TEXT PRIMARY KEY`
- `source_packet_id TEXT NOT NULL`
- `candidate_type TEXT NOT NULL` -- `paper_page`, `concept_card`, `method_card`
- `title TEXT NOT NULL`
- `candidate_json TEXT NOT NULL`
- `status TEXT DEFAULT 'pending_review'`
- `created_at TEXT NOT NULL`

Candidate JSON shape:

```json
{
  "page_type": "ConceptPage",
  "title": "LLM-perceived question difficulty",
  "aliases": ["perceived difficulty", "question difficulty perception"],
  "summary": "...",
  "content_json": {
    "schema_version": "paper-wiki-v1",
    "definition": "...",
    "mechanism": "...",
    "method": "...",
    "findings": "...",
    "limitations": "",
    "key_takeaways": []
  },
  "claims": [
    {
      "claim": "...",
      "evidence": "...",
      "section_id": "method",
      "page_start": 3
    }
  ],
  "related_topics": [],
  "source_level": "primary"
}
```

### `review_reports`

Stores review decision and repair instructions.

Fields:

- `id TEXT PRIMARY KEY`
- `candidate_id TEXT NOT NULL`
- `status TEXT NOT NULL` -- `approved`, `needs_revision`, `rejected`
- `report_json TEXT NOT NULL`
- `created_at TEXT NOT NULL`

Report JSON shape:

```json
{
  "status": "approved",
  "schema_errors": [],
  "unsupported_claims": [],
  "evidence_quality": "good",
  "duplicate_candidates": [
    {
      "existing_card_id": "...",
      "existing_title": "LLM-perceived question difficulty",
      "confidence": 0.91,
      "reason": "normalized title and alias match"
    }
  ],
  "merge_recommendation": {
    "action": "update_existing",
    "target_card_id": "...",
    "confidence": 0.91
  }
}
```

### `wiki_card_sources`

Connects durable Wiki cards to paper evidence.

Fields:

- `id TEXT PRIMARY KEY`
- `card_id TEXT NOT NULL`
- `source_card_id TEXT DEFAULT ''` -- PaperPage card id
- `source_packet_id TEXT NOT NULL`
- `raw_source_path TEXT DEFAULT ''`
- `source_url TEXT DEFAULT ''`
- `section_id TEXT DEFAULT ''`
- `evidence_text TEXT DEFAULT ''`
- `claim_text TEXT DEFAULT ''`
- `confidence REAL DEFAULT 0`
- `created_at TEXT NOT NULL`

### `wiki_card_links`

Connects Wiki cards to each other.

Fields:

- `id TEXT PRIMARY KEY`
- `from_card_id TEXT NOT NULL`
- `to_card_id TEXT NOT NULL`
- `relation_type TEXT NOT NULL` -- `introduces`, `uses`, `extends`, `compares`, `evidence_for`
- `source_packet_id TEXT DEFAULT ''`
- `evidence_text TEXT DEFAULT ''`
- `created_at TEXT NOT NULL`

### `wiki_aliases`

Enables deterministic merge resolution.

Fields:

- `card_id TEXT NOT NULL`
- `alias TEXT NOT NULL`
- `normalized_alias TEXT NOT NULL`
- unique index on `normalized_alias`

## Backend services

Create a paper pipeline package:

```txt
system/wiki/paper_pipeline/
  __init__.py
  models.py
  store.py
  extractor.py
  distiller.py
  reviewer.py
  merger.py
  orchestrator.py
```

### `models.py`

Define Pydantic models for:

- `SourcePacket`
- `SourceSection`
- `DistilledCandidate`
- `CandidateClaim`
- `ReviewReport`
- `MergePlan`
- `MergeResult`

Keep model names stable because these are the cross-agent contracts.

### `extractor.py`

Responsibilities:

- Resolve local PDF.
- Fetch arXiv metadata when available.
- Validate PDF title vs source URL.
- Run Docling or fallback parser.
- Build section-aware `SourcePacket`.
- Save raw Markdown through `RawSourceVault`.
- Upload PDF to OSS through `get_object_storage()`.
- Store packet in `source_packets`.

It must not summarize or create user-facing Wiki content.

### `distiller.py`

Responsibilities:

- Input: `SourcePacket`.
- Output:
  - one `paper_page` candidate
  - 5-12 `concept_card` / `method_card` candidates
- Every claim must include evidence text and section id.
- Use `get_summary_llm()` with temperature `0.0`.
- Use a strict paper-only prompt.

Distiller should be allowed to leave fields empty when evidence is missing. It must not invent details.

Suggested candidate categories:

- `core_concept`
- `method`
- `metric`
- `dataset`
- `finding`
- `limitation`

For V1, only turn `core_concept` and `method` into independent cards. Keep metrics/datasets inside PaperPage unless they recur.

### `reviewer.py`

Responsibilities:

- Validate JSON schema.
- Check each claim has evidence.
- Reject claims that are unsupported by provided evidence.
- Normalize title and aliases.
- Check duplicates against `wiki_aliases` and existing `wiki_pages`.
- Produce `ReviewReport`.

For V1, review can combine deterministic checks plus one LLM review pass.

Deterministic checks:

- `title` not empty.
- `page_type` in allowed card types.
- no claim without evidence.
- `summary` length at least 30 for approved cards.
- no raw base64/image garbage.
- aliases normalized.

LLM review prompt should be narrow:

- "Given candidate and evidence snippets, mark unsupported claims."
- It should not rewrite the card unless status is `needs_revision`.

### `merger.py`

Responsibilities:

- Input: approved candidates + review report.
- Create PaperPage.
- Create or update ConceptPage/MethodPage.
- Add aliases.
- Add `wiki_card_sources`.
- Add `wiki_card_links`.
- Reindex changed cards.

Merge rules:

1. PaperPage is unique by source URL or PDF hash.
2. ConceptPage/MethodPage uses alias/title matching first.
3. If exact alias match exists, update existing card.
4. If no match and review confidence is high, create new card.
5. If duplicate confidence is ambiguous, create a `needs_human_review` report and do not merge automatically.

For V1, avoid embedding-based merge unless existing embeddings are already available. Start with normalized title/alias matching plus LLM duplicate suggestion in review.

### `orchestrator.py`

One entrypoint:

```python
run_paper_pipeline(pdf_path: Path, source_url: str) -> PaperPipelineResult
```

Result shape:

```json
{
  "ok": true,
  "source_packet_id": "...",
  "paper_card_id": "...",
  "created_cards": [],
  "updated_cards": [],
  "linked_cards": [],
  "review_rejections": [],
  "timings": {
    "extract_seconds": 0,
    "distill_seconds": 0,
    "review_seconds": 0,
    "merge_seconds": 0
  }
}
```

## API changes

Keep existing endpoint:

- `POST /api/papers/index-local`

Add optional payload field:

```json
{
  "local_path": "...",
  "source_url": "...",
  "pipeline": "four_agent"
}
```

Default can remain current behavior until the new pipeline is stable. For the goal implementation, use `pipeline=four_agent` in tests.

Return extra fields:

```json
{
  "ok": true,
  "pipeline": "four_agent",
  "paper_card_id": "...",
  "created_cards": [
    {"id": "...", "title": "...", "page_type": "ConceptPage"}
  ],
  "updated_cards": [
    {"id": "...", "title": "...", "page_type": "ConceptPage"}
  ],
  "linked_cards": [
    {"from": "PaperPage id", "to": "ConceptPage id", "relation_type": "introduces"}
  ],
  "review_rejections": [],
  "timings": {}
}
```

## Frontend changes

Do not redesign the whole Knowledge Vault for V1.

Add import result display on the capture page after paper import:

- `PaperPage created/updated`
- `Created knowledge cards`
- `Updated existing cards`
- `Linked existing cards`
- `Rejected candidates`
- total elapsed time

Add a small section on PaperPage detail in `KnowledgeVault.vue`:

- "本篇论文新增"
- "本篇论文更新"
- "本篇论文关联"

This can be powered by either API response stored in content JSON or a new endpoint:

- `GET /api/wiki/{card_id}/links`

## Prompt strategy

### Extraction prompt

Avoid LLM unless section extraction needs cleanup. Prefer deterministic parser output.

### Distillation prompt

Rules:

- Chinese explanations.
- Keep technical terms in original language.
- JSON only.
- Every candidate must include `claims[]`.
- Every claim must include direct evidence from source sections.
- Do not create more than 12 candidates.
- Prefer reusable concepts/methods over paper-specific trivia.

### Review prompt

Rules:

- Do not add new knowledge.
- Mark unsupported claims.
- Identify duplicate candidates.
- Return `approved`, `needs_revision`, or `rejected`.

### Merge prompt

Avoid broad merge prompts. Merge should be deterministic where possible. If LLM is used, only ask it to choose between explicit candidate targets.

## Acceptance test flow

Start from a clean generated Wiki state for the test.

Import first paper:

```powershell
$body = @{
  local_path = 'data\\The LLM Already Knows_ Estimating LLM-Perceived Question Difficulty via Hidden Representations.pdf'
  source_url = ''
  pipeline = 'four_agent'
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/papers/index-local' -ContentType 'application/json' -Body $body -TimeoutSec 420
```

Expected after first import:

- one PaperPage exists for `The LLM Already Knows...`
- at least 4 ConceptPage/MethodPage cards created
- candidate/review records exist
- every created concept/method has at least one `wiki_card_sources` evidence row
- no generated card has `compile_status=schema_fallback`

Import second paper:

```powershell
$body = @{
  local_path = 'data\\Probing the Difficulty Perception Mechanism of Large Language Models.pdf'
  source_url = ''
  pipeline = 'four_agent'
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/papers/index-local' -ContentType 'application/json' -Body $body -TimeoutSec 420
```

Expected after second import:

- one PaperPage exists for `Probing the Difficulty Perception Mechanism...`
- at least one existing difficulty-perception related concept/method card is updated, not duplicated
- `wiki_card_sources` for shared concept cards includes evidence from both papers
- PaperPage links point to reused concept/method cards
- frontend import result shows created vs updated vs linked cards

Suggested shared cards to watch for:

- `LLM-perceived question difficulty`
- `Question difficulty perception`
- `Hidden representations`
- `Difficulty estimation`
- `Probing LLM internal mechanisms`

Do not require exact card titles in tests, because the LLM may choose slightly different naming. Tests should check aliases and links.

## Validation commands

Database counts:

```powershell
python -c "import sqlite3; conn=sqlite3.connect('sessions.db'); \
for t in ['wiki_pages','wiki_chunks','papers','paper_blocks','source_packets','distilled_candidates','review_reports','wiki_card_sources','wiki_card_links','wiki_aliases']: \
    print(t, conn.execute(f'SELECT count(*) FROM {t}').fetchone()[0]); \
conn.close()"
```

Check current cards:

```powershell
curl.exe -s http://127.0.0.1:5173/api/wiki
```

Check links for a paper:

```powershell
curl.exe -s http://127.0.0.1:8000/api/wiki/<paper_card_id>/links
```

## Completion criteria

The implementation is complete only when:

- Both test PDFs can be imported through the four-agent pipeline.
- Both imports finish without fallback.
- The second import updates or links to at least one card created by the first import.
- Every merged ConceptPage/MethodPage has evidence rows pointing to source sections.
- The frontend shows import impact: created, updated, linked, rejected.
- OSS output stays under `users/admin/data/...`.
- Existing paper import path still works or is intentionally replaced behind the same endpoint.

## Risks and guardrails

- Do not let Merge Agent rewrite unsupported knowledge. It should only merge reviewed candidates.
- Do not rely on LLM duplicate matching alone. Use normalized aliases first.
- Do not create cards for every noun phrase. Limit to reusable concepts/methods.
- Do not block import forever on review. If review fails, create the PaperPage and store rejected candidates for inspection.
- Do not remove raw source. Raw source is the durable replay layer.
- Do not mix Xiaohongshu into this implementation. Paper-only first.


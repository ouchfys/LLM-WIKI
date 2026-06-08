# Paper Pipeline Merge Planner and Markdown-First Runbook

## Purpose

This runbook describes the next implementation steps for the paper ingestion pipeline. The current four-stage pipeline is valuable and should be preserved:

```text
extract -> distill -> review -> merge -> wiki graph -> retrieval/evaluation
```

The immediate issue is not the pipeline shape. The immediate issue is that the merge planner LLM is not currently wired correctly, so the merge stage is mostly using deterministic fallback logic. After that is fixed, the longer-term architecture should move the wiki authority layer from SQLite-first to Markdown-first.

## Current State

The project already has the core ingredients of a serious LLM-maintained research wiki:

- Docling-based PDF extraction with fallback parsing.
- Source packets and raw source preservation.
- LLM distillation into paper, concept, and method candidates.
- Reviewer reports and schema/evidence checks.
- Merge planning, aliases, card links, source evidence, chunks, and benchmark traces.
- OSS-backed durable storage for source/wiki/query artifacts.
- Frontend surfaces for capture, knowledge vault, wiki chat, and evaluation.

However, the current canonical write path is still DB-first:

```text
candidate -> wiki_pages/content_json -> render Markdown -> index chunks
```

The Markdown files exist and are useful, but they are generated from SQLite card fields. They are not yet the canonical source of truth.

## P0: Fix Merge Planner LLM Wiring

### Problem

`backend/deps.py` currently defines `get_merge_llm()` but returns nothing when `SiliconFlowChat` is available. The merge model initialization code is also placed after a return path inside `get_maintenance_fast_llm()`, making it unreachable.

This means the paper merge stage usually receives `merge_llm=None`, so `PaperMergeAgent` falls back to deterministic merge planning. This matches observed ingestion logs where `merge_llm_calls` stays at `0.0`.

### Required Change

Update `get_merge_llm()` so it constructs and returns the configured merge model:

```python
@lru_cache(maxsize=1)
def get_merge_llm():
    """Dedicated deterministic merge-planning model for paper cards."""
    if SiliconFlowChat is None:
        return None
    try:
        return SiliconFlowChat(
            model=SILICONFLOW_MERGE_MODEL,
            temperature=0.0,
            max_tokens=3600,
        )
    except Exception as exc:
        print(f"[deps] Merge LLM unavailable: {exc}")
        return None
```

Then remove the unreachable merge-model block from `get_maintenance_fast_llm()`.

### Acceptance Criteria

- `get_merge_llm()` returns a `SiliconFlowChat` instance when the API client is available and credentials/config are valid.
- `get_maintenance_fast_llm()` only handles the maintenance fast model.
- A paper ingestion run with a non-trivial duplicate/update case can produce `merge_llm_calls > 0.0` when deterministic confidence is below the high-confidence fallback threshold.
- If the merge LLM is unavailable, the pipeline still falls back safely to deterministic planning and logs the failure.

## P1: Verify the Four-Stage Pipeline Honestly

After P0, run or replay one paper ingestion and inspect the result.

### What to Check

- Extract stage produced a source packet and raw source artifact.
- Distill stage produced meaningful candidates.
- Review stage recorded reviewer reports.
- Merge stage used the LLM planner when appropriate.
- New or updated cards have source evidence links.
- `WikiChunkIndex.reindex_card()` ran for changed cards.
- The frontend Knowledge Vault can open the resulting card and show import impact.

### Expected Claim After P1

Use this wording in demos and interviews:

> The project has a four-stage paper ingestion pipeline. Extraction and distillation are already active; review and merge combine deterministic guardrails with LLM planning where available. The merge planner wiring was fixed so the LLM merge path can be exercised instead of silently falling back to deterministic behavior.

Avoid claiming that every merge decision is always LLM-driven. The deterministic fallback is intentional and useful.

## P2: Define the Markdown Wiki Schema

Before changing the merge target, define the Markdown contract. The schema should be simple enough for an LLM to maintain and strict enough for a parser to reindex.

### Frontmatter

Recommended fields:

```yaml
---
id: <stable-card-id>
title: <human-readable-title>
type: PaperPage | ConceptPage | MethodPage | ComparePage | InterviewQA | MistakeNote | StudyPlan | SourceNote
status: draft | reviewed | verified
created: YYYY-MM-DD
updated: YYYY-MM-DD
source_level: primary | secondary | inferred | user_selection
aliases:
  - <alias>
tags:
  - <tag>
sources:
  - url: <source-url-or-storage-uri>
    level: primary | secondary | inferred
    source_packet_id: <optional-source-packet-id>
related:
  - <card-id-or-wikilink>
---
```

### Body Sections

Recommended common sections:

```text
# Title

## Summary

## Key Ideas

## Evidence

## Links

## Notes

## Review Status
```

Page-type-specific sections can be added later, but the parser should only require a small common core at first.

### Acceptance Criteria

- Existing `MarkdownVault.render_card()` can render the new schema.
- A future parser can recover card identity, type, summary, aliases, sources, and related links from Markdown alone.
- The schema avoids embedding large opaque JSON blobs as the main content body.

## P3: Build Markdown Parser and SQLite Reindexer

The next layer is a deterministic reindexer that treats Markdown as canonical input and SQLite as a cache.

### Target Flow

```text
wiki/*.md -> parse frontmatter/body -> upsert wiki_pages/wiki_aliases/wiki_card_links/wiki_card_sources/wiki_chunks
```

### Required Components

- A Markdown parser that reads frontmatter and body sections.
- A card upsert path that can preserve stable IDs from Markdown.
- Alias extraction from frontmatter.
- Link extraction from `related:` and `[[wikilinks]]`.
- Source extraction from frontmatter and evidence sections.
- Chunk indexing from Markdown body text.
- Validation errors for missing IDs, duplicate IDs, broken source references, and malformed frontmatter.

### Acceptance Criteria

- Deleting and rebuilding SQLite from Markdown produces the same visible Knowledge Vault cards for a sample set.
- Query indices can be regenerated from the rebuilt SQLite tables.
- Manual edits to a Markdown card can be reflected in the UI after reindexing.

## P4: Move Merge Target to Markdown Patch

Only after P0-P3 are stable should the merge agent stop writing SQLite card fields directly.

### Future Flow

```text
candidate -> merge plan -> Markdown patch -> write wiki/*.md -> parse/reindex SQLite -> generate query indices
```

### Merge Agent Responsibilities

- Choose whether to create, update, link, skip duplicate, or require human review.
- Generate a Markdown patch or complete Markdown replacement for the target card.
- Preserve source evidence and review history in readable sections.
- Avoid direct writes to `wiki_pages.content_json` except through the reindexer.

### Deterministic Merger Responsibilities

- Validate the patch target and card ID.
- Apply the patch or write the new Markdown file.
- Run Markdown validation.
- Reindex SQLite.
- Regenerate query indices.
- Record merge audit metadata.

### Acceptance Criteria

- A paper ingestion can create or update Markdown cards as the canonical artifact.
- SQLite state is derived from Markdown after the merge.
- The frontend still works without major UI changes.
- Evaluation and wiki chat continue to use SQLite/chunk indices as retrieval caches.

## Recommended Priority

```text
P0: Fix get_merge_llm() and remove unreachable merge-model code.
P1: Run one paper ingestion and confirm the merge LLM path can be exercised.
P2: Define the Markdown frontmatter/body schema.
P3: Build Markdown -> SQLite reindexing.
P4: Change merge target from DB content_json to Markdown patch.
```

## Interview Positioning

Use this framing:

> The current version is a DB-backed LLM Wiki Graph. I chose SQLite first to make web rendering, retrieval, evidence links, aliases, and benchmarks work quickly. The next step is to move the canonical knowledge layer to Markdown patches while keeping SQLite as the index and graph cache. That gives the system both product-grade retrieval and a Karpathy-style human-readable, diffable, LLM-maintained wiki.

This is the most accurate story: the four-agent pipeline is real and worth keeping, but the canonical knowledge layer should evolve from DB-first to Markdown-first.

# Karpathy Style Web Wiki Minimal Plan

## 1. Decision

The product should first replicate the core Karpathy-style LLM Wiki idea:

```text
Raw sources -> Markdown pages -> human-readable Wiki -> LLM reads/writes Wiki
```

Do not make GraphRAG, vector RAG, page-level chunk rerank, or knowledge graph UI the main product path.

The main product is a private Markdown Wiki with an LLM assistant.

## 2. What We Are Copying

The important idea is not Obsidian itself. The important idea is:

- Knowledge is stored as plain Markdown pages.
- Pages are readable and editable by humans.
- The LLM can read, write, revise, and link pages.
- The Wiki improves over time because useful answers become durable notes.
- Raw sources are preserved, but the user mainly interacts with compiled notes.

In this project, the Web UI replaces Obsidian as the main display layer.

## 3. What We Are Not Doing First

For the first replication pass, do not prioritize:

- GraphRAG.
- Knowledge graph visualization.
- Multi-stage reranking.
- Complex paper chunk scoring.
- Enterprise document ingestion.
- A full Obsidian dependency.

These can remain optional internal tools, but they are not the product center.

## 4. Storage Model

The Wiki is file-first.

```text
data/wiki/
  papers/
  concepts/
  methods/
  comparisons/
  interview/
  sources/
  raw_sources/
  attachments/
```

SQLite is only the catalog and search index:

```text
wiki_pages:
  id, title, page_type, markdown_path, source_urls, summary

wiki_chunks:
  card_id, section, text, markdown_path
```

Markdown and attachments are the durable knowledge source. SQLite is not the source of truth.

For online deployment, the same layout maps to OSS:

```text
oss://fysjarvis/dev/users/admin/wiki/...
oss://fysjarvis/dev/users/admin/attachments/...
oss://fysjarvis/dev/users/admin/raw_sources/...
```

## 5. Ingestion Rule

Every source must become Markdown.

Sources:

- PDF paper.
- Xiaohongshu share text and screenshots.
- Uploaded images.
- Blog/article text.
- Interview notes.
- Bilibili transcript or manually pasted video notes.

Output:

```text
Raw source Markdown:
  data/wiki/raw_sources/<source_type>/<source_id>.md

Compiled Wiki pages:
  data/wiki/papers/<paper>.md
  data/wiki/methods/<method>.md
  data/wiki/concepts/<concept>.md
  data/wiki/comparisons/<compare>.md
  data/wiki/interview/<qa>.md
```

The system should not rely on raw PDF chunks as the normal answer path.

## 6. Paper Handling

For papers, the first pass should produce a readable paper page:

```markdown
# Paper Title

## Why This Matters

## Core Idea

## Method

## Key Figures

## Key Tables

## Main Takeaways

## Interview Questions

## Related Pages
```

If possible, also create small pages:

```text
methods/grpo.md
comparisons/grpo-vs-ppo.md
concepts/rule-based-reward.md
interview/grpo-advantage-estimation.md
```

This is the Karpathy-style move: compile knowledge into durable notes instead of querying the raw paper every time.

## 7. Image And Table Handling

Images and tables are preserved as attachments.

Markdown should reference them:

```markdown
![Figure 3: PPO vs GRPO](../attachments/papers/grpo/figure_003.png)
```

The page should also contain text that a normal text LLM can use:

```markdown
### Figure 3 Notes

This figure compares PPO and GRPO. PPO uses a value model and GAE to estimate advantages.
GRPO removes the value model and estimates advantages from group scores sampled for the same query.
```

If no multimodal model is available, use OCR, captions, and manual/LLM-written notes. When a VLM is available, use it only at ingestion time to write figure notes.

## 8. Chat Rule

The assistant should behave like a Wiki copilot:

```text
User question
  -> find relevant Markdown pages
  -> read page text
  -> answer from Wiki
  -> if answer is useful, optionally save/update Wiki
```

Raw source search is only a fallback:

```text
If Wiki pages are missing or too weak:
  -> inspect raw source Markdown / PDF-derived text
  -> answer with caveat
  -> suggest compiling a new Wiki page
```

The answer should cite pages, not arbitrary chunks, whenever possible.

## 9. Retrieval Philosophy

Some search is still necessary, but it is not the product concept.

Allowed:

- Filename search.
- Title search.
- Full-text Markdown search.
- Simple page-level matching.

Not first priority:

- Dense vector search.
- Rerank-heavy pipelines.
- Complex chunk-level paper QA.

If a query needs exact source verification, use raw source audit as a tool, not as the default experience.

## 10. Minimal Implementation Target

Phase 1 should complete these:

1. All ingestion paths write Markdown files.
2. The Web UI can browse Markdown Wiki pages.
3. Wiki Chat retrieves and reads relevant Markdown pages.
4. Chat answers can be saved back into the Wiki.
5. Paper ingestion produces a compiled paper note, not only raw extracted text.
6. Attachments are stored locally or in OSS and referenced from Markdown.

This is enough to demonstrate the core LLM Wiki idea.

## 11. What To Remove From The Main Story

Do not sell the project as:

```text
GraphRAG paper QA system
```

Sell it as:

```text
Private Jarvis Notes: an LLM-maintained personal Markdown Wiki for papers, interview notes, posts, and learning materials.
```

GraphRAG, vector retrieval, and rerank can be mentioned as explored but intentionally de-emphasized because the personal Wiki product needs fast, editable, durable notes.

## 12. Next Code Direction

The next code changes should be:

1. Add a `WikiCompiler` that turns raw source Markdown into one or more compiled Wiki pages.
2. Make PDF/XHS/image/text ingestion call the compiler after raw source creation.
3. Make Wiki Chat prefer compiled page content over raw chunks.
4. Keep raw source search as an explicit fallback tool.
5. Add "save this answer to Wiki" and "update existing page" flows.

That is the minimum faithful replication.

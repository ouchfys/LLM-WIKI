# Retrieval / QA Testset

Date: 2026-05-30

This testset is for the current private wiki pipeline:

`capture/import -> raw markdown -> wiki page -> wiki_chunks FTS5 -> /api/wiki/chat`

It covers three source classes:

1. Local paper PDFs already under `data/`
2. Xiaohongshu interview notes imported by direct `discovery/item` URL
3. Xiaohongshu interview notes imported by `xhslink.com` short share URL

## Test Items

| ID | Type | Input | Goal |
| --- | --- | --- | --- |
| P1 | paper | `data/attention is all you need.pdf` | Verify paper ingestion and theory QA |
| P2 | paper | `data/Does Reinforcement Learning Really Incentivize Reasoning Capacity in LLMs Beyond the Base Model?.pdf` | Verify paper ingestion on another PDF |
| X1 | xhs direct | `https://www.xiaohongshu.com/discovery/item/69c1279f000000001b001165?...` | Verify direct Xiaohongshu import + OCR + retrieval |
| X2 | xhs short | `http://xhslink.com/o/30SfbNeUi0f` | Verify short-link redirect + image-heavy OCR note |
| X3 | xhs short | `http://xhslink.com/o/33nQxAVDUS8` | Verify short-link redirect + interview recap retrieval |

## Imported Xiaohongshu Samples

### X1

- Source: direct Xiaohongshu `discovery/item`
- Final page: `69c1279f000000001b001165`
- Imported card id: `cafb5e7c-ea74-4fef-91a7-8889beecf1e1`
- Images downloaded: `2`
- OCR status: `done`
- OCR chars: `18942`
- Raw markdown: `data/wiki/raw_sources/xiaohongshu_note/69c1279f000000001b001165.md`

### X2

- Source: share text + `xhslink.com/o/30SfbNeUi0f`
- Final page: `6824cf20000000002100af19`
- Imported card id: `dfefd2a5-3c28-400e-b217-976da29db527`
- Images downloaded: `9`
- OCR status: `done`
- OCR chars: `2766033`
- Raw markdown: `data/wiki/raw_sources/xiaohongshu_note/6824cf20000000002100af19.md`

### X3

- Source: share text + `xhslink.com/o/33nQxAVDUS8`
- Final page: `6a180a2800000000070137cd`
- Imported card id: `3bd8a586-3807-4c30-90f1-22916adf1230`
- Images downloaded: `12`
- OCR status: `done`
- OCR chars: `8764`
- Raw markdown: `data/wiki/raw_sources/xiaohongshu_note/6a180a2800000000070137cd.md`

## QA Queries

### Q1: RMSNorm concept

- Query:
  `为什么现在大模型普遍用 RMSNorm，而不是 LayerNorm？`
- Expected:
  Hit X2 first, optionally cite related Transformer material.
- Actual:
  Hit X2, returned a structurally usable answer, but all Chinese display text is mojibake.
- Status:
  `partial`

### Q2: Agent interview recap

- Query:
  `淘天 AI Agent 一面主要会深挖哪些项目细节？`
- Expected:
  Hit X3 and summarize RAG / Agent / engineering drill-down points.
- Actual:
  Hit X3 and returned the right themes: recall ranking, query rewrite, RRF, hallucination handling, agent evaluation, multi-agent state management, MCP, LangGraph, backend load testing.
- Status:
  `pass` on retrieval, `partial` on display quality because of mojibake.

### Q3: Paper theory question

- Query:
  `Attention is All You Need 里为什么要除以 sqrt(dk)？`
- Expected:
  Hit P1 and explain scaling from variance / softmax saturation / gradient stability.
- Actual:
  Retrieved the paper card, but answered that the wiki does not contain the needed explanation.
- Status:
  `fail`

### Q4: Didi interview recap

- Query:
  `总结一下我库里那篇滴滴大模型实习面经里，多Agent系统和外部知识检索相关的问题。`
- Expected:
  Hit X1.
- Actual:
  Missed X1 and answered using X3 + a memory paper instead.
- Status:
  `fail`

## Observed Product Issues

### 1. Xiaohongshu Chinese text is corrupted

Imported titles, summaries, markdown body, and answers show mojibake instead of readable Chinese.

Impact:

- Retrieval still partly works on some keywords
- UI usability is damaged
- Citations are unreadable

### 2. Xiaohongshu import latency is too high

Observed import times for short-link items were about `136-138s`.

Likely cause:

- image download
- Docling OCR on many images
- very large OCR output

Impact:

- short default client timeout will fail
- UI needs progress state and longer timeout budget

### 3. OCR markdown contains giant base64 image payloads

At least one raw markdown file includes a huge `data:image/png;base64,...` block inside the stored OCR text.

Impact:

- chunk index pollution
- larger SQLite / markdown footprint
- weaker retrieval signal-to-noise ratio
- slower prompts

### 4. Paper retrieval is not grounded enough for fine-grained theory QA

Even though `Attention Is All You Need` exists in the wiki, the system failed to answer a standard `sqrt(dk)` question.

Likely causes:

- chunking / retrieval misses the relevant paragraph
- raw markdown summary is stronger than full-body grounding
- no query rewrite for formula-like questions

## Suggested Next Fixes

1. Fix Xiaohongshu encoding end to end before more UI work.
2. Strip base64 image payloads from OCR markdown before indexing.
3. Add import-stage progress reporting and separate:
   - fetch
   - image download
   - OCR
   - chunk indexing
4. Add a retrieval debug endpoint to inspect matched chunks per query.
5. Add one offline regression test that replays Q1-Q4 against the current database.

## Minimal Runbook

Import Xiaohongshu sample:

```powershell
$body = @{
  text_or_url = "淘天 AI Agent 一面复盘 http://xhslink.com/o/33nQxAVDUS8"
  tags = @("面经","Agent","淘天")
} | ConvertTo-Json -Depth 5

Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/wiki/import-xhs" `
  -Method Post `
  -ContentType "application/json; charset=utf-8" `
  -Body $body `
  -TimeoutSec 300
```

Ask a QA query:

```powershell
$body = @{
  message = "淘天 AI Agent 一面主要会深挖哪些项目细节？"
  stream = $false
} | ConvertTo-Json -Depth 4

Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/wiki/chat" `
  -Method Post `
  -ContentType "application/json; charset=utf-8" `
  -Body $body
```

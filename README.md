# LLM Wiki

An LLM-powered personal knowledge system for paper reading, technical notes, and
source-grounded Q&A.

The project is not a default vector RAG demo. The main path is:

```text
PDF / note / image / link
  -> raw Markdown source
  -> structured Wiki cards
  -> SQLite FTS chunk index
  -> Tool-Use Wiki Agent
  -> Agent Trace and evaluation dashboard
```

## Features

- PDF ingestion with Docling remote parsing and a local fallback parser.
- PaperIndex for fast paper sections, blocks, and keyword search.
- Structured Wiki cards: `PaperPage`, `ConceptPage`, `MethodPage`,
  `InterviewQA`, and `SourceNote`.
- Paper knowledge workflow: extraction, distillation, review, and merge.
- Tool-use Q&A loop inspired by Codex / Claude Code:
  `plan -> call tools -> observe -> answer`.
- Tools: `wiki_search`, `wiki_card`, `web_search`, and
  `resource_recommend`.
- Agent Trace showing tool calls, observations, retrieved cards, web results,
  citations, and diagnostics.
- Benchmark and LLM-as-Judge evaluation scripts with a Vue evaluation dashboard.

## Architecture

```text
Frontend (Vue + Naive UI)
  - Capture
  - Wiki Chat
  - Knowledge Vault
  - Paper Library
  - Evaluation Dashboard
        |
        v
FastAPI Backend
        |
        v
Ingestion
  - Docling parser
  - PaperIndex
  - RawSourceVault
        |
        v
Wiki Layer
  - wiki_pages
  - wiki_chunks
  - source_packets
  - distilled_candidates
  - review_reports
  - wiki_card_sources / wiki_card_links / wiki_aliases
        |
        v
Tool-Use Agent
  - tool schema
  - tool-call JSON
  - Python tool executor
  - observations
  - final grounded answer
```

## Quick Start

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment variables

PowerShell:

```powershell
Copy-Item .env.example .env
```

At minimum, configure:

```env
SILICONFLOW_API_KEY=your-key
```

Optional Docling remote parser:

```env
DOCLING_MODE=remote
DOCLING_BASE_URL=http://127.0.0.1:5001
DOCLING_TIMEOUT_SECONDS=360
```

### 3. Start Docling

```powershell
docker compose -f .\docker-compose.docling.yml up -d
```

If you do not want Docling, set:

```env
DOCLING_MODE=off
```

### 4. Start backend

```bash
uvicorn backend.app:app --reload --port 8000
```

Backend:

```text
http://127.0.0.1:8000
```

### 5. Start frontend

```bash
cd frontend
npm install
npm run dev
```

Frontend:

```text
http://localhost:5173
```

## Evaluation

Run the Wiki Agent benchmark against the real API:

```bash
python test/evaluation/scripts/run_agentic_wiki_eval.py \
  --dataset test/evaluation/datasets/wiki_chat/current_papers_30.csv \
  --output-dir test/evaluation/runs/current30_run
```

The evaluation dashboard reads run artifacts from:

```text
test/evaluation/runs/
```

Tracked example runs are kept small and are intended to show metric formats and
failure-case analysis.

## Repository Hygiene

The repository intentionally excludes:

- `.env`
- local SQLite databases
- local logs
- uploaded PDFs
- generated raw sources
- frontend build output
- Python caches

Use `.env.example` and sample evaluation artifacts to reproduce the system
without publishing private data or credentials.

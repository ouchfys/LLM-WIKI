# Agentic Wiki Answer Confidence Evaluation Plan

## Current Revision

The active benchmark has been rebuilt to a compact current-paper set after the
older 50-question benchmark proved misaligned with the current Wiki contents.
The active dataset is now:

```text
data/eval/wiki_chat/current_papers_30.csv
data/eval/wiki_chat/all_agentic_wiki_eval.csv
```

It contains 30 private-Wiki questions: five questions for each of the six
currently ingested PaperPage entries. All rows use `allow_web=false` so the
evaluation measures whether the current Wiki can answer from imported papers
instead of letting Web Search compensate for missing private knowledge.

Historical notes below describe the previous 50+20 plan. The implementation has
been superseded by the 30-question current-paper benchmark.

## Goal

Rebuild the Wiki Chat evaluation system so it measures whether the agent gives trustworthy real answers, not only whether retrieval returns the expected documents.

The existing 50-question benchmark remains the base regression set:

```text
logs/benchmark_50_compare_layered_v2_20260511/benchmark_50_queries_details.csv
```

The new evaluation must add coverage for the two newer papers:

- `The LLM Already Knows: Estimating LLM-Perceived Question Difficulty via Hidden Representations`
- `Probing the Difficulty Perception Mechanism of Large Language Models`

The primary score is answer confidence/faithfulness. Retrieval hit rate is still tracked, but it is a secondary diagnostic metric.

## Why This Change

The old benchmark is useful for retrieval regression, but it is not enough for the current Agentic Wiki design.

The current system has four tools:

- `wiki_search`: find candidate private Wiki cards.
- `wiki_card`: open and use matched cards as structured memory/skills.
- `web_search`: temporary external evidence for freshness or missing coverage.
- `resource_recommend`: follow-up learning resource discovery.

Because the answer agent can use tools and structured Wiki cards, the evaluation should judge the final answer quality:

- Did it answer the actual question?
- Was the answer supported by retrieved Wiki cards?
- Did it avoid unsupported claims?
- Did it use Web Search only when appropriate?
- Did it cite the right evidence?

## Target Dataset Layout

Create a stable evaluation dataset directory:

```text
data/eval/wiki_chat/
  base_50.csv
  new_difficulty_papers_20.csv
  all_agentic_wiki_eval.csv
  expected_cards.json
```

### Base 50 Set

Copy or normalize the existing CSV:

```text
logs/benchmark_50_compare_layered_v2_20260511/benchmark_50_queries_details.csv
```

into:

```text
data/eval/wiki_chat/base_50.csv
```

Required normalized fields:

```text
id
source
query
query_type
difficulty
expected_keywords
expected_source
expected_card_titles
allow_web
notes
```

For the original 50 questions:

- `allow_web=false` by default.
- `expected_card_titles` can be inferred from `source`, `retrieval_hits`, `top1_source`, and existing answer fields.
- Keep the original file unchanged under `logs/` as historical evidence.

### New Difficulty Papers Set

Create:

```text
data/eval/wiki_chat/new_difficulty_papers_20.csv
```

Add 20 questions covering the two new papers.

Recommended split:

1. 6 single-paper questions for `The LLM Already Knows`
2. 6 single-paper questions for `Probing the Difficulty Perception Mechanism`
3. 4 comparison questions across both papers
4. 2 interview-style explanation questions
5. 2 private-Wiki routing questions

Example questions:

```text
What is the core hypothesis of The LLM Already Knows?
How does the paper estimate LLM-perceived question difficulty from hidden representations?
What evidence supports the claim that LLMs already encode perceived difficulty?
What is the main problem studied by Probing the Difficulty Perception Mechanism of Large Language Models?
How does the probing paper analyze the mechanism behind difficulty perception?
Compare the two papers: what does each one contribute to understanding question difficulty?
If an interviewer asks why hidden states can reveal perceived difficulty, how should I answer?
In my Wiki, what content is related to difficulty perception?
```

Each row must include:

```text
id
source
query
query_type
difficulty
expected_keywords
expected_source
expected_card_titles
allow_web
notes
```

For these questions:

- `allow_web=false` for private-Wiki and paper-content questions.
- `allow_web=true` only if the query explicitly asks for latest external context.

### Combined Set

Generate:

```text
data/eval/wiki_chat/all_agentic_wiki_eval.csv
```

It should concatenate:

- `base_50.csv`
- `new_difficulty_papers_20.csv`

Expected total: 70 questions.

## Evaluation Runner

Create a runner script:

```text
scripts/evaluation/run_agentic_wiki_eval.py
```

The runner should call the real backend service, not bypass the API.

Default endpoint:

```text
POST http://127.0.0.1:8000/api/wiki/chat
```

Payload:

```json
{
  "message": "<query>",
  "session_id": "eval-agentic-wiki",
  "stream": false
}
```

The runner should save per-query details:

```text
logs/agentic_wiki_eval_<timestamp>/
  queries_details.csv
  queries_details.jsonl
  summary.json
  failed_cases.md
```

Per-query captured fields:

```text
id
query
query_type
difficulty
expected_source
expected_card_titles
allow_web
answer
citations
resources
tool_plan
latency_seconds
retrieval_hit
top1_hit
web_allowed
web_used
tool_routing_correct
answer_confidence
answer_completeness
citation_grounding
unsupported_claim_risk
final_score
reviewer_reason
```

## Reviewer

Create a deterministic reviewer module:

```text
scripts/evaluation/agentic_wiki_answer_reviewer.py
```

The reviewer can use the same SiliconFlow Qwen model already configured for review, but with a separate prompt and strict JSON output.

Recommended model:

```text
Qwen/Qwen3.6-27B
```

The reviewer receives:

- User query
- Expected source/card hints
- Agent answer
- Returned citations
- Tool plan
- Whether Web was allowed
- Wiki card snippets or citation summaries, if available

It returns strict JSON:

```json
{
  "answer_confidence": 0.0,
  "answer_completeness": 0.0,
  "citation_grounding": 0.0,
  "tool_routing_correctness": 0.0,
  "unsupported_claim_risk": 0.0,
  "final_score": 0.0,
  "passed": false,
  "reason": "short explanation"
}
```

Scoring meaning:

- `answer_confidence`: the answer is likely correct and faithful to available evidence.
- `answer_completeness`: the answer covers the important parts of the query.
- `citation_grounding`: cited Wiki/Web evidence supports the answer.
- `tool_routing_correctness`: the agent used the right tools and avoided unnecessary Web Search.
- `unsupported_claim_risk`: high score means higher hallucination or unsupported external claims.

## Metric Weights

Use this weighted score:

```text
final_score =
  0.45 * answer_confidence
  0.25 * answer_completeness
  0.15 * citation_grounding
  0.10 * retrieval_hit_rate
  0.05 * tool_routing_correctness
  - 0.10 * unsupported_claim_risk
```

Clamp final score to `[0, 1]`.

Primary pass condition:

```text
final_score >= 0.75
answer_confidence >= 0.75
citation_grounding >= 0.65
```

Strict private-Wiki condition:

If `allow_web=false`, then:

```text
web_used must be false
```

unless the query had no Wiki hits and the answer explicitly says Wiki has no strong evidence before using external information. For the first benchmark version, prefer strict mode: no Web when `allow_web=false`.

## Retrieval Diagnostics

Keep retrieval metrics, but treat them as diagnostics:

```text
retrieval_hit_rate
top1_hit_rate
top3_hit_rate
mean_citations_per_answer
no_citation_rate
wrong_web_usage_rate
```

Important: a retrieval hit does not mean the answer is good. It only means the agent found a plausible source.

## Tool Routing Checks

Expected behavior:

- Private Wiki queries should use `wiki_search` and `wiki_card`.
- Private Wiki queries should not use `web_search`.
- Latest/current/mainstream/external-source queries may use `web_search`.
- Resource recommendation queries may use `resource_recommend`.
- The agent should not call `resource_recommend` for ordinary factual answers.

Examples:

```text
"In my Wiki, what content is related to GRPO?"
expected: wiki_search + wiki_card, no web_search

"What are the current mainstream RAG evaluation methods?"
expected: wiki_search + wiki_card + web_search

"Recommend papers and GitHub resources for RMSNorm"
expected: wiki_search + wiki_card + web_search + resource_recommend
```

## Report Format

Generate:

```text
logs/agentic_wiki_eval_<timestamp>/summary.md
```

It should include:

```text
Overall final_score
Overall answer_confidence
Overall citation_grounding
Pass rate
Base 50 score
New difficulty papers score
Wrong web usage rate
No citation rate
Top 10 failed cases
Recommended fixes
```

Failure case format:

```text
## <id> <query>

Expected:
- source:
- cards:
- allow_web:

Actual:
- tool_plan:
- citations:
- answer excerpt:

Reviewer:
- final_score:
- answer_confidence:
- citation_grounding:
- reason:

Likely fix:
- retrieval alias
- card quality
- prompt/tool routing
- answer synthesis
```

## Implementation Steps For Goal Mode

### Step 1: Normalize Existing 50-Question Benchmark

Input:

```text
logs/benchmark_50_compare_layered_v2_20260511/benchmark_50_queries_details.csv
```

Output:

```text
data/eval/wiki_chat/base_50.csv
```

Acceptance:

- Exactly 50 rows.
- Required columns exist.
- Existing benchmark file remains untouched.

### Step 2: Add New 20-Question Difficulty-Papers Benchmark

Output:

```text
data/eval/wiki_chat/new_difficulty_papers_20.csv
```

Acceptance:

- Exactly 20 rows.
- Covers both new papers.
- Includes comparison and interview-style questions.
- `allow_web=false` for paper-content/private-Wiki questions.

### Step 3: Build Combined 70-Question Dataset

Output:

```text
data/eval/wiki_chat/all_agentic_wiki_eval.csv
```

Acceptance:

- Exactly 70 rows.
- No duplicate IDs.
- Columns are consistent.

### Step 4: Implement API-Based Evaluation Runner

Output:

```text
scripts/evaluation/run_agentic_wiki_eval.py
```

Acceptance:

- Calls `/api/wiki/chat`.
- Captures answer, citations, resources, tool_plan, latency.
- Writes CSV, JSONL, summary JSON.
- Can run a subset by `--limit` and `--ids`.

### Step 5: Implement Answer Reviewer

Output:

```text
scripts/evaluation/agentic_wiki_answer_reviewer.py
```

Acceptance:

- Produces strict JSON.
- Retries or falls back safely when reviewer output is invalid.
- Penalizes unsupported claims and wrong Web usage.

### Step 6: Add Summary Report Generator

Output:

```text
logs/agentic_wiki_eval_<timestamp>/summary.md
```

Acceptance:

- Shows overall and split scores.
- Lists worst cases.
- Gives likely fix categories.

### Step 7: Run Smoke Evaluation

Command target:

```text
python scripts/evaluation/run_agentic_wiki_eval.py --dataset data/eval/wiki_chat/all_agentic_wiki_eval.csv --limit 5
```

Acceptance:

- Produces valid output files.
- At least one private-Wiki query shows no Web Search.
- At least one latest/external query shows Web Search.

### Step 8: Run Full Evaluation

Command target:

```text
python scripts/evaluation/run_agentic_wiki_eval.py --dataset data/eval/wiki_chat/all_agentic_wiki_eval.csv
```

Acceptance:

- Produces a full 70-question report.
- No uncaught exceptions.
- Summary includes base 50 and new papers split.

## Target Quality Bar

Initial acceptable target:

```text
overall_final_score >= 0.72
overall_answer_confidence >= 0.75
new_difficulty_papers_final_score >= 0.70
wrong_web_usage_rate <= 0.10
no_citation_rate <= 0.15
```

Strong target:

```text
overall_final_score >= 0.82
overall_answer_confidence >= 0.85
new_difficulty_papers_final_score >= 0.80
wrong_web_usage_rate <= 0.05
no_citation_rate <= 0.08
```

## Expected Follow-Up Fix Types

After the first full run, classify failures into:

```text
retrieval_alias_missing
retrieval_rank_wrong
wiki_card_content_too_weak
tool_routing_wrong
answer_synthesis_unfaithful
citation_grounding_weak
reviewer_uncertain
```

This classification decides the next iteration:

- Alias failures: add aliases or improve card indexing.
- Rank failures: improve rerank/query rewriting.
- Card content failures: improve distillation/merge output.
- Routing failures: adjust tool router prompt/fallback.
- Answer failures: improve answer prompt and citation discipline.
- Reviewer failures: refine reviewer rubric.

## Do Not Do

- Do not judge success only by retrieval hit rate.
- Do not let Web Search compensate for private-Wiki misses in private-Wiki questions.
- Do not silently merge web results into Wiki during evaluation.
- Do not overwrite the historical benchmark logs.
- Do not delete failure cases after a run; they are the main debugging artifact.

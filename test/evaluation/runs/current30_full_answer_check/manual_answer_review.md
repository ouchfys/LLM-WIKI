# Manual Answer Review - Current Papers 30

Run directory:

```text
test/evaluation/runs/current30_full_answer_check
```

## Overall Judgment

The 30-question run is broadly usable. Most answers retrieve the right paper card, cite Wiki cards, and give a reasonably complete Chinese answer.

The main issue is not answer absence. The main issue is tool routing: 8 of 30 questions used `web_search` even though every benchmark row has `allow_web=false`. The answers still usually cite Wiki cards, but this should be fixed because the benchmark is intended to test private-Wiki knowledge only.

Summary:

```text
overall_final_score: 0.8287
answer_confidence: 0.8632
citation_grounding: 1.0
retrieval_hit_rate: 1.0
top1_hit_rate: 0.7667
wrong_web_usage_rate: 0.2667
```

## Good Answer Groups

### Attention Is All You Need

Status: good.

Questions `paper_001` to `paper_005` all look acceptable. They cover Transformer architecture, scaled dot-product attention, multi-head attention, positional encoding, and interview-style encoder-decoder explanation.

Scores:

```text
paper_001 0.975
paper_002 0.8543
paper_003 0.8183
paper_004 0.8183
paper_005 0.9327
```

### DeepSeek-R1 / GRPO

Status: mostly good.

The answers are generally correct and cover RL-driven reasoning, GRPO vs PPO, emergent reasoning behavior, and interview framing. However, `paper_006`, `paper_008`, and `paper_010` incorrectly enabled Web Search despite `allow_web=false`.

Scores:

```text
paper_006 0.7743 web_used=true
paper_007 0.9327
paper_008 0.8700 web_used=true
paper_009 0.7760
paper_010 0.7743 web_used=true
```

### RL Reasoning Capacity Paper

Status: good with one watch item.

The answers correctly explain the paper's question, conclusion, experiments, and interview framing. `paper_013` is a comparison question and is slightly weaker, but the answer direction is still basically right: RL improves exploitation of existing capabilities, while distillation can transfer/expand accessible reasoning behavior.

Scores:

```text
paper_011 0.975
paper_012 0.8543
paper_013 0.7383 web_used=true
paper_014 0.8183
paper_015 0.8543
```

### Probing Difficulty Perception

Status: good.

The answers cover the research question, linear probing, attention heads, evidence, and interview explanation. `paper_024` is an evidence-chain question and is slightly weaker, but the content is still aligned with the paper.

Scores:

```text
paper_021 0.9327
paper_022 0.8967
paper_023 0.8543
paper_024 0.7383 web_used=true
paper_025 0.9327
```

### The LLM Already Knows

Status: mostly good.

The answers cover the core hypothesis, hidden-representation difficulty estimation, evidence, and comparison with the probing paper. `paper_028` is the weakest answer in the run.

Scores:

```text
paper_026 0.7760
paper_027 0.975
paper_028 0.6600 web_used=true
paper_029 0.7760
paper_030 0.8347
```

## Weak Answer Groups

### Lawyer Recommendation System

Status: acceptable but less complete than the paper-focused LLM questions.

The role/function answer is mostly right, and the collaborative-filtering answer is usable. The weakest issue is `paper_018`: the answer says the Wiki does not clearly contain the frontend/backend stack and then gives generic mini-program stack guesses. If the original paper contains exact technologies, the Wiki card extraction is not detailed enough, or the retrieval prompt is not opening the right section.

Scores:

```text
paper_016 0.6960 web_used=true
paper_017 0.7760
paper_018 0.6960 web_used=true
paper_019 0.7760
paper_020 0.7760
```

## Needs Fix

### 1. Stop Web Search for this benchmark

All 30 rows have:

```text
allow_web=false
```

But these rows used Web:

```text
paper_006
paper_008
paper_010
paper_013
paper_016
paper_018
paper_024
paper_028
```

This does not always make the answer wrong, but it violates the benchmark contract. For private-paper benchmarks, the router should not auto-fallback to Web just because a query sounds broad or interview-like.

### 2. Improve section coverage for lawyer-system implementation details

`paper_018` asks:

```text
What are the main frontend and backend technologies used by the lawyer service mini-program?
```

The answer is not fully satisfactory. It admits the Wiki lacks exact tech-stack details and then gives generic guesses. This likely needs better paper card extraction or a richer `content_json` section for implementation details.

### 3. Tighten answer for no-token difficulty estimation

`paper_028` asks:

```text
Why is estimating difficulty without generating output tokens useful?
```

The answer is directionally right: lower compute cost, faster routing, pre-generation decision making. But it also adds some generic extrapolations like beam search/resource allocation/curriculum learning. The answer should be more tightly grounded in the paper's claims: no generation, hidden-state-only estimation, cheaper difficulty scoring, and use before solving.

## Practical Conclusion

The 30-question current-paper benchmark is suitable as the main test set.

The output answers are mostly correct and reasonably comprehensive for the LLM paper questions. The lawyer-system paper needs richer implementation-detail extraction, and the router must be changed so this private-paper benchmark does not call Web Search.


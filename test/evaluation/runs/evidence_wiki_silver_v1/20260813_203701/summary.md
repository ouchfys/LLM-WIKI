# Evidence Wiki Silver Benchmark

- Run: `20260813_203701`
- Dataset: `evidence-wiki-silver-v1`
- Benchmark type: **llm-generated silver benchmark** (0 human annotations)
- Verifier model: `Qwen/Qwen3.6-27B`
- Table QA model: `deepseek-ai/DeepSeek-V3`
- Runtime: 723.25 seconds

## Verifier

- Cases: 140
- Gate accuracy: 92.86%
- False accept rate: 8.57%
- False reject rate: 2.86%
- Semantic exact accuracy (cases reaching LLM): 68.84%

## Table QA

- Cases: 100
- End-to-end pass rate: 76.00%
- Resolver recall: 94.00%
- Result-row recall: 77.00%
- Cell-citation recall: 80.00%
- Answer correctness: 79.00%
- SQL success: 89.00%

## Honest boundary

These are silver-label results without human adjudication. They must not be presented as human-gold accuracy.

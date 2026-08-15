# Evaluation

当前评测分成两条互不混淆的回归线。

## Wiki Chat 30 题

- 数据集：`datasets/wiki_chat/current_papers_30.csv`
- 期望页面：`datasets/wiki_chat/expected_cards.json`
- 运行脚本：`scripts/run_agentic_wiki_eval.py`
- 答案复核：`scripts/agentic_wiki_answer_reviewer.py`
- 当前保留基线：`runs/current30_full_answer_check/`

```powershell
python test/evaluation/scripts/run_agentic_wiki_eval.py \
  --dataset test/evaluation/datasets/wiki_chat/current_papers_30.csv \
  --output-dir test/evaluation/runs/current30_run
```

## Evidence Verifier / Table QA 240 条 Silver Benchmark

- 数据集：`datasets/evidence_wiki_silver_v1/`
- 构建：`scripts/build_evidence_wiki_silver_benchmark.py`
- 运行：`scripts/run_evidence_wiki_silver_benchmark.py`
- LLM 裁决：`scripts/adjudicate_verifier_silver_benchmark.py`
- 当前结果：`runs/evidence_wiki_silver_v1/`

Silver 数据由真实持久化 evidence/table cells 自动派生，用于零人工回归；它不是人工 gold benchmark。

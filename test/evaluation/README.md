# Evaluation

旧版固定语料、测试场景、运行结果和隔离环境已清理，不再提供可直接复跑的历史基线。

保留以下通用脚本，运行前必须使用 `--dataset` 显式指定新数据集：

- `scripts/run_agentic_wiki_eval.py`：针对正在运行的服务评测论文问答，输入为 CSV。
- `scripts/agentic_wiki_answer_reviewer.py`：供问答评测调用的答案复核组件。
- `scripts/run_evidence_wiki_silver_benchmark.py`：评测证据核验，数据目录需含 `manifest.json` 和 `verifier.jsonl`，证据编号必须对应 `--db` 指定的测试库。
- `scripts/adjudicate_verifier_silver_benchmark.py`：对证据核验数据作模型裁决；模型标签不能视为人工 Gold。

真实评测会调用模型 API，按需手动运行。当前没有新版端到端成绩。

代码回归测试仍位于 `test/agent_runtime/`、`test/wiki/`、`test/paper_pipeline/` 等目录。清空论文语料时保留聊天的回归用例已归入 `test/wiki/test_reset_paper_corpus_results.py`。

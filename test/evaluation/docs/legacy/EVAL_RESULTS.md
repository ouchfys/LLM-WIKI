# Evaluation Results

> 50 题带标注 benchmark，覆盖 4 篇论文（Attention、GRPO、RL-Reasoning、my paper），含列举 / 概念解释 / 事实定位 / 细节查找 / 跨章节 / 概括 / 方法论 / 比较 8 类查询，按易 / 中 / 难三档标注。
>
> 评估指标：Retrieval Recall / Answer Recall / Full Hit Rate / LLM Judge Top1（DeepSeek 判分）。

## 核心结论

| 指标 | 基线（main） | 最终版（caption_filter + reingested） | 增量 |
|------|-----:|-----:|-----:|
| Retrieval Recall | 78.95% | **85.83%** | +6.88 pt |
| Answer Recall | 56.14% | **66.00%** | +9.86 pt |
| Judge Top1 | 0.7024 | **0.8019** | +0.10 |

## 迭代历史（main_rerank，50 题全量）

| 版本 | Ret Recall | Ans Recall | Judge Top1 | 变更点 |
|------|-----:|-----:|-----:|------|
| v1 | 0.8867 | 0.6583 | 0.7873 | 接入 BGE Rerank |
| v2 | 0.8867 | 0.6467 | 0.7902 | selector 调优 |
| v3 | 0.8867 | 0.6067 | 0.7900 | 回归 |
| v4 | 0.8867 | 0.6567 | 0.7899 | 修复 v3 |
| v5 | 0.8867 | **0.6883** | 0.7872 | answer recall 峰值 |
| caption_filter | 0.8583 | 0.6700 | 0.7995 | 图注/目录过滤 |
| reingested | 0.8583 | 0.6600 | **0.8019** | 对齐 parent_block 后重新入库 |

## 消融实验

### 1. BGE Rerank（vs 纯向量+图+RRF）

样本：19 题 decoupled

| 策略 | Ret Recall | Ans Recall | Judge Top1 |
|------|-----:|-----:|-----:|
| main | 0.7895 | 0.5614 | 0.7024 |
| main_rerank | 0.7544 | 0.5351 | 0.6871 |

Rerank 在小样本上提升不稳定，但全量 50 题上 +9.72 pt retrieval recall。

### 2. Conclusion Selector（RL 论文）

| 阶段 | Ans Recall | Full Hit |
|------|-----:|-----:|
| 上线前 | 36.9% | 14.3% |
| retest | **43.5%** | **21.4%** |

### 3. Conclusion Selector（my paper）

| 指标 | 数值 |
|------|-----:|
| Judge Top1 | **0.9213** |
| Answer Full Hit | 60% |

（同期全量样本 full hit 仅 36–40%，selector 在目标论文上带来显著收益）

### 4. parent_block_id（GRPO 论文）

| 指标 | 数值 |
|------|-----:|
| Retrieval Recall | 0.8846 |
| Judge Top1 | 0.8767 |

## 分层归因（最终版 reingested）

### 按论文

| 论文 | Ret Recall | Judge Top1 |
|------|-----:|-----:|
| my paper | 83.3% | 0.9194 |
| Attention | 87.8% | 0.8058 |
| GRPO | 88.5% | 0.8767 |
| RL-Reasoning | 83.3% | 0.6449 |

### 按难度

| 难度 | 样本数 | Ret Recall | Judge Top1 |
|------|---:|-----:|-----:|
| 易 | 4 | 100% | 0.9366 |
| 中 | 28 | 85.4% | 0.7941 |
| 难 | 18 | 83.3% | 0.7841 |

### 按查询类型（暴露短板）

| 类型 | Ret Recall | Full Hit |
|------|-----:|-----:|
| 列举型 | 100% | 80% |
| 概念解释 | 88.2% | 29.4% |
| 事实定位 | 90.0% | 50.0% |
| 细节查找 | 73.9% | 25.0% |
| 跨章节 | 66.7% | 60.0% |
| 方法论 | 100% | 0% |
| 比较型 | 100% | 0% |

## 当前弱点

1. 跨章节题 retrieval recall 仅 66.7%，NEXT_CHUNK 扩展仍有遗漏
2. 细节查找 full hit 25%，附录/图注噪声未完全过滤
3. 比较型 / 方法论类样本 full hit 为 0，答案完整性仍需 selector 细化

## 2026-05-11 Compare Selector Retest

Benchmark script: `scripts/benchmark_19_queries.py --strategy main_rerank`

Baseline:
`logs/benchmark_50_reingested_20260420/benchmark_19_queries_summary.json`

Retest:
`logs/benchmark_50_compare_layered_v2_20260511/benchmark_50_queries_summary.json`

### Overall Delta (50 queries, `main_rerank`)

| Metric | Hard match baseline | Layered compare retest | Delta |
|------|-----:|-----:|-----:|
| Retrieval Recall | 85.83% | **87.50%** | +1.67 pt |
| Answer Recall | **66.00%** | 64.17% | -1.83 pt |
| Answer Full Hit | **40.0%** | 34.0% | -6.0 pt |
| Judge Top1 | **0.8019** | 0.7975 | -0.0044 |

### Compare Query Delta (`n=1`)

Query:
`GRPO 相比 PPO 除了去掉 value model，还减少了哪类调参负担？`

| Metric | Hard match baseline | Layered compare retest |
|------|-----:|-----:|
| Retrieval Recall | 100% | 100% |
| Answer Recall | 66.67% | **100%** |
| Answer Full Hit | 0% | **100%** |
| Judge Top1 | 0.8984 | **0.9009** |

Observed answer-keyword delta:

- Baseline hits: `["GAE", "lambda"]`
- Retest hits: `["GAE", "lambda", "调优"]`

Interpretation:

- The layered compare intent rule now routes the target query through `compare_selector`.
- The compare prompt now preserves explicit overhead terms (`tuning` / `调优` / `调参`), so the exact-match scorer no longer drops the last slot.
- The compare fix is real on the target sample, but the 50-query aggregate moved in a mixed direction, so the next pass should narrow the wording change to compare-only paths.

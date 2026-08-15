# Evidence Wiki Silver Benchmark（2026-08-13）

## 结论

项目已经建立并真实运行一套 240 条、零人工参与的 Wiki evidence silver benchmark：

- Semantic Verifier：140 条；
- Table QA：100 条；
- 人工标注：0 条；
- 数据来源：当前 26 篇论文的持久化 Docling element 与 table cell；
- 数据集：`test/evaluation/datasets/evidence_wiki_silver_v1/`。

它是 `LLM-generated and independently adjudicated silver benchmark`，不是人工 golden benchmark，也不是公开通用 NLI/Table QA 榜单。

## 构建方法

### Semantic Verifier

从 26 个 source packets 中选择 35 个不同的真实 Docling evidence spans，每个 span 生成四种 claim，共 140 条：

1. `entailed`：证据直接蕴含；
2. `contradicted_relation`：反转关系、比较、存在性或结论；
3. `insufficient_scope`：加入证据未覆盖的全称范围或保证；
4. `insufficient_attribution`：加入证据未建立的归因、机制或因果关系。

`DeepSeek-V3` 负责第一轮生成和预标注，`DeepSeek-V4-Flash` 在不读取第一轮理由的情况下，仅根据原始证据独立裁决。二次裁决修改了 23/140 条标签。最终标签分布为：

| 标签 | 数量 |
|---|---:|
| entailed | 34 |
| contradicted | 55 |
| insufficient | 51 |

每条样本保存 claim、完整 evidence excerpt、element ID、source packet、论文标题、页码、两个模型的理由和是否一致。

### Table QA

100 条期望答案不由 LLM 标注，而是直接从持久化 Docling table cells 确定：

| 类型 | 数量 |
|---|---:|
| 精确单元格查询 | 60 |
| 单表最大值 | 20 |
| 单表最小值 | 10 |
| 跨论文双单元格比较 | 10 |

每条样本冻结 source packet ID、table ID、cell ID、row/column label、原始值和页码。评测不只检查最终答案字符串，还检查找表、DuckDB 结果行和 cell citation。

## 最终结果

### Semantic Verifier

被测模型：`Qwen/Qwen3.6-27B`
最终运行：`test/evaluation/runs/evidence_wiki_silver_v1/20260813_205204/`

| 指标 | 结果 |
|---|---:|
| Gate accuracy（accept/reject） | 93.57% |
| False accept rate | 8.49% |
| False reject rate | 0.00% |
| 三分类 exact accuracy（进入语义模型的 138 条） | 78.26% |
| 未处理错误 | 0 |
| 平均延迟 | 4.831 秒/条 |

主要问题是 9 条 `insufficient` 被错误放行，集中在隐含因果、归因和范围扩大。当前确定性防线对错误数字、缺失 evidence ID 很有效，但对“证据有相关描述，却没有真正建立因果关系”的 hard negative 仍依赖 LLM judge。

三分类准确率低于二分类门禁准确率是合理的：系统提交 Wiki 时主要关心能否安全接受；`contradicted` 与 `insufficient` 都应拒绝，但二者的细分类仍可能不同。

### Table QA

被测模型：`DeepSeek-V3`，结构化执行：只读 DuckDB
完整运行：`test/evaluation/runs/evidence_wiki_silver_v1/20260813_203701/`

| 指标 | 结果 |
|---|---:|
| 端到端通过率 | 76.00% |
| Table Resolver recall | 94.00% |
| DuckDB/result-row recall | 77.00% |
| Cell citation recall | 80.00% |
| Answer correctness | 79.00% |
| SQL success rate | 89.00% |
| Fallback rate | 24.00% |
| 未处理错误 | 0 |
| 平均延迟 | 14.687 秒/条 |

| 类型 | 端到端通过率 |
|---|---:|
| 精确单元格 | 88.33% |
| 单表最大值 | 75.00% |
| 单表最小值 | 70.00% |
| 跨论文比较 | 10.00% |

结果说明当前系统已经能可靠处理大部分精确单元格问题，但跨论文比较仍是明确短板。失败主要发生在 SQL planner 没有同时保留两个目标 cell、复杂/缺失 caption 导致找错表，以及 fallback cell ranking 无法完成聚合或比较。

评测以 3 workers 运行，期间 SiliconFlow 返回过 429。客户端完成了重试，未产生未处理异常；但部分规划失败会进入系统设计中的 table-resolver fallback。因此这里的 76%衡量的是当前部署链路在三并发条件下的端到端表现，而不是排除供应商限流后的模型能力上界。

## 可复现命令

```powershell
python test/evaluation/scripts/build_evidence_wiki_silver_benchmark.py --force --teacher-workers 3
python test/evaluation/scripts/adjudicate_verifier_silver_benchmark.py --workers 3
python test/evaluation/scripts/run_evidence_wiki_silver_benchmark.py --workers 3
```

数据集 manifest 保存文件哈希、教师模型、裁决模型、标签来源和零人工标注声明。评测运行目录保存逐条输入、预测、SQL、结果行、citation、延迟、失败样本与汇总指标。

## 诚实边界

- 不能写成“人工 benchmark 93.57%”，应写成“独立强模型裁决的 140 条 silver set 上，Verifier 门禁准确率 93.57%”。
- 二次模型裁决降低了单教师偏差，但没有消除模型共同偏差和标签歧义。
- Table QA 标签来自真实单元格，答案 provenance 比语义标签更客观；问题分布仍只覆盖当前 26 篇论文。
- 该 benchmark 适合项目回归、失败分析和面试展示，不替代公开数据集或人工专家评审。

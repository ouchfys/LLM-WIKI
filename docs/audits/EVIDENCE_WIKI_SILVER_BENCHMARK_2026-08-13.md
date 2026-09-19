# Evidence Verifier Silver Benchmark（2026-08-13）

## 结论

项目建立并真实运行了一套 140 条、零人工参与的 Semantic Verifier silver benchmark。数据来自冻结论文语料中的真实 evidence spans，用于验证“结论—原文证据”门禁，不是人工 golden benchmark，也不是公开通用 NLI 榜单。

## 构建方法

从 26 个 source packets 中选择 35 个不同的真实 evidence spans，每个 span 生成四种 claim，共 140 条：

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

## 最终结果

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

## 可复现命令

```powershell
python test/evaluation/scripts/adjudicate_verifier_silver_benchmark.py --workers 3
python test/evaluation/scripts/run_evidence_wiki_silver_benchmark.py --workers 3
```

数据集 manifest 保存文件哈希、教师模型、裁决模型、标签来源和零人工标注声明。评测运行目录保存逐条输入、预测、延迟、失败样本与汇总指标。

## 诚实边界

- 不能写成“人工 benchmark 93.57%”，应写成“独立强模型裁决的 140 条 silver set 上，Verifier 门禁准确率 93.57%”。
- 二次模型裁决降低了单教师偏差，但没有消除模型共同偏差和标签歧义。
- 该 benchmark 适合项目回归、失败分析和面试展示，不替代公开数据集或人工专家评审。

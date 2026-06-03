# 论文 GraphRAG 项目复盘与面试讲稿

更新时间：2026-04-20

## 1. 一句话介绍

我做的是一个面向论文问答的 GraphRAG 系统：把 PDF 论文结构化入库到 Neo4j，把向量检索、实体驱动的图检索、RRF 融合、Reranker、答案选择器和 LLM 生成串起来，重点解决“中文 query 问英文论文”“公式和图表多”“答案分散在不同章节”“自动评测不可靠”这几类实际问题。

这不是一个“炫技型 Graph 推理系统”，而是一个更务实的、围绕论文 QA 场景做过多轮迭代的检索增强系统。

---

## 2. 面试时最推荐的定位说法

不要把这个项目说成：

- 复杂的图推理系统
- 通用论文智能体
- 完全解决了论文问答

更准确的说法是：

> 这是一个面向学术论文问答的 Entity-driven Graph-augmented Retrieval 系统。核心目标不是做复杂图推理，而是利用实体和章节结构，把检索结果变得更完整、更少噪声，然后让 LLM 更稳定地生成答案。

这句话非常重要，因为它决定了你后面所有回答是否可信。

---

## 3. 当前系统到底长什么样

### 3.1 入库流程

```text
PDF
 -> PyMuPDF4LLM / Markdown 提取
 -> heading 识别
 -> semantic block 合并
 -> chunk 切分
 -> LLM 抽取实体与关系
 -> Neo4j 存储 Chunk / Entity / RELATES_TO / HAS_ENTITY / NEXT_CHUNK
 -> bge-m3 生成 chunk embedding
 -> Neo4jVector 建索引
```

### 3.2 检索流程

```text
Query
 -> 中文/英文识别
 -> 若 query 为中文且论文为英文，则做检索翻译
 -> QueryEntityExtractor 抽取 query 实体
 -> 向量检索
 -> 章节内二次检索
 -> 显式实体图检索
 -> RRF 融合
 -> 结构去噪
 -> 可选 reranker
 -> answer-doc selector
 -> LLM 生成答案
```

### 3.3 图谱结构

```text
(Chunk)-[:HAS_ENTITY]->(Entity)
(Entity)-[:RELATES_TO]->(Entity)
(Chunk)-[:NEXT_CHUNK]->(Chunk)
```

### 3.3.1 面试时怎么讲这张数据库结构图

我会配合这张图来讲数据库设计：[database schema.png](/E:/Agent-learn/agent项目/agent项目/pictures/database%20schema.png)

这张图最适合讲的不是“Neo4j 里有什么节点”，而是“我为什么把论文库设计成这种结构”：

- 粉色节点可以理解为 `Chunk`，是最终可回溯的证据单元；实际库里会带 `chunk_id`、`text`、`source`、`page`、`chapter`、`chunk_index`、`parent_block_id`、`embedding` 等字段。
- 棕色节点是 `Entity`，是从 chunk 中抽出来的概念锚点；实际节点上会存 `name`、`type`、`description`。
- `HAS_ENTITY` 表示“这个 chunk 提到了这个概念”，它把原文证据和知识锚点连接起来。
- `RELATES_TO` 表示实体之间在论文里的语义联系，比如方法组成、模块关系、上下位概念或对比关系。
- `NEXT_CHUNK` 保留原始文档顺序，这条边很重要，因为很多论文答案不是单个 chunk，而是连续两段甚至三段一起才能答完整。

如果面试官追问“这和纯向量 RAG 有什么区别”，我会这样解释：

> 纯向量检索更像是在找语义相似片段，而我这个库额外保留了两种结构信息。第一种是 chunk 到实体的显式锚定，方便围绕概念做补召回；第二种是 chunk 之间的连续关系，方便把被切开的上下文重新补齐。所以这个图谱不是为了做复杂图推理，而是为了让检索结果更完整、更可解释。

如果要结合图里的例子讲，可以这样说：

- 某个 chunk 提到了 `Multi-Head Attention`，那它会通过 `HAS_ENTITY` 连到对应实体。
- 这个实体再通过 `RELATES_TO` 连到 `parallel attention`、`single-head`、`concatenate` 这类相关概念。
- 如果答案刚好被切在相邻 chunk，中间再通过 `NEXT_CHUNK` 把后续证据补回来。

这套设计的核心不是“把文档全部图化”，而是只把对 QA 真有帮助的结构保留下来。

### 3.4 当前最重要的系统模块

- [system/rag_pipeline.py](/E:/Agent-learn/agent项目/agent项目/system/rag_pipeline.py)
- [system/graphrag_retrieval.py](/E:/Agent-learn/agent项目/agent项目/system/graphrag_retrieval.py)
- [system/file_extraction.py](/E:/Agent-learn/agent项目/agent项目/system/file_extraction.py)
- [system/data_import_graphrag.py](/E:/Agent-learn/agent项目/agent项目/system/data_import_graphrag.py)
- [system/prompt_templates.py](/E:/Agent-learn/agent项目/agent项目/system/prompt_templates.py)
- [scripts/benchmark_queries.py](/E:/Agent-learn/agent项目/agent项目/scripts/benchmark_queries.py)

---

## 4. 我们是怎么一步步迭代到现在的

这一部分是面试里最有价值的内容，因为它体现的是工程判断，而不是“背架构图”。

### V0：初始版混合检索

最早的主链路是：

- 向量检索
- LLM 抽 query 实体
- 图检索召回相关 chunk
- RRF 融合
- 可选 reranker
- LLM 生成答案

这个版本能跑通，但很快暴露出几个核心问题：

- 中文 query 问英文论文时，向量检索经常偏
- 图里的实体名和 query 里的说法对不上
- reranker 会被标题党 chunk 带偏
- 附录、目录、图注、参考文献这类结构噪声太多
- benchmark 的自动分数和人实际看答案经常不一致

### V1：先解决“中文问英文论文”这个硬问题

最早最明显的问题是：用户问的是中文，论文是英文，向量空间没有对齐。

做法：

- 在检索阶段加入 query 翻译
- 对英文论文启用 `force_language="english"`
- QueryEntityExtractor 在英文论文场景下尽量输出英文实体

效果：

- RL 那篇和 GRPO 那篇的召回明显改善
- 说明这不是 reranker 的问题，而是最前面的 query-language alignment 就错了

这一步的经验是：

> 多语言 RAG 不先解决 query 和 corpus 的语言对齐，后面做再多优化都只是补锅。

### V2：从“只看 top-k chunk”转向“章节内再检索”

后面发现很多论文题不是单个 chunk 能答的，尤其是跨章节题和对比题。

做法：

- 第一阶段先向量检索
- 从 top 结果里提取章节名
- 在这些章节里做一次 chapter-scoped search
- 再和图检索结果做 RRF 融合

这个阶段的一个典型问题是 attention 那题：

- 问题：`encoder 和 decoder 各由几层组成`
- 现象：encoder chunk 被召回了，decoder chunk 因为相似度稍低被挤出 top-k
- 解决：利用 `NEXT_CHUNK` 扩展，把相邻 chunk 带回来

经验：

> 论文 QA 的很多答案是连续段落，而不是孤立 chunk。只做独立 chunk 排序，天然会漏掉相邻信息。

### V3：发现“检索对了，不代表答案就会答对”

这是整个项目里最重要的认知转折之一。

以 `GRPO vs PPO` 那题为例：

- 检索到了一个标题非常相关的附录 chunk
- reranker 也给了很高分
- 但那个 chunk 讲的是 KL divergence 和训练细节
- 真正最重要的信息是“GRPO 去掉了 value model / critic”
- LLM 因为先看到了高分但不核心的 chunk，先写了次要差异

结论：

> “检索相关” 和 “适合生成答案” 不是一回事。

于是我们开始加入 answer selection 这一层，而不是把 rerank top-k 直接喂给生成模型。

### V4：compare selector，不再按 rerank 顺序硬切

对比类问题最先暴露这个问题，因为它们很容易被“标题很像但内容不互补”的 chunk 带偏。

做法：

- 识别 compare query
- 拆成 2 到 3 个子问题
- 对每个子问题独立 rerank
- 用贪心策略选“互补”的 3 到 4 个 answer docs

典型收益：

- `GRPO 相比 PPO 去掉了什么，为什么更高效`
- 从“答得语义相关但没抓住核心差异”
- 变成“先说去掉 value model，再说 why”

经验：

> compare query 的关键不是“找最相关的 chunk”，而是“找互补的 chunk”。

### V5：conclusion selector，修“核心结论题”

RL 那篇的 `论文的核心结论是什么？` 是一个很典型的坑。

一开始的错误现象：

- top1 常常来自 `Conclusion and Limitations`
- 但这章里混着真正结论、局限性、future work、策略建议
- LLM 被 “we hope / future work / more exploration” 这类句子带偏
- 最终答成“RL 很有潜力，只是还需要更好方法”

真正的论文主结论其实是：

- 当前 RLVR 没有真正突破 base model 的 reasoning boundary
- 提升更多来自 sampling efficiency
- 甚至可能缩小可解问题 coverage

做法分了两步：

第一步：

- 不是只修 selector，而是先修 rerank 前的 candidate 保活
- 对 conclusion query，主动保留 `Introduction / Abstract / Conclusion / Discussion` 的 chunk
- 防止结构过滤把最关键的 intro evidence 先挤出去

第二步：

- conclusion selector 给主结论句、中心发现、直接 claim 更高权重
- 对 `future work / limitations / we hope / may be crucial` 这类段落做惩罚

经验：

> 对“核心结论题”，最重要的信息不一定在 Conclusion 章节里，往往在 Introduction 里的总结句、图 1 的文字说明、Discussion 里的判断性段落里。

### V6：metric-definition selector，修“coverage 是什么”这种定义题

`coverage 这个指标在文中想衡量什么？` 这题很有代表性。

一开始并不是没检索到，而是检索到了太多这种东西：

- `Coverage (pass@k)`
- 图表标题
- 坐标轴文字
- 可视化 caption

于是生成模型会把：

- `coverage 想衡量什么`

错误地答成：

- `coverage 就是 pass@k 的计算方式`

后来我们把这类题单独识别成 metric-definition query，做两件事：

- 选择 answer docs 时，优先正文里的定义句
- prompt 明确要求：先答概念上想衡量什么，再答它如何 operationalize 成 pass@k

经验：

> 指标定义题最怕图表标签抢答案。图表里写的往往是名字，不是定义。

### V7：公式型、技术报告型 PDF 的入库问题

GRPO / DeepSeek-R1 这种技术报告特别能暴露切块问题。

最初 `grpo.pdf` 被切得非常碎：

- 公式单独成 chunk
- 说明文字和公式被切开
- chunk 数暴涨
- embedding 语义很弱
- 入库 LLM 调用数量暴涨

我们后面明确意识到：

> 公式不应该孤立成 chunk，而应该和上下文文字粘在一起。

后来的改法：

- 在更早的 unit / semantic block 阶段就开始粘连短公式块
- 短公式、短代码优先并入相邻文本
- 保留 `parent_block_id`
- 检索时支持 sibling chunk 扩展

经验：

- 只看 chunk 数下降了没，不够
- 更重要的是：公式和解释文字有没有重新回到同一个语义单元里

### V8：不是所有 benchmark 题都成立，先保证题本身是对的

后来我们发现一个很大的坑：

- `grpo.pdf` 其实是 DeepSeek-R1 技术报告
- 不是 GRPO 原始论文
- 于是原 benchmark 里有几道题，答案根本不在文档里

这一步很重要，因为它暴露出一个常见误区：

> 很多人把“系统答不出来”当系统问题，但其实有时是 benchmark 设计错了，问了文档里不存在的内容。

所以后来我们重写了 GRPO 相关 benchmark，让问题和文档本身对齐，比如：

- DeepSeek-R1-Zero 怎么训练
- GRPO 的 advantage 公式
- DeepSeek-R1 多阶段训练
- GRPO 为什么去掉 value model

### V9：自动指标不是最终标准，完整 answer 才是

这个项目里另一个非常关键的认知转折是：

- 关键词 recall 很适合做调试指标
- 但它不适合做最终成绩

典型例子：

- 答案写了 `value model`
- benchmark 关键词写的是 `critic model`
- 语义上是对的
- 自动评测却记成错

所以最后我们的标准改成：

- 自动指标只看趋势，不做最终判断
- 最终评测看 `answer_full`
- 人工判定为 `对 / 部分对 / 错`

这一步很适合在面试里主动说，因为它体现你知道：

> 一个系统不应该为了适配评测脚本，而偏离真实任务目标。

---

## 5. 这一路最有价值的踩坑经验

下面这些不是“问题清单”，而是面试里最值得讲的工程判断。

### 5.1 实体同义词问题，本质是实体归一化，不是补关键词

最典型的例子：

- query 里说 `critic model`
- 图里存的是 `value model`
- `identify_entities_in_query` 和图匹配都按字符串做
- 完全匹配不上

这不是 reranker 问题，也不是 prompt 问题，而是实体归一化问题。

过渡方案：

- alias expansion

更根本的方案：

- 入库时给实体存 `aliases`
- 或者实体节点自己也做 embedding

面试里推荐的诚实说法：

> 我们后面已经验证这类问题的根因是 entity normalization，而不是简单关键词漏召回。alias expansion 只是过渡方案，最终更合理的是入库时建 aliases，或者做 entity embedding。

### 5.2 reranker 不是越强越好，它也会稳定地把你带偏

我们一开始对 reranker 的直觉是：

- 它能把结果排得更准

后来才发现：

- 它很容易被标题、章节名、附录标题吸引
- 尤其是对 `compare`、`conclusion`、`metric definition` 这几类题
- 它会稳定地选到“看起来最相关、但不是最有用”的 chunk

所以后面的策略不是“更信 reranker”，而是：

- 主检索排序和 answer-doc 选择分层
- reranker 只负责打分
- selector 负责“最终哪些 docs 真的送给 LLM”

### 5.3 附录不能一刀切过滤

这个坑很早就踩到了。

一开始觉得附录噪声大，于是想强压附录。

后来发现：

- `A.3 A Comparison of GRPO and PPO` 这种附录里就有非常关键的对比信息

所以真正合理的策略不是：

- 过滤所有 appendix

而是：

- 区分“有用附录”和“无用附录”
- 至少不要让 appendix 独占 top-k
- 最好在 answer selection 层按题型处理

### 5.4 图注、坐标轴、可视化碎片是论文 QA 的高危噪声

这类噪声很阴险，因为它们往往：

- 词特别像 query
- 标题又很像正确答案
- reranker 也容易给高分

但实际作用只是：

- 干扰生成
- 让模型先复述图里写了什么
- 忽略正文定义句和结论句

所以我们最终专门加了：

- `caption_like` 结构噪声识别
- 对非视觉类 query 压制图注 / 坐标轴 / chart legend / figure caption

### 5.5 benchmark 脚本本身也要当产品维护

后面我们还踩到两个“评测系统自己的坑”：

- benchmark 实际已经是 50 题，但脚本和输出文件名还叫 `benchmark_19_queries`
- 自动分数看起来下降，不代表完整 answer 变差

这类问题说明：

> 评测脚本本身也是系统的一部分，也需要版本管理、命名治理和可解释性。

---

## 6. 目前这个系统的真实水平

这里不要吹，也不要自黑，讲真实状态最加分。

### 6.1 现在已经做好的

- 中文 query 问英文论文，检索对齐明显比最初稳定
- 实体驱动的图检索能为向量检索补召回
- compare / conclusion / metric-definition 三类高频难题已经有专门 answer selector
- `NEXT_CHUNK` 和 `parent_block_id` 对连续段落、公式说明分离的问题有帮助
- 附录 / 目录 / 图注 / references 的结构噪声处理比早期版本稳定很多
- benchmark 已从“只看关键词”转向“看完整 answer”

### 6.2 还没彻底解决的

#### 1. 实体别名的根治方案还没落完

现在只是部分靠 alias expansion 和 query 侧补救。

最终更合理的是：

- 入库时写入实体别名
- 或做实体向量化

#### 2. 跨章节题仍然是系统上限

即使加了章节二次检索、NEXT_CHUNK、selector，跨章节整合能力仍然不如人。

原因很直接：

- 候选池有限
- chunk 仍然是局部语义单元
- 生成阶段没有真正的多轮检索规划

#### 3. 技术报告型 PDF 仍然比普通论文更难

因为它们通常有：

- 长公式
- 图表密集
- 附录特别长
- 章节写法不标准

这类文档对切块、结构识别、caption 去噪都更敏感。

### 6.3 最新这一轮人工看完整答案的结果

我们最终不再以关键词 recall 为准，而是人工看 `answer_full`。

最新一轮 50 题人工结论：

- 对：39
- 部分对：9
- 错：2

这组结果比任何自动分数都更适合在面试里讲，因为它更贴近真实用户体验。

---

## 7. 我最推荐在面试里重点讲的 4 个 case

如果面试官时间不多，不要平铺直叙讲所有功能，讲 4 个代表性 case 就够了。

### Case 1：GRPO vs PPO

现象：

- 检索到了相关 chunk
- 但答案没有先说最核心的区别

根因：

- answer generation 直接按 rerank 顺序写
- 没有区分“最相关” 和 “最适合作答”

解决：

- compare selector
- prompt 强调先答核心差异

可体现的能力：

- 你知道检索和生成之间还有一层 answer selection

### Case 2：RL 论文核心结论题

现象：

- 系统一度答成“RL 很有潜力，需要更多探索策略”

根因：

- 关键证据在 Introduction
- 结构过滤和 reranker 把 Conclusion and Limitations 里的 future work 顶上来了

解决：

- conclusion query 候选保活
- conclusion selector
- 压 future work / limitations

可体现的能力：

- 你能分清 retrieval miss、candidate starvation、selector 失效、generation 偏航

### Case 3：coverage 指标题

现象：

- 系统一度把 coverage 和 pass@k 直接混为一谈

根因：

- 图表标签和可视化 caption 抢答案
- LLM 把“图里写了什么”当“定义”

解决：

- metric-definition selector
- caption_like 去噪
- prompt 区分“概念定义”和“操作化方式”

可体现的能力：

- 你能处理“定义题”和“图表噪声”的细粒度问题

### Case 4：grpo.pdf 入库变慢、切块变差

现象：

- 入库耗时异常
- chunk 数过多
- 公式单独成块，检索质量差

根因：

- LLM 调用签名错误导致公式修复异常
- 公式块切得过碎

解决：

- 修复 chat 调用方式
- 调整公式块和文本块的粘连策略
- 加 `parent_block_id`

可体现的能力：

- 你不只会调检索，还会从 ingestion 端追根溯源

---

## 8. 面试时建议怎么讲

### 8.1 90 秒版本

> 我做了一个论文问答 GraphRAG 系统，把 PDF 结构化入库到 Neo4j，融合向量检索、实体驱动图检索、RRF、Reranker 和 LLM 生成。项目后期我发现真正难的不是“把图搭起来”，而是三件事：第一，中文 query 问英文论文的语言对齐；第二，reranker 会被附录、标题和图注带偏；第三，自动评测经常误判语义上其实正确的答案。所以我后来重点做了 answer selector、结构去噪、caption 过滤，以及从关键词评测切到完整答案人工判定。这个项目最后最有价值的不是某个单点指标，而是我把检索错误、选择错误、生成错误和 benchmark 错误分开定位了。 

### 8.2 3 分钟版本

建议按这条顺序讲：

1. 为什么做这个项目
2. 系统架构是什么
3. 最关键的 3 个坑
4. 我是怎么一步一步定位和修的
5. 当前真实效果和剩余短板

推荐表达：

> 这个项目一开始是一个混合检索系统，但做到后面我发现真正重要的是“诊断能力”。例如有些题不是检索错了，而是检索对了但 answer doc 选错了；有些题不是系统错了，而是 benchmark 问了文档里根本没有的内容；还有些题自动分数很低，但人看完整回答其实是对的。我后面最大的收获不是把某个 recall 拉高，而是建立了一套从 ingest、retrieval、rerank、answer selection 到 benchmark 的分层排错方法。

### 8.3 如果面试官让我现场讲数据库图

推荐直接这样讲，基本 30 到 45 秒能说明白：

> 这张图里我没有把整篇论文做成一个很重的知识图谱，而是只保留了对问答最有帮助的三层结构。第一层是 Chunk，它承载原文证据和 embedding；第二层是 Entity，它承载论文里的关键概念；中间 `HAS_ENTITY` 把证据和概念连起来。除此之外，我还保留了两种结构边：`RELATES_TO` 表示概念之间的语义关系，`NEXT_CHUNK` 表示原文里的前后连续关系。这样做的目的不是替代向量检索，而是给向量检索补两件事，一是概念级召回，二是连续上下文补全。

如果对方继续追问“那这个设计解决了什么实际问题”，可以顺着说：

- 它能解释为什么 `value model` 和 `critic` 这类概念问题会暴露实体归一化短板。
- 它能解释为什么 `encoder 和 decoder 各几层` 这类题需要 `NEXT_CHUNK` 去补相邻证据。
- 它也能解释为什么我后面会继续加 `parent_block_id`，因为有些答案不是跨相邻 chunk，而是同一个 semantic block 被 splitter 切开了。

---

## 9. 面试官高概率会追问的问题

### Q1：为什么用 GraphRAG，而不是纯向量 RAG？

推荐回答：

> 纯向量 RAG 更擅长找语义相似的 chunk，但对论文里的实体关系不敏感。比如比较两个方法、追踪一个概念跨章节出现时，显式实体和图边可以补一些向量检索漏掉的内容。不过我的系统也不是靠图做复杂推理，图的主要作用是 recall enhancement，而不是替代向量检索。

### Q2：你这个系统最难的问题是什么？

推荐回答：

> 不是建图本身，而是“检索结果到底适不适合生成答案”。很多失败案例不是没检索到，而是检索到了不该优先喂给 LLM 的内容，比如附录标题、图注、future work。我后来专门加了 answer selector，才把这个问题真正拆开。

### Q3：为什么后来不再看自动指标？

推荐回答：

> 不是完全不看，而是把它们降级成诊断指标。关键词 recall 适合看趋势，但不适合当最终成绩，因为它会误判很多语义正确但措辞不同的答案。最终我改成看完整 answer 的人工判定，这更接近真实用户体验。

### Q4：你这个项目还没解决什么？

推荐回答：

> 我会重点说三点：实体归一化还没有在入库阶段彻底完成；跨章节整合能力仍然是上限；技术报告和公式密集型 PDF 依然更难。这三点我都已经知道根因，也有清晰的下一步方案。

### Q5：如果再做一版，你最优先做什么？

推荐回答：

> 第一是入库时做实体 aliases；第二是把跨章节题做成多轮检索或 query decomposition；第三是继续减少图注、表格、可视化对正文定义句和结论句的干扰。

---

## 10. 最后一句总结

这个项目最值得讲的，不是“我做了一个 GraphRAG”，而是：

> 我把一个看起来只是“检索不准”的问题，拆成了入库、实体归一化、语言对齐、结构噪声、候选池饥饿、答案选择、生成偏航和评测失真这几类不同问题，并且针对每一类问题都做过实际迭代。

如果面试官能听懂这一句，基本就会知道你不是在背八股，而是真的做过系统。

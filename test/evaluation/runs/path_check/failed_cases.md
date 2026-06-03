## paper_001 What is the core architecture proposed in Attention Is All You Need?

Expected:
- source: Attention Is All You Need
- cards: Attention Is All You Need
- allow_web: false

Actual:
- tool_plan: {"intent": "query about the core architecture of the 'Attention Is All You Need' paper", "answer_mode": "wiki_card", "use_wiki": true, "use_web": false, "use_resources": false, "open_cards": true, "tools": [{"name": "wiki_search", "query": "Attention Is All You Need core architecture", "reason": "The user is asking about a well-known paper's architecture, which is likely documented in their private Wiki as a stable concept."}, {"name": "wiki_card", "query": "What is the core architecture proposed in Attention Is All You Need?", "reason": "open matched Wiki cards"}]}
- citations: [{"card_id": "6e91883d-e6a7-4887-9708-58166fc37dba", "title": "Attention Is All You Need", "page_type": "PaperPage", "summary": "提出了一种全新的序列转导模型架构 Transformer，完全基于注意力机制，摒弃了循环和卷积结构，在机器翻译任务上取得了更优的性能和更高的训练效率。", "markdown_path": "oss://fysjarvis/users/admin/data/generated/wiki/papers/attention-is-all-you-need.md"}, {"card_id": "28ab6893-e69e-452f-8f81-5e120783d25e", "title": "Self-Attention", "page_type": "ConceptPage", "summary": "一种注意力机制，用于计算单个序列中不同位置之间的表示关系，是 Transformer 的核心组件。", "markdown_path": "oss://fysjarvis/users/admin/data/generated/wiki/concepts/self-attention.md"}, {"card_id": "30148c8e-0845-46d8-9a6a-d0fd1c895b3b", "title": "Transformer", "page_type": "MethodPage", "summary": "一种完全基于注意力机制的序列转导模型，由编码器和解码器堆叠而成，每层包含多头自注意力和前馈网络，广泛应用于自然语言处理任务。", "markdown_path": "oss://fysjarvis/users/admin/data/generated/wiki/methods/transformer.md"}, {"card_id": "88d65b1a-ad56-4bfc-9669-fcb97d6cc26c", "title": "Hidden representations", "page_type": "ConceptPage", "summary": "LLM 中间层的隐藏表示，可用于探测模型内部状态、任务感知和推理过程。", "markdown_path": "oss://fysjarvis/users/admin/data/generated/wiki/concepts/hidden-representations.md"}, {"card_id": "6cb2f34c-ce24-4942-bc3f-e8c021176c7e", "title": "律师推荐和服务小程序的设计与实现", "page_type": "PaperPage", "summary": "本论文设计并实现了一个律师推荐和服务系统，通过在线服务平台使律师工作更高效，用户法律咨询更便捷。系统包含用户、律师、管理员三个角色，用户端为小程序，提供注册登录、查看律师信息、预约等功能。", "markdown_path": "oss://fysjarvis/users/admin/data/generated/wiki/papers/律师推荐和服务小程序的设计与实现.md"}, {"card_id": "c37a6ed4-e585-4415-8dc3-73ada6538cf7", "title": "Does Reinforcement Learning Really Incentivize Reasoning Capacity in LLMs Beyond the Base Model?", "page_type": "PaperPage", "summary": "本文质疑了当前广泛使用的 Reinforcement Learning with Verifiable Rewards (RLVR) 是否真正扩展了大型语言模型 (LLM) 的推理能力边界。通过系统实验，作者发现 RLVR 并未提升基座模型在原本无法解决的问题上的表现，而是通过增加推理计算量（如更长的思维链）更好地利用了基座模型已有的能力。相比之下，蒸馏 (distillation) 能够引入新知识，从而真正扩展推理边界。", "markdown_path": "oss://fysjarvis/users/admin/data/generated/wiki/papers/does-reinforcement-learning-really-incentivize-reasoning-capacity-in-llms-beyond.md"}]
- answer excerpt: 结论： 《Attention Is All You Need》论文提出的核心架构是 **Transformer**，其完全基于注意力机制，摒弃了传统的循环（RNN）和卷积（CNN）结构，由编码器（Encoder）和解码器（Decoder）堆叠组成，核心组件包括多头自注意力（Multi-Head Self-Attention）和前馈网络（Feed-Forward Network）[1][3]。 结构化解析： 1. **整体架构** - 采用编码器-解码器结构，编码器将输入序列映射为连续表示，解码器基于编码输出生成目标序列[1][3]。 -...

Reviewer:
- final_score: 0.9327
- answer_confidence: 0.98
- citation_grounding: 1.0
- reason: fallback review; keyword_score=0.67, retrieval_hit_rate=1.00, web_used=False, citations=6

Likely fix:
- wiki_card_content_too_weak

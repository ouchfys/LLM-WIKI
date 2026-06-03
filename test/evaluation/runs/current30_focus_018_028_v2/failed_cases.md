## paper_028 Why is estimating difficulty without generating output tokens useful?

Expected:
- source: The LLM Already Knows: Estimating LLM-Perceived Question Difficulty via Hidden Representations
- cards: The LLM Already Knows: Estimating LLM-Perceived Question Difficulty via Hidden Representations
- allow_web: false

Actual:
- tool_plan: {"intent": "understand the utility of difficulty estimation without token generation", "answer_mode": "wiki_card", "use_wiki": true, "use_web": false, "use_resources": false, "open_cards": true, "tools": [{"name": "wiki_search", "query": "estimating difficulty without generating output tokens", "reason": "The user's question pertains to a conceptual understanding likely covered in stable Wiki notes about efficiency or model evaluation techniques."}, {"name": "wiki_card", "query": "Why is estimating difficulty without generating output tokens useful?", "reason": "open matched Wiki cards"}]}
- citations: [{"card_id": "b6563ca0-e02f-4786-b70b-d474a6991141", "title": "The LLM Already Knows: Estimating LLM-Perceived Question Difficulty via Hidden Representations", "page_type": "PaperPage", "summary": "The paper's contribution is hidden-state-only difficulty estimation: it uses the target LLM's hidden representations, models token-level generation as a Markov chain, and learns a value function over hidden states so difficulty can be estimated from the initial hidden state without generating output tokens.", "markdown_path": "oss://fysjarvis/users/admin/data/generated/wiki/papers/the-llm-already-knows-estimating-llm-perceived-question-difficulty-via-hidden-re.md"}, {"card_id": "23b21f9a-ab37-4a36-8c8f-a656b775b97d", "title": "Probing the Difficulty Perception Mechanism of Large Language Models", "page_type": "PaperPage", "summary": "本文研究大型语言模型（LLMs）是否在内部表示中隐式编码问题难度。通过在线性探针（linear probe）作用于LLMs的最终token表示，作者证明数学问题的难度可以被线性建模。进一步定位了最终Transformer层的特定注意力头，这些注意力头对简单和困难问题具有相反的激活模式，从而实现难度感知。消融实验验证了定位的准确性。该工作为使用LLMs作为自动难度标注器提供了实践支持，有望减少对昂贵人工标注的依赖。", "markdown_path": "oss://fysjarvis/users/admin/data/generated/wiki/papers/probing-the-difficulty-perception-mechanism-of-large-language-models.md"}, {"card_id": "6da61a94-0cb9-4c1b-ab48-f6c641df2feb", "title": "Value-based Difficulty Estimation from Hidden Representations", "page_type": "MethodPage", "summary": "该方法将LLM的token级生成过程建模为马尔可夫链，并定义一个价值函数来估计从任意隐藏状态出发的期望输出质量。难度估计直接使用输入编码后的初始隐藏状态对应的价值函数值，无需生成任何输出token。\n\n补充：估计问题对模型而言难易程度的方法，可用于样本筛选、推理预算分配和自适应解题策略。", "markdown_path": "oss://fysjarvis/users/admin/data/generated/wiki/methods/value-based-difficulty-estimation-from-hidden-representations.md"}, {"card_id": "8e8d5634-b538-483b-835b-a771405fe4c8", "title": "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning", "page_type": "PaperPage", "summary": "本文提出 DeepSeek-R1，通过纯强化学习（RL）激励大语言模型的推理能力，无需人工标注的推理轨迹。模型在数学、编程竞赛和 STEM 领域等可验证任务上取得了超越对比模型的性能。", "markdown_path": "oss://fysjarvis/users/admin/data/generated/wiki/papers/deepseek-r1-incentivizing-reasoning-capability-in-llms-via-reinforcement-learnin.md"}, {"card_id": "cd863c0d-bbc5-45ac-a60c-d1761ba6b331", "title": "LLM-Perceived Question Difficulty", "page_type": "ConceptPage", "summary": "LLM感知难度是指大语言模型在内部处理问题时，其隐藏表示所反映的问题困难程度，而非基于人类标注或外部指标。该概念强调从模型自身视角评估难度，可用于自适应推理和性能分析。\n\n补充：大型语言模型（LLMs）在内部表示中隐式编码问题难度的能力。通过线性探针和注意力头激活模式分析，可以揭示模型对问题难度的感知机制。", "markdown_path": "oss://fysjarvis/users/admin/data/generated/wiki/concepts/llm-perceived-question-difficulty.md"}, {"card_id": "c37a6ed4-e585-4415-8dc3-73ada6538cf7", "title": "Does Reinforcement Learning Really Incentivize Reasoning Capacity in LLMs Beyond the Base Model?", "page_type": "PaperPage", "summary": "本文质疑了当前广泛使用的 Reinforcement Learning with Verifiable Rewards (RLVR) 是否真正扩展了大型语言模型 (LLM) 的推理能力边界。通过系统实验，作者发现 RLVR 并未提升基座模型在原本无法解决的问题上的表现，而是通过增加推理计算量（如更长的思维链）更好地利用了基座模型已有的能力。相比之下，蒸馏 (distillation) 能够引入新知识，从而真正扩展推理边界。", "markdown_path": "oss://fysjarvis/users/admin/data/generated/wiki/papers/does-reinforcement-learning-really-incentivize-reasoning-capacity-in-llms-beyond.md"}]
- answer excerpt: ### 结论 不生成输出token即可估计问题难度的核心价值在于**效率提升**和**资源优化**，具体表现为： 1. **节省计算成本**：避免实际生成token的昂贵计算（[1][3]） 2. **实时决策支持**：在推理前即可分配计算资源（如调整思维链长度）或过滤超难样本（[1][5]） 3. **揭示模型认知**：通过隐藏状态直接观测LLM对问题的内在难度感知（[2][5]） ### 结构化分析 1. **技术原理** - 基于马尔可夫链建模：将token生成过程转化为状态价值函数，从初始隐藏状态预测最终输出质量（[1][3]） -...

Reviewer:
- final_score: 0.975
- answer_confidence: 1.0
- citation_grounding: 1.0
- reason: fallback review; keyword_score=1.00, retrieval_hit_rate=1.00, web_used=False, citations=6

Likely fix:
- wiki_card_content_too_weak

## paper_018 What are the main frontend and backend technologies used by the lawyer service mini-program?

Expected:
- source: 律师推荐和服务小程序的设计与实现
- cards: 律师推荐和服务小程序的设计与实现
- allow_web: false

Actual:
- tool_plan: {"intent": "query_tech_stack", "answer_mode": "tools", "use_wiki": true, "use_web": true, "use_resources": false, "open_cards": true, "tools": [{"name": "wiki_search", "query": "lawyer service mini-program frontend backend technologies", "reason": "Check if the user's private Wiki has documented the tech stack for lawyer service mini-programs."}, {"name": "wiki_card", "query": "What are the main frontend and backend technologies used by the lawyer service mini-program?", "reason": "open matched Wiki cards"}, {"name": "web_search", "query": "main frontend and backend technologies used by lawyer service mini-program", "reason": "Fallback to web search if the Wiki lacks this specific tech stack information, as mini-program tech stacks can be platform-specific or mainstream."}]}
- citations: [{"card_id": "6cb2f34c-ce24-4942-bc3f-e8c021176c7e", "title": "律师推荐和服务小程序的设计与实现", "page_type": "PaperPage", "summary": "The paper designs a lawyer recommendation and service mini-program using a separated frontend/backend architecture. The frontend uses Vue for the law-firm management UI and WeChat Developer Tools / WeChat Mini Program APIs for the client mini-program; the backend is built with Java Spring Boot, MySQL, ElasticSearch, WebSocket, and Ollama-backed AI service components.", "markdown_path": "oss://fysjarvis/users/admin/data/generated/wiki/papers/律师推荐和服务小程序的设计与实现.md"}]
- answer excerpt: 结论： 该律师服务小程序采用前后端分离架构，主要技术栈如下： - **前端**：Vue.js + Element UI（律所管理后台）、微信开发者工具 + 微信小程序API（客户端小程序） - **后端**：Java Spring Boot框架、MySQL数据库、ElasticSearch搜索引擎、WebSocket实时通信 依据与结构： 1. **核心证据**： - Wiki卡片[1]明确记载了该系统的技术选型，包括前后端具体技术组件（Vue、Spring Boot等）及基础设施（MySQL、ElasticSearch）[1]。 2. **补充说明**： -...

Reviewer:
- final_score: 0.9775
- answer_confidence: 1.0
- citation_grounding: 1.0
- reason: fallback review; keyword_score=1.00, retrieval_hit_rate=1.00, web_used=True, citations=1

Likely fix:
- wiki_card_content_too_weak

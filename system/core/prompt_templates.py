from difflib import SequenceMatcher
from typing import Dict, List, Optional

from langchain_core.documents import Document


class PromptTemplateManager:
    """Prompt builders for document-grounded QA."""

    @staticmethod
    def build_rag_prompt(
        query: str,
        contexts: List[Document],
        prompt_type: str = "default",
        examples: Optional[List[Dict[str, str]]] = None,
        user_preferences: Optional[Dict[str, str]] = None,
        discussed_topics: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        preference_block = PromptTemplateManager._build_preference_block(
            user_preferences, discussed_topics,
        )

        if prompt_type == "default":
            return PromptTemplateManager._default_prompt(
                query, contexts, user_preferences, discussed_topics,
            )
        if prompt_type == "cot":
            return preference_block + PromptTemplateManager._chain_of_thought_prompt(query, contexts)
        if prompt_type == "react":
            return preference_block + PromptTemplateManager._react_prompt(query, contexts)
        if prompt_type == "few_shot":
            return preference_block + PromptTemplateManager._few_shot_prompt(query, contexts, examples)

        print(f"[PromptTemplate] Unknown prompt type '{prompt_type}', using default")
        return PromptTemplateManager._default_prompt(
            query, contexts, user_preferences, discussed_topics,
        )

    @staticmethod
    def _build_preference_block(
        user_preferences: Optional[Dict[str, str]] = None,
        discussed_topics: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        """构建用户偏好注入块，插入到 prompt 的 Requirements 之前。"""
        parts: List[str] = []

        if user_preferences:
            pref_lines = []
            # 可读性映射
            key_labels = {
                "language_preference": "回答语言",
                "answer_length": "答案长度",
                "answer_style": "答案风格",
                "retrieval_strategy": "检索策略",
                "detail_level": "细节程度",
                "citation_preference": "引用方式",
                "explanation_style": "解释风格",
            }
            for key, value in user_preferences.items():
                label = key_labels.get(key, key)
                pref_lines.append(f"- {label}: {value}")
            if pref_lines:
                parts.append(
                    "# User Preferences (adapt your answer style accordingly)\n"
                    + "\n".join(pref_lines)
                )

        if discussed_topics:
            topic_lines = []
            for topic_info in discussed_topics[:5]:  # 最多 5 条
                topic = topic_info.get("topic", "")
                paper = topic_info.get("paper", "")
                if paper:
                    topic_lines.append(f"- {topic} (论文: {paper})")
                else:
                    topic_lines.append(f"- {topic}")
            if topic_lines:
                parts.append(
                    "# Previously Discussed Topics (no need to re-explain basics)\n"
                    + "\n".join(topic_lines)
                )

        if not parts:
            return ""
        return "\n\n".join(parts) + "\n\n"

    @staticmethod
    def _default_prompt(
        query: str,
        contexts: List[Document],
        user_preferences: Optional[Dict[str, str]] = None,
        discussed_topics: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        context_str = PromptTemplateManager._format_contexts(contexts)
        preference_block = PromptTemplateManager._build_preference_block(
            user_preferences, discussed_topics,
        )

        requirements = [
            "1. Use only information that is explicitly present in the provided document snippets.",
            "2. If a detail is not clearly stated in the snippets, say so explicitly instead of inferring or adding outside knowledge.",
            "3. Cite supporting snippet numbers in the answer.",
            "4. Put the direct answer first, then add the necessary evidence or clarification.",
            "5. Keep the answer complete and do not omit key information already present in the snippets.",
            "6. If the user question contains Chinese characters, reply in Simplified Chinese. Otherwise reply in English.",
        ]
        requirements.extend(
            PromptTemplateManager._build_query_specific_requirements(
                query,
                start_index=len(requirements) + 1,
            )
        )

        requirements_str = "\n".join(requirements)
        return (
            "You are a careful academic QA assistant. Answer strictly from the provided snippets.\n"
            f"{preference_block}"
            "# Requirements\n"
            f"{requirements_str}\n\n"
            "# Snippets\n"
            f"{context_str}\n\n"
            "# User Question\n"
            f"{query}\n\n"
            "# Answer\n"
        )

    @staticmethod
    def _build_query_specific_requirements(
        query: str,
        start_index: int = 7,
    ) -> List[str]:
        query_text = query or ""
        lowered = query_text.lower()
        requirements: List[str] = []

        def has_any(keywords: List[str]) -> bool:
            return any(keyword in query_text or keyword in lowered for keyword in keywords)

        compare_keywords = [
            "\u533a\u522b",
            "\u5bf9\u6bd4",
            "\u6bd4\u8f83",
            "\u5dee\u5f02",
            "\u76f8\u6bd4",
            "\u76f8\u8f83",
            "\u76f8\u5bf9",
            "\u4f18\u52bf",
            "\u52a3\u52bf",
            "\u4f18\u7f3a\u70b9",
            "difference",
            "differences",
            "compare",
            "comparison",
            "compared",
            "compare to",
            "compared to",
            "compared with",
            "versus",
            "vs",
            "pros and cons",
            "difference between",
        ]
        conclusion_keywords = [
            "\u6838\u5fc3\u7ed3\u8bba",
            "\u4e3b\u8981\u7ed3\u8bba",
            "\u7ed3\u8bba",
            "\u603b\u7ed3",
            "\u6838\u5fc3\u89c2\u70b9",
            "main conclusion",
            "core conclusion",
            "key conclusion",
            "takeaway",
        ]
        list_keywords = [
            "\u54ea\u4e9b",
            "\u54ea\u51e0\u4e2a",
            "\u54ea\u51e0\u79cd",
            "\u54ea\u51e0\u7c7b",
            "\u5305\u62ec",
            "\u6570\u636e\u96c6",
            "\u6a21\u5757",
            "\u65b9\u6cd5",
            "\u9700\u6c42",
            "\u6c9f\u901a\u65b9\u5f0f",
            "which",
            "what are",
            "what kinds of",
            "what methods",
            "what operations",
            "supported operations",
            "regularization",
            "benchmark",
            "benchmarks",
        ]
        non_functional_keywords = [
            "\u975e\u529f\u80fd\u6027\u9700\u6c42",
            "non-functional",
        ]
        stage_keywords = [
            "\u9636\u6bb5",
            "\u6b65\u9aa4",
            "\u6d41\u7a0b",
            "phase",
            "stage",
            "pipeline",
            "process",
        ]
        first_second_stage_keywords = [
            "\u7b2c\u4e00\u9636\u6bb5",
            "\u7b2c\u4e8c\u9636\u6bb5",
            "first stage",
            "second stage",
        ]
        deployment_keywords = [
            "\u90e8\u7f72",
            "\u67b6\u6784",
            "deployment",
            "deployed",
            "architecture",
        ]
        operations_keywords = [
            "\u64cd\u4f5c",
            "\u652f\u6301\u54ea\u4e9b\u64cd\u4f5c",
            "\u529f\u80fd",
            "operation",
            "operations",
            "supported operations",
        ]
        dataset_keywords = [
            "\u6570\u636e\u96c6",
            "\u6d4b\u8bd5",
            "\u8bc4\u6d4b",
            "dataset",
            "datasets",
            "benchmark",
            "benchmarks",
        ]
        formula_keywords = [
            "\u516c\u5f0f",
            "\u65b9\u7a0b",
            "\u76ee\u6807\u51fd\u6570",
            "\u5173\u952e\u9879",
            "\u5305\u542b\u54ea\u4e9b",
            "\u9664\u4e86",
            "formula",
            "equation",
            "objective",
            "loss",
            "term",
            "terms",
            "component",
            "components",
            "advantage",
        ]
        metric_definition_keywords = [
            "\u6307\u6807",
            "\u8861\u91cf",
            "\u5b9a\u4e49",
            "\u542b\u4e49",
            "\u8868\u793a\u4ec0\u4e48",
            "\u60f3\u8861\u91cf\u4ec0\u4e48",
            "metric",
            "metrics",
            "measure",
            "measures",
            "definition",
            "defined",
            "meaning",
            "coverage",
            "pass@k",
        ]

        if has_any(compare_keywords):
            requirements.append(
                "For comparison questions, state the single most important architectural or conceptual difference in the first sentence, then list the remaining differences by importance."
            )
            requirements.append(
                "If the question asks what was reduced or removed, keep the original overhead term explicit when supported by the snippets, such as tuning, hyperparameter tuning, 调优, or 调参, instead of paraphrasing it away."
            )

        if has_any(conclusion_keywords):
            requirements.append(
                "For conclusion or summary questions, the first sentence must give the main conclusion directly, then add 2 to 4 supporting points. Do not spend the opening on background."
            )

        if has_any(list_keywords):
            requirements.append(
                "For listing questions, enumerate the concrete items explicitly mentioned in the snippets and prefer the original terms over broad paraphrases."
            )
            requirements.append(
                "Only repeat a claimed total count such as 'three types' or 'several stages' if you can enumerate every item from the provided snippets. Otherwise say that the provided snippets explicitly show the following items."
            )

        if has_any(non_functional_keywords):
            requirements.append(
                "For non-functional requirements questions, prioritize explicit labels or metrics such as response time, throughput, concurrency, stability, security, usability, cost, or intelligence-related requirements instead of vague performance wording."
            )

        if has_any(stage_keywords):
            requirements.append(
                "For stage, step, or process questions, list items in time order. For each item, name the stage first and then state its purpose. Do not merge adjacent stages into one item."
            )

        if has_any(first_second_stage_keywords):
            requirements.append(
                "If the question explicitly names the first stage and the second stage, answer only those named stages instead of expanding to other stages."
            )

        if has_any(deployment_keywords):
            requirements.append(
                "If the question asks about the model and deployment method, separate the answer into 'model or component' and 'deployment or communication mode'. If the deployment environment is not explicitly stated, say that clearly instead of inferring it."
            )

        if has_any(operations_keywords):
            requirements.append(
                "For operations or feature questions, first list the user-visible or role-specific actions explicitly supported by the system. Put backend implementation details such as method names or API names in a secondary note instead of letting them replace the main operation list."
            )

        if has_any(dataset_keywords):
            requirements.append(
                "For dataset or benchmark questions, separate test or evaluation datasets from training datasets. If some names appear only in auxiliary analysis or figures, label them as analysis-only instead of mixing them with the main evaluation answer."
            )
            requirements.append(
                "Answer dataset questions in this order: first the datasets or benchmarks explicitly used for test or evaluation, then training datasets if mentioned, and finally analysis-only datasets if they are relevant."
            )

        if has_any(formula_keywords):
            requirements.append(
                "For formula or objective questions, first list the explicit symbols or terms that appear in the equation or definition, such as epsilon, beta, pi_ref, KL term, clip term, or value model, and then briefly explain each role. Do not replace explicit symbols with vague categories or omit symbols that are present in the snippets."
            )

        if has_any(metric_definition_keywords):
            requirements.append(
                "For metric-definition questions, first state the conceptual quantity the metric is intended to measure, and only then explain how the paper operationalizes it, such as with pass@k or another computation."
            )
            requirements.append(
                "Do not equate a figure label, axis label, or chart caption with the metric's definition unless the snippets explicitly define them as the same thing. Prefer prose definition sentences over plot labels."
            )

        numbered = []
        for offset, requirement in enumerate(requirements):
            numbered.append(f"{start_index + offset}. {requirement}")
        return numbered

    @staticmethod
    def _chain_of_thought_prompt(query: str, contexts: List[Document]) -> str:
        context_str = PromptTemplateManager._format_contexts(contexts)
        return (
            "You are a careful document analyst.\n"
            "# Snippets\n"
            f"{context_str}\n\n"
            "# Question\n"
            f"{query}\n\n"
            "# Think Step by Step\n"
            "1. Restate what the question is asking.\n"
            "2. Identify which snippets contain the needed evidence.\n"
            "3. Extract the key facts.\n"
            "4. Give the final answer in the same language as the question.\n"
        )

    @staticmethod
    def _react_prompt(query: str, contexts: List[Document]) -> str:
        context_str = PromptTemplateManager._format_contexts(contexts)
        return (
            "You are a reasoning assistant using a simple ReAct style.\n"
            "# Snippets\n"
            f"{context_str}\n\n"
            "# Question\n"
            f"{query}\n\n"
            "Thought 1: What is the question really asking?\n"
            "Action 1: Identify the most relevant snippets.\n"
            "Observation 1: Summarize the useful evidence.\n"
            "Thought 2: What is still missing or uncertain?\n"
            "Action 2: Cross-check the snippets.\n"
            "Observation 2: Confirm the final evidence.\n"
            "Answer: Reply in the same language as the question and cite snippet numbers.\n"
        )

    @staticmethod
    def _few_shot_prompt(
        query: str,
        contexts: List[Document],
        examples: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        if examples is None:
            examples = [
                {
                    "query": "What is RAG?",
                    "context": "RAG combines retrieval and generation so the model can answer with grounded context.",
                    "answer": "RAG is retrieval-augmented generation. It retrieves relevant context first and then generates the answer from that context.",
                },
                {
                    "query": "What operations are supported?",
                    "context": "The system supports create, update, query, and delete for orders.",
                    "answer": "The supported operations are create, update, query, and delete.",
                },
            ]

        examples_str = "\n\n".join(
            [
                (
                    f"Example {i + 1}\n"
                    f"Question: {example['query']}\n"
                    f"Snippet: {example['context']}\n"
                    f"Answer: {example['answer']}"
                )
                for i, example in enumerate(examples)
            ]
        )
        context_str = PromptTemplateManager._format_contexts(contexts)
        return (
            "You are a careful document-grounded QA assistant.\n"
            "# Examples\n"
            f"{examples_str}\n\n"
            "# Snippets\n"
            f"{context_str}\n\n"
            "# Question\n"
            f"{query}\n\n"
            "# Answer\n"
        )

    @staticmethod
    def _format_contexts(contexts: List[Document]) -> str:
        if not contexts:
            return "[Snippet 1]\nSource: unknown - unknown chapter (page ?)\nContent: "

        return "\n\n".join(
            [
                (
                    f"[Snippet {i + 1}]\n"
                    f"Source: {doc.metadata.get('source', 'unknown')} - "
                    f"{doc.metadata.get('chapter', 'unknown chapter')} "
                    f"(page {doc.metadata.get('page', '?')})\n"
                    f"Content: {doc.page_content}"
                )
                for i, doc in enumerate(contexts)
            ]
        )


class SelfConsistencyVerifier:
    """Generate multiple answers and keep the most self-consistent one."""

    def __init__(self, llm, tokenizer):
        self.llm = llm
        self.tokenizer = tokenizer

    def generate_with_consistency(
        self,
        prompt: str,
        num_samples: int = 3,
        temperature: float = 0.7,
    ) -> str:
        print(f"[SelfConsistency] Generating {num_samples} candidates...")

        answers: List[str] = []
        for index in range(num_samples):
            print(f"[SelfConsistency] Candidate {index + 1}/{num_samples}")
            try:
                response, _ = self.llm.chat(
                    self.tokenizer,
                    prompt,
                    history=[],
                    do_sample=True,
                    temperature=temperature,
                    repetition_penalty=1.2,
                )
                answers.append(response)
            except Exception as exc:
                print(f"[SelfConsistency] Candidate generation failed: {exc}")

        if not answers:
            print("[SelfConsistency] No answer generated")
            return "Sorry, answer generation failed."
        if len(answers) == 1:
            return answers[0]

        print("[SelfConsistency] Selecting the most consistent answer...")
        return self._select_most_consistent(answers)

    def _select_most_consistent(self, answers: List[str]) -> str:
        scores = []
        for index, answer in enumerate(answers):
            similarity_sum = 0.0
            for other_index, other_answer in enumerate(answers):
                if index == other_index:
                    continue
                similarity_sum += SequenceMatcher(None, answer, other_answer).ratio()

            average_similarity = similarity_sum / (len(answers) - 1) if len(answers) > 1 else 0.0
            scores.append((answer, average_similarity))
            print(
                f"[SelfConsistency] Candidate {index + 1} average similarity: "
                f"{average_similarity:.3f}"
            )

        best_answer = max(scores, key=lambda item: item[1])[0]
        print("[SelfConsistency] Selected the most consistent answer")
        return best_answer

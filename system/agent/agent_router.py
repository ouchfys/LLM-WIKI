import json
import re
from typing import Any, Dict, List, Optional


class AgentRouter:
    def __init__(self, llm):
        self.llm = llm

    def decide(self, query: str, history: list) -> dict:
        query = (query or "").strip()
        recent_history = (history or [])[-3:]
        prompt = self._build_prompt(query, recent_history)

        try:
            raw_response = self.llm.invoke(prompt)
        except Exception as exc:
            print(f"[AgentRouter] LLM routing failed: {exc}")
            return {"action": "retrieve", "search_query": query}

        if hasattr(raw_response, "content"):
            raw_text = str(raw_response.content).strip()
        else:
            raw_text = str(raw_response or "").strip()

        parsed = self._parse_json_response(raw_text)
        if not parsed:
            return {"action": "retrieve", "search_query": query}

        action = str(parsed.get("action", "")).strip()
        if self._looks_like_paper_fact(query) and action != "retrieve":
            return {"action": "retrieve", "search_query": query}

        if action == "answer_from_history":
            if not recent_history:
                return {"action": "retrieve", "search_query": query}
            return {"action": "answer_from_history"}

        if action == "clarify":
            clarification = str(
                parsed.get("clarification") or self._default_clarification(query)
            ).strip()
            return {
                "action": "clarify",
                "clarification": clarification or self._default_clarification(query),
            }

        search_query = str(parsed.get("search_query") or query).strip()
        return {
            "action": "retrieve",
            "search_query": search_query or query,
        }

    def _build_prompt(self, query: str, history: list) -> str:
        formatted_history = self._format_history(history)
        return (
            "你是一个RAG系统的路由器。根据对话历史和用户问题，决定下一步动作。\n"
            "只输出JSON，不要输出解释、Markdown或多余文本。\n\n"
            f"对话历史（最近3轮）：\n{formatted_history}\n\n"
            f"用户问题：{query}\n\n"
            "可选输出：\n"
            '1. {"action": "answer_from_history"}\n'
            '2. {"action": "retrieve", "search_query": "改写后的独立检索问题"}\n'
            '3. {"action": "clarify", "clarification": "需要澄清的问题"}\n\n'
            "判断规则：\n"
            "- 只有当问题能直接从对话历史回答时，才选 answer_from_history。\n"
            "- 只要涉及论文事实、实验、方法、数据、结论等知识点，优先选 retrieve。\n"
            "- 指代不清、需要用户补充信息时，选 clarify。"
        )

    def _format_history(self, history: list) -> str:
        if not history:
            return "(无)"

        lines = []
        for user_query, assistant_response in history[-3:]:
            lines.append(f"User: {self._truncate(user_query)}")
            lines.append(f"Assistant: {self._truncate(assistant_response)}")
        return "\n".join(lines)

    @staticmethod
    def _truncate(text: Any, limit: int = 200) -> str:
        value = str(text or "").strip()
        if len(value) <= limit:
            return value
        return value[:limit]

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        text = text.strip()
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(1).strip()
        return text

    def _parse_json_response(self, text: str) -> Optional[Dict[str, Any]]:
        cleaned = self._strip_code_fences(text)
        start = cleaned.find("{")
        if start == -1:
            return None

        candidate = cleaned[start:]
        decoder = json.JSONDecoder()
        try:
            payload, _ = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            return None

        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _looks_like_paper_fact(query: str) -> bool:
        lowered = (query or "").lower()
        keywords = [
            "论文",
            "实验",
            "方法",
            "数据",
            "结果",
            "结论",
            "指标",
            "消融",
            "对比",
            "模型",
            "章节",
            "图",
            "表",
            "baseline",
            "dataset",
            "method",
            "experiment",
            "result",
        ]
        return any(keyword in lowered for keyword in keywords)

    @staticmethod
    def _default_clarification(query: str) -> str:
        base = query.strip() if query else "这个问题"
        return f"你能把“{base}”指得更具体一点吗？"

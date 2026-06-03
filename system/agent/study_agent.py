"""
StudyAgent — top-level intent router and workflow controller.

Classifies user queries into one of 8 intents and routes to the appropriate
workflow. Delegates to existing AgentRouter for paper_qa fine-grained routing.

Intent schema:
{
  "intent": "paper_qa | discover | wiki_lookup | build_note | interview | review | deep_ingest | clarify",
  "query": "standalone user goal",
  "source_policy": "primary_only | allow_secondary | user_provided",
  "needs_graphrag": false,
  "clarification": ""
}
"""

import json
import re
from typing import Any, Dict, List, Optional


ROUTING_PROMPT = """\
You are the router for a Personal Research Agent. Based on the user's query and context, decide the primary intent.

## Available Modes
- discover: User wants to find new papers, articles, or explore a topic.
- wiki_lookup: Answer can come from the user's saved wiki cards.
- paper_qa: User asks about an already-ingested paper (use GraphRAG deep reading).
- build_note: User wants to create or update a wiki card.
- interview: User wants mock interview practice or answer polishing.
- review: User wants to review weak points or past material.
- deep_ingest: User explicitly wants to ingest a paper for deep reading.
- clarify: User's intent is ambiguous, needs clarification.

## Context
Conversation history (recent): {history_summary}
Wiki cards available: {wiki_count}
Ingested papers: {ingested_papers}
User has learning goals: {has_goals}

## User Query
{query}

## Output
Return JSON only, no other text:
{{"intent": "...", "query": "reformulated standalone query", "source_policy": "primary_only | allow_secondary | user_provided", "needs_graphrag": false, "clarification": ""}}

## Routing Rules
- Use "discover" when the user asks for new papers, trends, reading lists, or related material.
- Use "wiki_lookup" when the answer can come from saved wiki cards.
- Use "paper_qa" when the user asks about already-ingested papers (facts, experiments, methods, data).
- Use "build_note" when the user wants to save, create, or update a note/wiki card.
- Use "interview" when the user asks for mock interview, practice questions, or answer feedback.
- Use "review" when the user asks about their weak points, progress, or review schedule.
- Use "deep_ingest" only after the user explicitly chooses a specific source to ingest.
- Use "clarify" when the user's intent is truly ambiguous.
- Set "needs_graphrag": true only for "paper_qa" and "deep_ingest".
- Set "source_policy" to "primary_only" for factual/technical queries, "allow_secondary" for exploration.
"""


class StudyAgent:
    """Top-level intent router that delegates to specialized workflows."""

    def __init__(
        self,
        llm,
        agent_router=None,
        wiki_store=None,
        discovery=None,
        interview_coach=None,
        learning_profile=None,
        ingestion_queue=None,
    ):
        self.llm = llm
        self.agent_router = agent_router
        self.wiki_store = wiki_store
        self.discovery = discovery
        self.interview_coach = interview_coach
        self.learning_profile = learning_profile
        self.ingestion_queue = ingestion_queue

    def decide(
        self,
        query: str,
        history: List = None,
        ingested_papers: List[str] = None,
    ) -> Dict[str, Any]:
        """Classify user intent and return a routing decision."""
        query = (query or "").strip()
        if not query:
            return {
                "intent": "clarify",
                "query": query,
                "source_policy": "user_provided",
                "needs_graphrag": False,
                "clarification": "What would you like to do?",
            }

        # Quick-rule detection for performance (bypass LLM for obvious patterns)
        quick = self._quick_rules(query)
        if quick:
            return quick

        # Build context
        wiki_count = 0
        if self.wiki_store:
            wiki_count = len(self.wiki_store.get_recent_cards(limit=100))

        papers = ingested_papers or []
        has_goals = False
        if self.learning_profile:
            goals = self.learning_profile.get_goals()
            has_goals = bool(goals)

        history_summary = self._summarize_history(history or [])

        prompt = ROUTING_PROMPT.format(
            history_summary=history_summary,
            wiki_count=wiki_count,
            ingested_papers=", ".join(papers) if papers else "None",
            has_goals="Yes" if has_goals else "No",
            query=query,
        )

        try:
            raw = self.llm.invoke(prompt)
        except Exception as exc:
            print(f"[StudyAgent] LLM routing failed: {exc}")
            return {
                "intent": "paper_qa",
                "query": query,
                "source_policy": "primary_only",
                "needs_graphrag": True,
                "clarification": "",
            }

        parsed = self._parse_json(raw)
        if not parsed:
            return {
                "intent": "paper_qa",
                "query": query,
                "source_policy": "primary_only",
                "needs_graphrag": True,
                "clarification": "",
            }

        intent = parsed.get("intent", "paper_qa")
        if intent not in {
            "paper_qa", "discover", "wiki_lookup", "build_note",
            "interview", "review", "deep_ingest", "clarify",
        }:
            intent = "paper_qa"

        return {
            "intent": intent,
            "query": parsed.get("query", query),
            "source_policy": parsed.get("source_policy", "primary_only"),
            "needs_graphrag": parsed.get("needs_graphrag", intent in ("paper_qa", "deep_ingest")),
            "clarification": parsed.get("clarification", ""),
        }

    def execute(self, intent_decision: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the appropriate workflow based on intent.

        Returns a dict with suggested action for the UI:
        {"action": "switch_tab", "tab": "discovery", "message": "..."}
        """
        intent = intent_decision.get("intent", "paper_qa")

        tab_map = {
            "discover": "Discovery",
            "wiki_lookup": "Wiki",
            "build_note": "Wiki",
            "paper_qa": "Deep Reading",
            "deep_ingest": "Deep Reading",
            "interview": "Interview",
            "review": "Profile",
            "clarify": None,
        }

        suggested_tab = tab_map.get(intent)
        clarification = intent_decision.get("clarification", "")

        return {
            "action": "switch_tab" if suggested_tab else "clarify",
            "tab": suggested_tab,
            "message": clarification or f"Routing to {suggested_tab} mode.",
            "needs_graphrag": intent_decision.get("needs_graphrag", False),
        }

    def _quick_rules(self, query: str) -> Optional[Dict[str, Any]]:
        """Fast keyword-based routing to avoid LLM calls for obvious intents."""
        lowered = query.lower()

        interview_keywords = [
            "mock interview", "interview me", "ask me a question",
            "practice interview", "模拟面试", "面试练习", "问我问题",
            "面试题", "面试问题",
        ]
        if any(kw in lowered for kw in interview_keywords):
            return {
                "intent": "interview",
                "query": query,
                "source_policy": "allow_secondary",
                "needs_graphrag": False,
                "clarification": "",
            }

        discover_keywords = [
            "find papers", "search for", "discover", "what papers",
            "latest research", "trending", "recommend papers",
            "找论文", "搜索", "有什么论文", "推荐论文", "发现",
        ]
        if any(kw in lowered for kw in discover_keywords):
            return {
                "intent": "discover",
                "query": query,
                "source_policy": "allow_secondary",
                "needs_graphrag": False,
                "clarification": "",
            }

        note_keywords = [
            "save this", "create a note", "add to wiki", "build a card",
            "write this down", "make a note", "保存", "记下来", "创建卡片",
        ]
        if any(kw in lowered for kw in note_keywords):
            return {
                "intent": "build_note",
                "query": query,
                "source_policy": "user_provided",
                "needs_graphrag": False,
                "clarification": "",
            }

        ingest_keywords = [
            "ingest this paper", "deep read", "add this paper",
            "import this pdf", "ingest this pdf",
            "深度阅读", "导入这篇", "摄入这篇",
        ]
        if any(kw in lowered for kw in ingest_keywords):
            return {
                "intent": "deep_ingest",
                "query": query,
                "source_policy": "user_provided",
                "needs_graphrag": True,
                "clarification": "",
            }

        review_keywords = [
            "my progress", "what are my weak points", "review my learning",
            "what do i need to review", "我的进展", "我的弱点", "复习计划",
        ]
        if any(kw in lowered for kw in review_keywords):
            return {
                "intent": "review",
                "query": query,
                "source_policy": "user_provided",
                "needs_graphrag": False,
                "clarification": "",
            }

        # Paper-related queries default to paper_qa (handled by AgentRouter)
        paper_keywords = [
            "论文", "实验", "方法", "数据", "结果", "结论", "指标",
            "消融", "对比", "模型", "章节", "图表", "公式",
            "baseline", "dataset", "method", "experiment", "result",
            "paper", "equation", "metric", "ablation",
        ]
        if any(kw in lowered for kw in paper_keywords):
            return {
                "intent": "paper_qa",
                "query": query,
                "source_policy": "primary_only",
                "needs_graphrag": True,
                "clarification": "",
            }

        return None

    @staticmethod
    def _summarize_history(history: List) -> str:
        if not history:
            return "(no history)"
        lines = []
        for item in history[-3:]:
            if isinstance(item, tuple) and len(item) >= 2:
                lines.append(f"User: {str(item[0])[:100]}")
                lines.append(f"Assistant: {str(item[1])[:100]}")
            elif isinstance(item, dict):
                lines.append(str(item.get("question", item.get("query", "")))[:100])
        return "\n".join(lines) if lines else "(no history)"

    @staticmethod
    def _parse_json(text: Any) -> Optional[Dict[str, Any]]:
        text = str(text or "").strip()
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
        if match:
            text = match.group(1).strip()
        start = text.find("{")
        if start == -1:
            return None
        candidate = text[start:]
        decoder = json.JSONDecoder()
        try:
            payload, _ = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

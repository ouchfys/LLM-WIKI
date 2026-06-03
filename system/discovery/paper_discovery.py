"""
Paper Discovery — orchestrate search across adapters and rank by user goal.

Search → normalize → classify → LLM-rank by relevance to user's goal.
"""

import json
import re
from typing import Any, Dict, List, Optional

from system.discovery.source_adapters import SourceAdapter, SourceItem


RANKING_PROMPT = """\
You are a research relevance evaluator. Given a user's learning goal and a list of papers/articles, rate each item's relevance on a scale of 1-10.

User goal: {user_goal}

Items:
{items_text}

Return a JSON object mapping item IDs to scores:
{{"item_id_1": 8, "item_id_2": 6, ...}}

Scoring guidelines:
- 9-10: Directly core to the goal, must-read
- 7-8: Highly relevant, very useful
- 5-6: Somewhat relevant, useful background
- 3-4: Tangentially related
- 1-2: Mostly irrelevant

Only return JSON, no other text.
"""


class PaperDiscovery:
    """Orchestrate search across adapters and rank results by user goal."""

    def __init__(self, llm, adapters: List[SourceAdapter], classifier=None):
        self.llm = llm
        self.adapters = adapters
        self.classifier = classifier

    def discover(
        self,
        query: str,
        user_goal: str = "",
        limit: int = 10,
    ) -> List[SourceItem]:
        """Search across all adapters, normalize, classify, rank."""
        query = (query or "").strip()
        if not query:
            return []

        # Search
        all_items: List[SourceItem] = []
        seen_ids = set()

        for adapter in self.adapters:
            try:
                items = adapter.search(query, limit=limit)
                for item in items:
                    if item.id not in seen_ids:
                        seen_ids.add(item.id)
                        all_items.append(item)
            except Exception as e:
                print(f"[PaperDiscovery] Adapter {type(adapter).__name__} failed: {e}")

        if not all_items:
            return []

        # Classify
        if self.classifier:
            all_items = self.classifier.classify_batch(all_items)

        # Rank by goal
        if user_goal and len(all_items) > 1:
            all_items = self._rank_by_goal(all_items, user_goal)

        # Sort: by relevance_score descending, then by citation_count descending
        all_items.sort(
            key=lambda item: (item.relevance_score, item.citation_count or 0),
            reverse=True,
        )

        return all_items[:limit]

    def _rank_by_goal(
        self,
        items: List[SourceItem],
        user_goal: str,
    ) -> List[SourceItem]:
        """Use LLM to score each item's relevance to the user's learning goal."""
        items_text_parts = []
        for i, item in enumerate(items):
            parts = [f"ID: {item.id}"]
            parts.append(f"Title: {item.title}")
            parts.append(f"Summary: {item.summary[:300]}")
            if item.authors:
                parts.append(f"Authors: {', '.join(item.authors[:3])}")
            if item.year:
                parts.append(f"Year: {item.year}")
            if item.venue:
                parts.append(f"Venue: {item.venue}")
            items_text_parts.append("\n".join(parts))

        items_text = "\n\n---\n\n".join(items_text_parts)
        prompt = RANKING_PROMPT.format(user_goal=user_goal, items_text=items_text)

        try:
            raw = self.llm.invoke(prompt)
        except Exception as exc:
            print(f"[PaperDiscovery] LLM ranking failed: {exc}")
            return items

        scores = self._parse_ranking(raw)
        if not scores:
            return items

        for item in items:
            score = scores.get(item.id)
            if score is not None:
                item.relevance_score = float(score)

        return items

    @staticmethod
    def _parse_ranking(text: Any) -> Dict[str, float]:
        text = str(text or "").strip()
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
        if match:
            text = match.group(1).strip()

        start = text.find("{")
        if start == -1:
            return {}

        candidate = text[start:]
        decoder = json.JSONDecoder()
        try:
            payload, _ = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            return {}

        if not isinstance(payload, dict):
            return {}

        result = {}
        for key, value in payload.items():
            try:
                result[str(key)] = float(value)
            except (ValueError, TypeError):
                pass
        return result

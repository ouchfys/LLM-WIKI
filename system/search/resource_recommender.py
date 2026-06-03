"""Learning resource recommender for missing Wiki knowledge.

It uses the web search adapter to suggest sources that can later be captured
into the private Wiki.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import List

from system.search.web_search import WebSearchTool


@dataclass
class LearningResource:
    category: str
    title: str
    url: str
    snippet: str = ""


class LearningResourceRecommender:
    def __init__(self, web_search: WebSearchTool | None = None):
        self.web_search = web_search

    @property
    def available(self) -> bool:
        return bool(self.web_search and self.web_search.available)

    def recommend(self, query: str, limit_per_category: int = 2) -> List[LearningResource]:
        if not self.available or not (query or "").strip():
            return []

        search_plan = [
            ("paper", self._paper_query(query)),
            ("video", self._video_query(query)),
            ("interview_post", self._post_query(query)),
        ]

        grouped: dict[str, list[LearningResource]] = {}
        seen_urls = set()

        with ThreadPoolExecutor(max_workers=len(search_plan)) as executor:
            future_to_category = {
                executor.submit(self.web_search.search, search_query, limit_per_category): category
                for category, search_query in search_plan
            }
            for future in as_completed(future_to_category):
                category = future_to_category[future]
                try:
                    items = future.result()
                except Exception:
                    items = []
                grouped[category] = [
                    LearningResource(
                        category=category,
                        title=item.title,
                        url=item.url,
                        snippet=item.snippet,
                    )
                    for item in items
                    if item.url
                ]

        resources: List[LearningResource] = []
        for category, _ in search_plan:
            for item in grouped.get(category, []):
                if item.url in seen_urls:
                    continue
                seen_urls.add(item.url)
                resources.append(item)
        return resources

    @staticmethod
    def _paper_query(query: str) -> str:
        normalized = LearningResourceRecommender._normalize_agent_query(query)
        return f"{normalized} arxiv paper survey"

    @staticmethod
    def _video_query(query: str) -> str:
        normalized = LearningResourceRecommender._normalize_agent_query(query)
        return f"site:bilibili.com {normalized} 教程"

    @staticmethod
    def _post_query(query: str) -> str:
        normalized = LearningResourceRecommender._normalize_agent_query(query)
        return f"{normalized} 小红书 面经 博客"

    @staticmethod
    def _normalize_agent_query(query: str) -> str:
        text = (query or "").lower()
        if "react" in text and "plan" in text and "execute" in text:
            return "ReAct Plan-and-Execute AI Agent"
        return query.strip()

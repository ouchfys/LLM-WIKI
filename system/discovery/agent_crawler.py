"""Autonomous discovery crawler for papers and public technical feeds."""

from dataclasses import dataclass
import time
from typing import Any, Dict, Iterable, List

from system.discovery.paper_discovery import PaperDiscovery
from system.discovery.source_adapters import ArxivAdapter, RssFeedAdapter, SourceItem
from system.discovery.source_classifier import SourceClassifier
from system.recommender.item_scorer import ProfileAwareRecommender


DEFAULT_FEEDS = [
    {
        "name": "Hugging Face Blog",
        "url": "https://huggingface.co/blog/feed.xml",
        "source_level": "secondary",
    },
    {
        "name": "Google Research Blog",
        "url": "https://research.google/blog/rss/",
        "source_level": "secondary",
    },
    {
        "name": "OpenAI Blog",
        "url": "https://openai.com/news/rss.xml",
        "source_level": "secondary",
    },
]

FALLBACK_QUERIES = [
    "retrieval augmented generation",
    "LLM agents memory",
    "RAG evaluation benchmark",
    "multimodal document understanding",
]


@dataclass
class CrawlReport:
    created: int
    discovered: int
    ranked: int
    queries: List[str]
    sources: List[str]


class AgentCrawler:
    """Pull fresh public sources and push ranked items into Monthly Reads."""

    def __init__(
        self,
        reading_store,
        learning_profile=None,
        profile_builder=None,
        recommender=None,
        llm=None,
        feeds: List[Dict[str, str]] = None,
    ):
        self.reading_store = reading_store
        self.learning_profile = learning_profile
        self.feeds = feeds or DEFAULT_FEEDS
        self.classifier = SourceClassifier()
        self.recommender = recommender or ProfileAwareRecommender(
            learning_profile=learning_profile,
            profile_builder=profile_builder,
        )
        self.paper_discovery = PaperDiscovery(
            llm=llm,
            adapters=[ArxivAdapter(sort_by="submittedDate")],
            classifier=self.classifier,
        )
        self.feed_adapter = RssFeedAdapter(self.feeds)

    def crawl_to_monthly_reads(
        self,
        topics: Iterable[str] = None,
        include_arxiv: bool = True,
        include_feeds: bool = True,
        per_query_limit: int = 8,
        max_items: int = 20,
        arxiv_interval_seconds: float = 3.2,
    ) -> CrawlReport:
        queries = self.build_queries(topics)
        goal_text = self._profile_goal_text()
        discovered: List[SourceItem] = []
        seen = set()
        last_arxiv_at = 0.0

        for query in queries:
            if include_arxiv:
                elapsed = time.monotonic() - last_arxiv_at
                if last_arxiv_at and elapsed < arxiv_interval_seconds:
                    time.sleep(arxiv_interval_seconds - elapsed)
                for item in self.paper_discovery.discover(query=query, user_goal=goal_text, limit=per_query_limit):
                    if item.id not in seen:
                        seen.add(item.id)
                        discovered.append(item)
                last_arxiv_at = time.monotonic()

            if include_feeds:
                for item in self.feed_adapter.search(query=query, limit=per_query_limit):
                    item.source_level = self.classifier.classify(item)
                    if item.id not in seen:
                        seen.add(item.id)
                        discovered.append(item)

        ranked = self.recommender.rank_sources(
            discovered,
            query=" ".join(queries),
            user_goal=goal_text,
            limit=max_items,
        )

        created = 0
        month = self.reading_store.current_month()
        for result in ranked:
            item = result.item
            existed = self.reading_store.find_existing(
                source_id=self._get(item, "id", ""),
                url=self._get(item, "url", ""),
                month=month,
            )
            self.reading_store.add_source_item(
                item,
                month=month,
                score=result.score,
                reasons=self._localize_reasons(result.reasons),
            )
            if not existed:
                created += 1

        return CrawlReport(
            created=created,
            discovered=len(discovered),
            ranked=len(ranked),
            queries=queries,
            sources=self.source_names(include_arxiv=include_arxiv, include_feeds=include_feeds),
        )

    def build_queries(self, topics: Iterable[str] = None) -> List[str]:
        candidates: List[str] = []
        for topic in topics or []:
            if topic and topic.strip():
                candidates.append(topic.strip())

        if self.learning_profile:
            for goal in self.learning_profile.get_goals():
                candidates.extend(self._expand_goal(goal))
            for signal in self.learning_profile.get_profile_signals(limit=80):
                if signal.get("signal_type") in {"interest", "weak_point", "goal"}:
                    candidates.append(signal.get("value", ""))

        if not candidates:
            candidates = FALLBACK_QUERIES

        expanded: List[str] = []
        for item in candidates:
            item = item.strip()
            if not item:
                continue
            expanded.append(item)
            lower = item.lower()
            if "survey" not in lower and "综述" not in item:
                expanded.append(f"{item} survey")
            if "benchmark" not in lower and "评测" not in item:
                expanded.append(f"{item} benchmark")
        return self._dedupe(expanded)[:6]

    def source_names(self, include_arxiv: bool, include_feeds: bool) -> List[str]:
        names = []
        if include_arxiv:
            names.append("arXiv submittedDate")
        if include_feeds:
            names.extend(feed.get("name") or feed.get("url", "") for feed in self.feeds)
        return names

    def _profile_goal_text(self) -> str:
        if not self.learning_profile:
            return ""
        parts = []
        parts.extend(self.learning_profile.get_goals())
        for signal in self.learning_profile.get_profile_signals(limit=80):
            if signal.get("signal_type") in {"interest", "weak_point", "preference"}:
                parts.append(signal.get("value", ""))
        return " ".join(part for part in parts if part)

    @staticmethod
    def _expand_goal(goal: str) -> List[str]:
        goal = (goal or "").strip()
        if not goal:
            return []
        result = [goal]
        if "面试" in goal or "interview" in goal.lower():
            result.extend(["RAG evaluation", "LLM agents", "agent memory", "reranking retrieval"])
        if "项目" in goal or "project" in goal.lower():
            result.extend(["retrieval augmented generation", "GraphRAG", "multimodal RAG"])
        return result

    @staticmethod
    def _localize_reasons(reasons: List[str]) -> List[str]:
        mapping = {
            "matches search topic": "匹配本次抓取主题",
            "matches learning goal": "匹配学习目标",
            "patches weak point": "覆盖薄弱点",
            "fits long-term interest": "符合长期兴趣",
            "similar to ignored topics": "接近已忽略主题",
            "primary source": "一手来源",
            "secondary source": "二手解读来源",
            "discovery relevance": "发现阶段相关性",
            "interview-useful": "适合面试追问",
            "general relevance": "通用相关性",
        }
        localized = []
        for reason in reasons:
            text = reason
            for key, value in mapping.items():
                text = text.replace(key, value)
            localized.append(text)
        return localized

    @staticmethod
    def _dedupe(items: Iterable[str]) -> List[str]:
        seen = set()
        result = []
        for item in items:
            key = item.lower().strip()
            if key and key not in seen:
                seen.add(key)
                result.append(item)
        return result

    @staticmethod
    def _get(item: Any, key: str, default: Any = None) -> Any:
        if isinstance(item, dict):
            return item.get(key, default)
        return getattr(item, key, default)

"""Cold-start helpers for Monthly Reads."""

from typing import Iterable, List


DEFAULT_TOPICS = {
    "interview": [
        "RAG evaluation benchmark",
        "GraphRAG retrieval augmented generation",
        "agent memory survey",
        "reranking retrieval augmented generation",
    ],
    "paper_reading": [
        "retrieval augmented generation survey",
        "large language model agents survey",
        "LLM evaluation benchmark",
    ],
    "project": [
        "GraphRAG system",
        "RAG benchmark",
        "knowledge graph retrieval augmented generation",
    ],
    "course": [
        "transformer attention survey",
        "retrieval augmented generation",
        "large language model evaluation",
    ],
}


class ColdStartSeeder:
    def __init__(self, discovery, recommender, reading_store, learning_profile=None):
        self.discovery = discovery
        self.recommender = recommender
        self.reading_store = reading_store
        self.learning_profile = learning_profile

    def seed_monthly_reads(
        self,
        goal: str,
        interests: Iterable[str],
        level: str = "",
        source_style: str = "",
        per_query_limit: int = 5,
        max_items: int = 12,
    ) -> int:
        interests = [item.strip() for item in interests if item and item.strip()]
        self._save_profile(goal, interests, level, source_style)

        queries = self.build_seed_queries(goal, interests)
        all_results = []
        seen = set()
        for query in queries:
            results = self.discovery.discover(
                query=query,
                user_goal=self._goal_text(goal, interests, level, source_style),
                limit=per_query_limit,
            )
            for item in results:
                if item.id not in seen:
                    seen.add(item.id)
                    all_results.append(item)

        ranked = self.recommender.rank_sources(
            all_results,
            query=" ".join(interests),
            user_goal=self._goal_text(goal, interests, level, source_style),
            limit=max_items,
        )

        count = 0
        for result in ranked:
            self.reading_store.add_source_item(
                result.item,
                score=result.score,
                reasons=result.reasons,
            )
            count += 1
        return count

    @staticmethod
    def build_seed_queries(goal: str, interests: List[str]) -> List[str]:
        goal_l = (goal or "").lower()
        if "interview" in goal_l or "面试" in goal_l:
            base = DEFAULT_TOPICS["interview"]
        elif "project" in goal_l or "项目" in goal_l:
            base = DEFAULT_TOPICS["project"]
        elif "course" in goal_l or "考试" in goal_l or "课程" in goal_l:
            base = DEFAULT_TOPICS["course"]
        else:
            base = DEFAULT_TOPICS["paper_reading"]

        queries = list(base)
        for interest in interests[:5]:
            queries.append(interest)
            queries.append(f"{interest} survey")
            queries.append(f"{interest} benchmark")
        return _dedupe(queries)[:10]

    def _save_profile(
        self,
        goal: str,
        interests: List[str],
        level: str,
        source_style: str,
    ) -> None:
        if not self.learning_profile:
            return
        if goal:
            self.learning_profile.set_goal(goal)
            self.learning_profile.upsert_signal(
                "goal", "learning_goal", goal, weight=2.0, evidence="Cold start", source="monthly_reads"
            )
        if level:
            self.learning_profile.upsert_signal(
                "preference", "level", level, weight=1.0, evidence="Cold start", source="monthly_reads"
            )
        if source_style:
            self.learning_profile.upsert_signal(
                "preference", "source_style", source_style, weight=1.0, evidence="Cold start", source="monthly_reads"
            )
        for interest in interests:
            self.learning_profile.upsert_signal(
                "interest", "topic", interest, weight=1.5, evidence="Cold start", source="monthly_reads"
            )

    @staticmethod
    def _goal_text(goal: str, interests: List[str], level: str, source_style: str) -> str:
        parts = [goal or "", " ".join(interests), level or "", source_style or ""]
        return " ".join(part for part in parts if part).strip()


def _dedupe(items: Iterable[str]) -> List[str]:
    seen = set()
    result = []
    for item in items:
        key = item.lower().strip()
        if key and key not in seen:
            seen.add(key)
            result.append(item)
    return result


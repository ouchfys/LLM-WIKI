"""
Profile-aware lightweight recommendation.

This is intentionally content-based and rule-reranked. It is suitable for a
single-user learning agent where collaborative filtering data does not exist.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Tuple


SOURCE_QUALITY_SCORE = {
    "primary": 2.0,
    "secondary": 1.0,
    "tertiary": -1.0,
}

FEEDBACK_WEIGHTS = {
    "saved_to_wiki": 1.5,
    "deep_read_requested": 2.0,
    "opened": 0.5,
    "reviewed": 0.8,
    "marked_useful": 1.2,
    "ignored": -1.5,
    "marked_not_useful": -2.0,
}


@dataclass
class RecommendationResult:
    item: Any
    score: float
    reasons: List[str] = field(default_factory=list)


class ProfileAwareRecommender:
    def __init__(self, learning_profile=None, profile_builder=None):
        self.learning_profile = learning_profile
        self.profile_builder = profile_builder

    def rank_sources(
        self,
        items: Iterable[Any],
        query: str = "",
        user_goal: str = "",
        limit: int = 10,
    ) -> List[RecommendationResult]:
        profile = self._load_profile()
        results = [
            self._score_item(item, query=query, user_goal=user_goal, profile=profile)
            for item in items
        ]
        results.sort(key=lambda result: result.score, reverse=True)
        return results[:limit]

    def record_feedback(
        self,
        item_id: str,
        item_type: str,
        action: str,
        reason: str = "",
        metadata: dict = None,
    ) -> None:
        if not self.learning_profile:
            return
        self.learning_profile.log_recommendation_feedback(
            item_id=item_id,
            item_type=item_type,
            action=action,
            reason=reason,
            metadata=metadata or {},
        )

        title = str((metadata or {}).get("title", "")).strip()
        topics = self._tokens(title + " " + str((metadata or {}).get("summary", "")))
        weight = FEEDBACK_WEIGHTS.get(action, 0.0)
        if weight == 0:
            return
        for topic in topics[:8]:
            self.learning_profile.upsert_signal(
                "interest" if weight > 0 else "negative_interest",
                "topic",
                topic,
                weight=abs(weight),
                evidence=reason or action,
                source="recommendation_feedback",
            )

    def _score_item(
        self,
        item: Any,
        query: str,
        user_goal: str,
        profile: Dict[str, Any],
    ) -> RecommendationResult:
        text = self._item_text(item)
        tokens = self._tokens(text)
        token_set = set(tokens)
        score = 0.0
        reasons: List[str] = []

        query_terms = set(self._tokens(query))
        if query_terms:
            overlap = len(query_terms & token_set)
            if overlap:
                delta = min(overlap * 1.2, 4.0)
                score += delta
                reasons.append(f"matches search topic (+{delta:.1f})")

        goal_terms = set(self._tokens(user_goal or " ".join(profile["goals"])))
        if goal_terms:
            overlap = len(goal_terms & token_set)
            if overlap:
                delta = min(overlap * 1.0, 3.0)
                score += delta
                reasons.append(f"matches learning goal (+{delta:.1f})")

        weak_terms = set(self._tokens(" ".join(profile["weak_points"])))
        if weak_terms:
            overlap = len(weak_terms & token_set)
            if overlap:
                delta = min(overlap * 1.5, 4.5)
                score += delta
                reasons.append(f"patches weak point (+{delta:.1f})")

        interest_terms = set(self._tokens(" ".join(profile["interests"])))
        if interest_terms:
            overlap = len(interest_terms & token_set)
            if overlap:
                delta = min(overlap * 0.8, 3.0)
                score += delta
                reasons.append(f"fits long-term interest (+{delta:.1f})")

        negative_terms = set(self._tokens(" ".join(profile.get("negative_interests", []))))
        if negative_terms:
            overlap = len(negative_terms & token_set)
            if overlap:
                delta = min(overlap * 1.0, 3.0)
                score -= delta
                reasons.append(f"similar to ignored topics (-{delta:.1f})")

        # Recent topics — what the user has been engaging with recently
        recent_topic_terms = set(self._tokens(" ".join(profile.get("recent_topics", []))))
        if recent_topic_terms:
            overlap = len(recent_topic_terms & token_set)
            if overlap:
                delta = min(overlap * 0.9, 3.5)
                score += delta
                reasons.append(f"related to recent activity (+{delta:.1f})")

        # Recent episodes — dampened signal from concrete actions
        recent_ep_text = " ".join(profile.get("recent_episodes", []))
        recent_ep_terms = set(self._tokens(recent_ep_text))
        if recent_ep_terms:
            overlap = len(recent_ep_terms & token_set)
            if overlap >= 2:
                delta = min(overlap * 0.6, 2.5)
                score += delta
                reasons.append(f"echoes recent interactions (+{delta:.1f})")

        # Preference-aware source bias
        prefs = profile.get("preferences", {})
        if prefs.get("language_preference") == "中文":
            if self._looks_chinese_domain(item):
                score += 1.0
                reasons.append("Chinese source preferred (+1.0)")

        source_level = self._get(item, "source_level", "")
        quality = SOURCE_QUALITY_SCORE.get(source_level, 0.0)
        if quality:
            score += quality
            reasons.append(f"{source_level} source ({quality:+.1f})")

        base_relevance = float(self._get(item, "relevance_score", 0) or 0)
        if base_relevance:
            delta = min(base_relevance / 2.0, 5.0)
            score += delta
            reasons.append(f"discovery relevance {base_relevance:.0f}/10 (+{delta:.1f})")

        if self._looks_interview_useful(text):
            score += 1.0
            reasons.append("interview-useful (+1.0)")

        if not reasons:
            reasons.append("general relevance")

        return RecommendationResult(item=item, score=score, reasons=reasons)

    def _load_profile(self) -> Dict[str, Any]:
        if self.profile_builder:
            snapshot = self.profile_builder.build()
            return {
                "goals": snapshot.goals,
                "weak_points": snapshot.weak_points,
                "interests": snapshot.interests,
                "negative_interests": snapshot.negative_interests,
                "recent_topics": snapshot.recent_topics,
                "recent_episodes": snapshot.recent_episodes,
                "preferences": snapshot.preferences,
            }

        # Fallback (legacy path)
        profile = {
            "goals": [],
            "weak_points": [],
            "interests": [],
            "negative_interests": [],
            "recent_topics": [],
            "recent_episodes": [],
            "preferences": {},
        }
        if not self.learning_profile:
            return profile

        profile["goals"] = self.learning_profile.get_goals()
        profile["weak_points"] = [item["topic"] for item in self.learning_profile.get_weak_points(min_events=1)]
        for signal in self.learning_profile.get_profile_signals(limit=100):
            if signal["signal_type"] == "interest":
                profile["interests"].append(signal["value"])
            elif signal["signal_type"] == "negative_interest":
                profile["negative_interests"].append(signal["value"])
            elif signal["signal_type"] == "weak_point":
                profile["weak_points"].append(signal["value"])
            elif signal["signal_type"] == "goal":
                profile["goals"].append(signal["value"])
        return profile

    @staticmethod
    def _item_text(item: Any) -> str:
        parts = [
            str(ProfileAwareRecommender._get(item, "title", "")),
            str(ProfileAwareRecommender._get(item, "summary", "")),
            str(ProfileAwareRecommender._get(item, "venue", "")),
        ]
        authors = ProfileAwareRecommender._get(item, "authors", [])
        if authors:
            parts.append(" ".join(authors))
        return " ".join(parts)

    @staticmethod
    def _get(item: Any, key: str, default: Any = None) -> Any:
        if isinstance(item, dict):
            return item.get(key, default)
        return getattr(item, key, default)

    @staticmethod
    def _tokens(text: str) -> List[str]:
        text = (text or "").lower()
        tokens = re.findall(r"[a-z0-9][a-z0-9_\-]{1,}", text)
        for block in re.findall(r"[\u4e00-\u9fff]{2,}", text):
            tokens.append(block)
            tokens.extend(block[i:i + 2] for i in range(len(block) - 1))
        stop = {"the", "and", "for", "with", "from", "this", "that", "into", "using", "about"}
        return [token for token in tokens if token not in stop]

    @staticmethod
    def _looks_chinese_domain(item: Any) -> bool:
        url = str(ProfileAwareRecommender._get(item, "url", ""))
        cn_domains = {"zhihu.com", "csdn.net", "juejin.cn", "jiqizhixin.com",
                       "mp.weixin.qq.com", "sspai.com", "xiaohongshu.com"}
        for domain in cn_domains:
            if domain in url:
                return True
        venue = str(ProfileAwareRecommender._get(item, "venue", "")).lower()
        return any(word in venue for word in ("中国", "chinese", "中文"))

    @staticmethod
    def _looks_interview_useful(text: str) -> bool:
        lowered = (text or "").lower()
        keywords = [
            "evaluation",
            "benchmark",
            "survey",
            "compare",
            "comparison",
            "method",
            "architecture",
            "rag",
            "agent",
            "面试",
            "八股",
            "评估",
            "对比",
        ]
        return any(keyword in lowered for keyword in keywords)


"""
Profile Builder — build a normalized snapshot from all memory layers.

This is the single entry point for understanding the user's current state.
It reads stable profile signals, episodic traces, preferences, and feedback
to produce one reusable snapshot consumed by scoring, recommendation, and
prompt building.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class ProfileSnapshot:
    goals: List[str] = field(default_factory=list)
    interests: List[str] = field(default_factory=list)
    weak_points: List[str] = field(default_factory=list)
    preferences: Dict[str, str] = field(default_factory=dict)
    recent_topics: List[str] = field(default_factory=list)
    recent_episodes: List[str] = field(default_factory=list)
    negative_interests: List[str] = field(default_factory=list)
    built_at: str = ""
    source_counts: Dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "goals": self.goals,
            "interests": self.interests,
            "weak_points": self.weak_points,
            "preferences": self.preferences,
            "recent_topics": self.recent_topics,
            "recent_episodes": self.recent_episodes,
            "negative_interests": self.negative_interests,
            "built_at": self.built_at,
        }

    def is_empty(self) -> bool:
        return not (
            self.goals or self.interests or self.weak_points
            or self.preferences or self.recent_topics or self.recent_episodes
        )


class ProfileBuilder:
    """Assemble a clean profile snapshot from all memory stores."""

    def __init__(
        self,
        learning_profile=None,
        session_store=None,
        max_recent_topics: int = 12,
        max_recent_episodes: int = 6,
    ):
        self.learning_profile = learning_profile
        self.session_store = session_store
        self.max_recent_topics = max_recent_topics
        self.max_recent_episodes = max_recent_episodes

    def build(self) -> ProfileSnapshot:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        snapshot = ProfileSnapshot(built_at=now)

        if not self.learning_profile:
            return snapshot

        signals = self.learning_profile.get_profile_signals(limit=200)
        feedback = self.learning_profile.get_recommendation_feedback(limit=100)
        events = self.learning_profile.get_recent_events(limit=50)

        # Goals
        snapshot.goals = self._collect_goals(signals)

        # Interests
        snapshot.interests = self._collect_by_type(signals, "interest")[:10]

        # Weak points
        snapshot.weak_points = self._collect_by_type(signals, "weak_point")[:8]

        # Negative interests (topics user ignored/dismissed)
        snapshot.negative_interests = self._collect_by_type(signals, "negative_interest")[:8]

        # Stable preferences
        snapshot.preferences = self._collect_preferences()

        # Recent topics from feedback and events
        snapshot.recent_topics = self._collect_recent_topics(feedback, events)[:self.max_recent_topics]

        # Recent episodes from feedback actions
        snapshot.recent_episodes = self._collect_recent_episodes(feedback, events)[:self.max_recent_episodes]

        # Source counts for diagnostics
        snapshot.source_counts = {
            "signals_used": len(signals),
            "feedback_used": len(feedback),
            "events_used": len(events),
        }

        return snapshot

    # ----------------------------------------------------------------
    #  Internal collectors
    # ----------------------------------------------------------------

    def _collect_goals(self, signals: List[Dict[str, Any]]) -> List[str]:
        goals: List[str] = []
        if self.learning_profile:
            goals = self.learning_profile.get_goals()
        for signal in signals:
            if signal.get("signal_type") == "goal":
                goals.append(signal["value"])
        return list(dict.fromkeys(goals))[:5]

    @staticmethod
    def _collect_by_type(signals: List[Dict[str, Any]], signal_type: str) -> List[str]:
        items: List[str] = []
        for signal in signals:
            if signal.get("signal_type") == signal_type:
                value = str(signal.get("value", "")).strip()
                if value:
                    items.append(value)
        return list(dict.fromkeys(items))

    def _collect_preferences(self) -> Dict[str, str]:
        prefs: Dict[str, str] = {}
        if self.session_store:
            raw = self.session_store.get_all_preferences()
            preferred_keys = {
                "language_preference", "answer_length", "answer_style",
                "detail_level", "citation_preference", "explanation_style",
            }
            for key, value in raw.items():
                if key in preferred_keys and value:
                    prefs[key] = value
        return prefs

    def _collect_recent_topics(
        self,
        feedback: List[Dict[str, Any]],
        events: List[Dict[str, Any]],
    ) -> List[str]:
        topics: List[str] = []

        # feedback and events are already DESC (newest first), take first N
        for item in feedback[:30]:
            meta = item.get("metadata", {}) or {}
            title = str(meta.get("title", "")).strip()
            if title:
                topics.append(title)

        for event in events[:20]:
            topic = str(event.get("topic", "")).strip()
            if topic:
                topics.append(topic)

        return list(dict.fromkeys(topics))

    def _collect_recent_episodes(
        self,
        feedback: List[Dict[str, Any]],
        events: List[Dict[str, Any]],
    ) -> List[str]:
        episodes: List[str] = []
        action_labels = {
            "saved_to_wiki": "saved a wiki card",
            "deep_read_requested": "requested deep reading",
            "reviewed": "marked as read",
            "marked_useful": "found useful",
        }

        for item in feedback[:20]:
            action = item.get("action", "")
            if action in {"ignored", "marked_not_useful"}:
                continue
            meta = item.get("metadata", {}) or {}
            title = str(meta.get("title", "")).strip()
            label = action_labels.get(action, action)
            if title and label:
                episodes.append(f"{label}: {title}")
            elif title:
                episodes.append(title)

        for event in events[:15]:
            event_type = event.get("event_type", "")
            if event_type == "interview_answer" and event.get("score"):
                topic = str(event.get("topic", ""))[:40]
                score = event.get("score", 0)
                episodes.append(f"interviewed on {topic} (score {score:.0f}/10)")

        return list(dict.fromkeys(episodes))

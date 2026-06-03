from typing import List

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from backend.deps import get_learning_profile, get_session_store
from system.memory.learning_profile import LearningProfileStore
from system.memory.session_store import SessionStore


router = APIRouter()


class SignalPayload(BaseModel):
    signal_type: str
    key: str = "topic"
    value: str
    weight: float = 1.0
    evidence: str = "web UI"
    source: str = "frontend"


class GoalsPayload(BaseModel):
    goals: List[str]


class OnboardingPayload(BaseModel):
    target_role: str = ""
    learning_goal: str = ""
    level: str = ""
    interests: List[str] = Field(default_factory=list)
    weak_points: List[str] = Field(default_factory=list)
    source_preferences: List[str] = Field(default_factory=list)
    monthly_reading_target: int = 8


@router.get("")
def get_profile(
    store: SessionStore = Depends(get_session_store),
    profile: LearningProfileStore = Depends(get_learning_profile),
):
    return {
        "preferences": store.get_all_preferences(),
        "onboarded": store.get_preference("onboarded") == "true",
        "goals": profile.get_goals(),
        "signals": profile.get_profile_signals(limit=100),
        "weak_points": profile.get_weak_points(min_events=1),
        "mastered_topics": profile.get_mastered_topics(min_events=1),
        "feedback": profile.get_recommendation_feedback(limit=30),
    }


@router.post("/goals")
def save_goals(
    payload: GoalsPayload,
    profile: LearningProfileStore = Depends(get_learning_profile),
):
    text = "\n".join(goal.strip() for goal in payload.goals if goal.strip())
    profile.set_goal(text)
    for goal in payload.goals:
        goal = goal.strip()
        if goal:
            profile.upsert_signal("goal", "learning_goal", goal, weight=2.0, evidence="web UI", source="frontend")
    return {"ok": True}


@router.post("/onboarding")
def save_onboarding(
    payload: OnboardingPayload,
    store: SessionStore = Depends(get_session_store),
    profile: LearningProfileStore = Depends(get_learning_profile),
):
    target_role = payload.target_role.strip()
    learning_goal = payload.learning_goal.strip()
    level = payload.level.strip()
    monthly_target = str(max(1, min(payload.monthly_reading_target, 60)))

    preferences = {
        "onboarded": "true",
        "target_role": target_role,
        "learning_level": level,
        "source_preferences": "\n".join(_clean_list(payload.source_preferences)),
        "monthly_reading_target": monthly_target,
    }
    for key, value in preferences.items():
        store.upsert_preference(key, value, evidence="onboarding")

    goals = []
    if target_role:
        goals.append(f"目标岗位：{target_role}")
    if learning_goal:
        goals.append(learning_goal)
    if goals:
        profile.set_goal("\n".join(goals))

    for goal in goals:
        profile.upsert_signal("goal", "learning_goal", goal, weight=2.0, evidence="onboarding", source="onboarding")
    if level:
        profile.upsert_signal("preference", "level", level, weight=1.0, evidence="onboarding", source="onboarding")
    for source in _clean_list(payload.source_preferences):
        profile.upsert_signal("preference", "source_style", source, weight=1.0, evidence="onboarding", source="onboarding")
    for interest in _clean_list(payload.interests):
        profile.upsert_signal("interest", "topic", interest, weight=1.8, evidence="onboarding", source="onboarding")
    for weak_point in _clean_list(payload.weak_points):
        profile.upsert_signal("weak_point", "topic", weak_point, weight=1.8, evidence="onboarding", source="onboarding")

    return {"ok": True}


@router.post("/signals")
def add_signal(
    payload: SignalPayload,
    profile: LearningProfileStore = Depends(get_learning_profile),
):
    profile.upsert_signal(
        payload.signal_type,
        payload.key,
        payload.value,
        weight=payload.weight,
        evidence=payload.evidence,
        source=payload.source,
    )
    return {"ok": True}


def _clean_list(items: List[str]) -> List[str]:
    result = []
    seen = set()
    for item in items:
        value = item.strip()
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return result

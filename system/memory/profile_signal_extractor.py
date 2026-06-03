"""Heuristic extraction of durable profile signals from user chat turns."""

from __future__ import annotations

import re
from typing import Any, Dict, List


class ProfileSignalExtractor:
    GENERIC_STOPWORDS = {
        "这个", "那个", "什么", "怎么", "如何", "为什么", "哪些", "一下", "帮我", "总结",
        "整理", "基于", "我的", "里面", "相关", "内容", "问题", "一下子", "一下吧", "继续",
        "wiki", "copilot", "资料", "笔记", "回答", "解释", "推荐", "给我", "看看", "想问",
        "可以", "关于", "用于", "项目", "面试", "论文", "阅读", "知识库",
        "我对", "不太熟", "还不太熟", "尤其是", "这块",
    }

    GOAL_RULES = [
        ("面试准备", ("面试", "八股", "mock interview", "interview")),
        ("项目表达", ("项目", "简历", "项目介绍", "项目表达", "resume")),
        ("推荐阅读", ("推荐", "读什么", "下一步", "阅读计划", "该看什么")),
        ("论文精读", ("精读", "deep read", "深入读", "仔细读")),
    ]

    def extract(
        self,
        message: str,
        cards: List[Dict[str, Any]] | None = None,
    ) -> Dict[str, List[Dict[str, str]]]:
        cards = cards or []
        signals: List[Dict[str, str]] = []
        preferences: List[Dict[str, str]] = []
        episodes: List[Dict[str, str]] = []

        lowered = (message or "").lower()
        candidate_topics = self._candidate_topics(message, cards)

        for goal, hints in self.GOAL_RULES:
            if any(hint in lowered for hint in hints):
                signals.append({"signal_type": "goal", "key": "learning_goal", "value": goal, "weight": "1.6"})

        if any(word in lowered for word in ("不懂", "不会", "不熟", "不太熟", "不太懂", "薄弱", "搞不懂", "忘了", "说不清", "weak")):
            for topic in candidate_topics[:3]:
                signals.append({"signal_type": "weak_point", "key": "topic", "value": topic, "weight": "1.8"})

        if any(word in lowered for word in ("想学", "关注", "想看", "多看看", "深入", "继续聊", "推荐我")):
            for topic in candidate_topics[:3]:
                signals.append({"signal_type": "interest", "key": "topic", "value": topic, "weight": "1.2"})

        preference_rules = [
            ("language_preference", "中文", ("中文回答", "中文", "用中文")),
            ("language_preference", "英文", ("英文回答", "英文", "english")),
            ("answer_length", "short", ("简短", "短一点", "精炼", "直接说结论")),
            ("answer_length", "long", ("详细", "展开", "讲细一点", "详细一点")),
            ("answer_style", "conclusion_first", ("结论先行", "先说结论", "先给结论")),
            ("explanation_style", "technical", ("技术一点", "专业一点", "深入一点")),
            ("explanation_style", "simplified", ("通俗", "简单一点", "白话一点")),
            ("citation_preference", "inline", ("带引用", "给出处", "标注来源")),
        ]
        for key, value, hints in preference_rules:
            if any(hint in lowered for hint in hints):
                preferences.append({"key": key, "value": value})

        for card in cards[:3]:
            title = (card.get("title") or "").strip()
            if not title:
                continue
            episodes.append({
                "topic": title,
                "detail": self._truncate(message, 160),
                "paper": title if card.get("page_type") == "PaperPage" else "",
            })

        return {
            "signals": self._dedupe_items(signals),
            "preferences": self._dedupe_items(preferences),
            "episodes": self._dedupe_items(episodes),
        }

    def _candidate_topics(self, message: str, cards: List[Dict[str, Any]]) -> List[str]:
        candidates: List[str] = []
        candidates.extend(self._extract_terms(message))
        for card in cards[:3]:
            candidates.extend(card.get("related_topics") or [])
            candidates.extend(self._extract_terms(card.get("title", "")))

        result: List[str] = []
        seen = set()
        for item in candidates:
            topic = self._normalize_topic(item)
            if not topic or topic in seen:
                continue
            seen.add(topic)
            result.append(topic)
        return result

    def _extract_terms(self, text: str) -> List[str]:
        text = text or ""
        terms = re.findall(r"[A-Za-z][A-Za-z0-9_\-]{1,}|[\u4e00-\u9fff]{2,12}", text)
        return [term.strip() for term in terms if term.strip()]

    def _normalize_topic(self, value: str) -> str:
        value = re.sub(r"[《》\"'“”‘’（）()，,。:：/\\\[\]{}]+", " ", value or "")
        value = re.sub(r"\s+", " ", value).strip()
        if not value:
            return ""
        if len(value) > 24:
            return ""
        lower = value.lower()
        if lower in self.GENERIC_STOPWORDS or value in self.GENERIC_STOPWORDS:
            return ""
        if value.endswith("总结") and len(value) <= 4:
            return ""
        return value

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        text = " ".join((text or "").split())
        return text[:limit]

    @staticmethod
    def _dedupe_items(items: List[Dict[str, str]]) -> List[Dict[str, str]]:
        seen = set()
        result = []
        for item in items:
            key = tuple(sorted(item.items()))
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

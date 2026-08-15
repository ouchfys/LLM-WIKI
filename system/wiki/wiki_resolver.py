"""Deterministic, explainable resolution from a user query to Wiki pages.

The resolver is deliberately page-first.  It searches compiled Wiki metadata
and aliases; it does not search raw paper chunks.  The returned candidates are
small enough for the tool-use controller to inspect without receiving the full
Wiki catalog.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional


_EN_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_+.-]{1,}")
_CJK_BLOCK_RE = re.compile(r"[\u3400-\u9fff]{2,}")

_QUERY_STOPWORDS = {
    "and", "or", "the", "for", "with", "from", "what", "how", "why",
    "are", "is", "was", "were", "this", "that", "about", "compare",
    "什么", "怎么", "如何", "为什么", "区别", "不同", "对比", "比较",
    "介绍", "解释", "一下", "哪些", "相关", "内容", "总结", "帮我",
}


@dataclass
class WikiResolution:
    card_id: str
    title: str
    page_type: str
    summary: str
    markdown_path: str
    score: float
    match_reasons: List[str] = field(default_factory=list)
    matched_alias: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "card_id": self.card_id,
            "title": self.title,
            "page_type": self.page_type,
            "summary": self.summary,
            "markdown_path": self.markdown_path,
            "score": round(self.score, 3),
            "match_reason": ", ".join(self.match_reasons),
            "match_reasons": list(self.match_reasons),
            "matched_alias": self.matched_alias,
        }


class WikiResolver:
    """Resolve a query to a small, ranked set of compiled Wiki pages."""

    def __init__(self, wiki_store, max_scan_pages: int = 2000):
        self.wiki_store = wiki_store
        self.max_scan_pages = max(50, int(max_scan_pages))

    def resolve(
        self,
        query: str,
        limit: int = 6,
        page_types: Optional[Iterable[str]] = None,
    ) -> List[Dict[str, Any]]:
        query = (query or "").strip()
        if not query:
            return []

        allowed_types = {str(item).strip() for item in (page_types or []) if str(item).strip()}
        cards = self.wiki_store.list_cards(limit=self.max_scan_pages)
        if allowed_types:
            cards = [card for card in cards if card.get("page_type") in allowed_types]
        if not cards:
            return []

        aliases_by_card = self._aliases_by_card()
        fts_rank = self._fts_rank(query, max(limit * 4, 20))
        query_normalized = normalize_lookup(query)
        query_terms = lookup_terms(query)

        resolved: List[WikiResolution] = []
        for card in cards:
            card_id = str(card.get("id") or "")
            title = str(card.get("title") or "")
            aliases = aliases_by_card.get(card_id, [])
            score, reasons, matched_alias = self._score_card(
                query_normalized=query_normalized,
                query_terms=query_terms,
                card=card,
                aliases=aliases,
                fts_position=fts_rank.get(card_id),
            )
            if score <= 0:
                continue
            resolved.append(WikiResolution(
                card_id=card_id,
                title=title,
                page_type=str(card.get("page_type") or ""),
                summary=str(card.get("summary") or ""),
                markdown_path=str(card.get("markdown_path") or ""),
                score=score,
                match_reasons=reasons,
                matched_alias=matched_alias,
            ))

        resolved.sort(key=lambda item: (-item.score, normalize_lookup(item.title), item.card_id))
        return [item.to_dict() for item in resolved[: max(1, min(int(limit), 20))]]

    def _aliases_by_card(self) -> Dict[str, List[str]]:
        try:
            rows = self.wiki_store.list_aliases_for_resolution()
        except (AttributeError, RuntimeError):
            return {}
        result: Dict[str, List[str]] = {}
        for row in rows:
            card_id = str(row.get("card_id") or "")
            alias = str(row.get("alias") or "").strip()
            if card_id and alias:
                result.setdefault(card_id, []).append(alias)
        return result

    def _fts_rank(self, query: str, limit: int) -> Dict[str, int]:
        try:
            cards = self.wiki_store.search_cards(query, limit=limit)
        except Exception:
            return {}
        return {
            str(card.get("id") or ""): index
            for index, card in enumerate(cards)
            if card.get("id")
        }

    @staticmethod
    def _score_card(
        query_normalized: str,
        query_terms: List[str],
        card: Dict[str, Any],
        aliases: List[str],
        fts_position: Optional[int],
    ) -> tuple[float, List[str], str]:
        card_id = str(card.get("id") or "")
        title = str(card.get("title") or "")
        title_normalized = normalize_lookup(title)
        alias_pairs = [(alias, normalize_lookup(alias)) for alias in aliases if normalize_lookup(alias)]
        reasons: List[str] = []
        matched_alias = ""
        score = 0.0

        if query_normalized and query_normalized == normalize_lookup(card_id):
            score = 130.0
            reasons.append("card_id_exact")
        title_exact = bool(query_normalized and query_normalized == title_normalized)
        if title_exact:
            score = max(score, 120.0)
            reasons.append("title_exact")

        alias_exact = False
        for alias, normalized_alias in alias_pairs:
            if query_normalized == normalized_alias:
                score = max(score, 116.0)
                reasons.append("alias_exact")
                matched_alias = alias
                alias_exact = True
                break

        if not title_exact and _safe_phrase(title_normalized) and title_normalized in query_normalized:
            score = max(score, 104.0)
            reasons.append("title_mentioned")
        elif _safe_phrase(query_normalized) and query_normalized in title_normalized:
            score = max(score, 90.0)
            reasons.append("title_contains_query")

        if not alias_exact:
            for alias, normalized_alias in alias_pairs:
                if _safe_phrase(normalized_alias) and normalized_alias in query_normalized:
                    score = max(score, 100.0)
                    reasons.append("alias_mentioned")
                    matched_alias = alias
                    break

        if fts_position is not None:
            score = max(score, 76.0 - min(fts_position, 20) * 1.5)
            reasons.append("page_fts")

        searchable = " ".join([
            title,
            " ".join(aliases),
            str(card.get("summary") or ""),
            " ".join(str(item) for item in (card.get("related_topics") or [])),
            _compact_content(card.get("content_json")),
        ])
        searchable_terms = set(lookup_terms(searchable))
        if query_terms and searchable_terms:
            matched = [term for term in query_terms if term in searchable_terms]
            if matched:
                coverage = len(matched) / max(len(set(query_terms)), 1)
                score = max(score, 35.0 + coverage * 45.0)
                reasons.append(f"term_overlap:{len(matched)}/{len(set(query_terms))}")

        return score, _dedupe(reasons), matched_alias


def normalize_lookup(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", value)


def lookup_terms(value: str) -> List[str]:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    terms: List[str] = []
    for token in _EN_TOKEN_RE.findall(text):
        token = token.casefold()
        if token not in _QUERY_STOPWORDS:
            terms.append(token)
    for block in _CJK_BLOCK_RE.findall(text):
        if block in _QUERY_STOPWORDS:
            continue
        if len(block) <= 3:
            terms.append(block)
            continue
        # Character bigrams make Chinese lookup robust to natural question
        # suffixes without requiring a process-wide segmentation dependency.
        terms.extend(block[index:index + 2] for index in range(len(block) - 1))
    return _dedupe(terms)[:64]


def _safe_phrase(value: str) -> bool:
    if not value:
        return False
    if re.search(r"[\u3400-\u9fff]", value):
        return len(value) >= 2
    return len(value) >= 3


def _compact_content(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value[:4000]
    try:
        return json.dumps(value, ensure_ascii=False)[:4000]
    except (TypeError, ValueError):
        return str(value)[:4000]


def _dedupe(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result

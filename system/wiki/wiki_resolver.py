"""Explainable page resolver using section FTS, multilingual vectors and RRF."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable, Optional

from system.wiki.wiki_search_index import WikiSearchIndex


class WikiResolver:
    """Retrieve a small catalog; the chat model performs the final page choice."""

    def __init__(
        self,
        wiki_store: Any,
        max_scan_pages: int = 2000,
        embedder: Any = None,
        rrf_k: int = 60,
    ):
        # max_scan_pages remains accepted for old callers, but is deliberately
        # unused: resolution no longer loads the full card catalog.
        self.wiki_store = wiki_store
        self.embedder = embedder
        self.rrf_k = max(1, int(rrf_k))
        search_index = getattr(wiki_store, "search_index", None)
        self.search_index: WikiSearchIndex = search_index or WikiSearchIndex(
            db_path=getattr(wiki_store, "db_path", None)
        )
        if embedder is not None:
            self.search_index.embedder = embedder

    def resolve(
        self,
        query: str,
        limit: int = 6,
        page_types: Optional[Iterable[str]] = None,
    ) -> list[dict[str, Any]]:
        query = (query or "").strip()
        if not query:
            return []
        allowed_types = [str(item).strip() for item in (page_types or []) if str(item).strip()]
        candidate_limit = max(24, min(int(limit) * 6, 80))

        # These three recall paths have no data dependency. Execute them in one
        # bounded read layer, then fuse in the stable order below.
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="wiki-recall") as pool:
            exact_future = pool.submit(self._exact_candidates, query, candidate_limit)
            lexical_future = pool.submit(
                self.search_index.search_fts, query, candidate_limit, allowed_types,
            )
            semantic_future = pool.submit(
                self.search_index.search_vector, query, candidate_limit, allowed_types,
            )
            exact = exact_future.result()
            lexical = lexical_future.result()
            try:
                semantic = semantic_future.result()
            except Exception:
                # Vector search is an optional recall path. Keyword resolution
                # stays available during API outages or embedding backfills.
                semantic = []

        scores: dict[str, float] = defaultdict(float)
        reasons: dict[str, list[str]] = defaultdict(list)
        sections: dict[str, list[dict[str, str]]] = defaultdict(list)
        matched_alias: dict[str, str] = {}

        for rank, card in enumerate(exact, start=1):
            page_id = str(card.get("id") or "")
            if not page_id or (allowed_types and card.get("page_type") not in allowed_types):
                continue
            normalized_query = normalize_lookup(query)
            if normalized_query == normalize_lookup(page_id):
                reason, weight = "card_id_exact", 4.0
            elif normalized_query == normalize_lookup(card.get("title", "")):
                reason, weight = "title_exact", 3.5
            else:
                alias = str(card.get("_resolution_alias") or query)
                if normalized_query == normalize_lookup(alias):
                    reason, weight = "alias_exact", 3.2
                else:
                    reason, weight = "alias_mentioned", 3.0
                matched_alias[page_id] = alias
            scores[page_id] += weight / (self.rrf_k + rank)
            reasons[page_id].append(reason)

        self._merge_ranked(lexical, "section_fts", scores, reasons, sections, weight=1.0)
        self._merge_ranked(semantic, "multilingual_vector", scores, reasons, sections, weight=1.0)

        ranked_ids = sorted(scores, key=lambda page_id: (-scores[page_id], page_id))
        cards = self.wiki_store.get_cards_by_ids(ranked_ids[: max(1, min(int(limit), 20))])
        by_id = {card["id"]: card for card in cards}
        results: list[dict[str, Any]] = []
        for page_id in ranked_ids:
            card = by_id.get(page_id)
            if not card:
                continue
            matched_sections = _unique_section_hits(sections.get(page_id, []))[:3]
            results.append({
                "card_id": page_id,
                "title": card.get("title", ""),
                "page_type": card.get("page_type", ""),
                "summary": card.get("summary", ""),
                "markdown_path": card.get("markdown_path", ""),
                "score": round(scores[page_id] * 2000, 3),
                "match_reason": ", ".join(_unique(reasons[page_id])),
                "match_reasons": _unique(reasons[page_id]),
                "matched_alias": matched_alias.get(page_id, ""),
                "matched_sections": matched_sections,
            })
            if len(results) >= max(1, min(int(limit), 20)):
                break
        return results

    def _exact_candidates(self, query: str, limit: int) -> list[dict[str, Any]]:
        try:
            return self.wiki_store.find_exact_resolution_candidates(
                query,
                normalize_alias(query),
                limit=limit,
            )
        except (AttributeError, RuntimeError):
            card = self.wiki_store.get_card(query)
            return [card] if card else []

    def _merge_ranked(
        self,
        rows: list[dict[str, Any]],
        route: str,
        scores: dict[str, float],
        reasons: dict[str, list[str]],
        sections: dict[str, list[dict[str, str]]],
        weight: float,
    ) -> None:
        # RRF operates at page level. Multiple matching sections add evidence
        # snippets, but do not unfairly count as multiple independent pages.
        seen_pages: set[str] = set()
        for raw_rank, row in enumerate(rows, start=1):
            page_id = str(row.get("page_id") or "")
            if not page_id:
                continue
            sections[page_id].append({
                "section": str(row.get("section") or ""),
                "snippet": str(row.get("snippet") or "")[:260],
                "route": route,
            })
            if page_id in seen_pages:
                continue
            seen_pages.add(page_id)
            scores[page_id] += weight / (self.rrf_k + raw_rank)
            reasons[page_id].append(route)


def normalize_lookup(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", value)


def normalize_alias(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    value = re.sub(r"[\u2010-\u2015_+-]+", " ", value)
    value = re.sub(r"[^0-9a-z\u3400-\u9fff]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def lookup_terms(value: str) -> list[str]:
    """Compatibility helper exposing the terms used by section FTS."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    terms = re.findall(r"[a-z][a-z0-9_.+-]{1,}", text)
    for block in re.findall(r"[\u3400-\u9fff]{2,}", text):
        if len(block) <= 3:
            terms.append(block)
        terms.extend(block[index:index + 2] for index in range(len(block) - 1))
    return _unique(terms)[:64]


def _unique(values: Iterable[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            output.append(value)
    return output


def _unique_section_hits(values: list[dict[str, str]]) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        key = value.get("section", "")
        if key not in seen:
            seen.add(key)
            output.append(value)
    return output

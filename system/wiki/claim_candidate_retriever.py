"""Hybrid retrieval for old Claims that may relate to an incoming Claim."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from system.wiki.paper_pipeline.models import CandidateClaim
from system.wiki.wiki_resolver import normalize_alias


class ClaimCandidateRetriever:
    """Recall comparable Claims without treating similarity as a relation.

    Exact entity/card matches, Claim-level FTS and Claim-level embeddings are
    fused with RRF. The returned candidates still require relation judgment by
    ``ClaimRelationResolver``; retrieval alone never creates a conflict.
    """

    def __init__(self, wiki_store: Any, *, embedder: Any = None, rrf_k: int = 60):
        self.wiki_store = wiki_store
        self.search_index = wiki_store.search_index
        if embedder is not None:
            self.search_index.embedder = embedder
        self.rrf_k = max(1, int(rrf_k))

    def retrieve(
        self,
        *,
        page_title: str,
        incoming: CandidateClaim,
        local_claims: list[dict[str, Any]] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        query = self._query(page_title, incoming)
        candidate_limit = max(30, min(limit * 4, 100))

        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="claim-recall") as pool:
            exact_future = pool.submit(
                self._exact_cards, page_title, incoming.subject, candidate_limit
            )
            lexical_future = pool.submit(
                self.search_index.search_fts,
                query,
                candidate_limit,
                None,
                ["claim"],
            )
            semantic_future = pool.submit(
                self.search_index.search_vector,
                query,
                candidate_limit,
                None,
                ["claim"],
            )
            exact_cards = exact_future.result()
            lexical = lexical_future.result()
            try:
                semantic = semantic_future.result()
            except Exception:
                semantic = []

        page_ids = {
            str(item.get("page_id") or "")
            for item in [*lexical, *semantic]
            if str(item.get("page_id") or "")
        }
        page_ids.update(str(card.get("id") or "") for card in exact_cards)
        cards = self.wiki_store.get_cards_by_ids(sorted(page_ids))
        by_claim_id: dict[str, dict[str, Any]] = {}
        claim_ids_by_page: dict[str, list[str]] = defaultdict(list)
        for card in cards:
            content = card.get("content_json") if isinstance(card.get("content_json"), dict) else {}
            for raw in content.get("claims") or []:
                if not isinstance(raw, dict):
                    continue
                claim_id = str(raw.get("id") or "")
                if not claim_id or str(raw.get("status") or "") == "removed":
                    continue
                item = dict(raw)
                item["_page_id"] = str(card.get("id") or "")
                item["_page_title"] = str(card.get("title") or "")
                by_claim_id[claim_id] = item
                claim_ids_by_page[item["_page_id"]].append(claim_id)

        scores: dict[str, float] = defaultdict(float)
        routes: dict[str, list[str]] = defaultdict(list)

        local = [item for item in (local_claims or []) if isinstance(item, dict)]
        local.sort(key=lambda item: (-_rough_relevance(query, item), str(item.get("id") or "")))
        for rank, raw in enumerate(local, start=1):
            claim_id = str(raw.get("id") or "")
            if not claim_id:
                continue
            item = dict(raw)
            item.setdefault("_page_title", page_title)
            item.setdefault("_page_id", "")
            by_claim_id[claim_id] = item
            scores[claim_id] += 4.0 / (self.rrf_k + rank)
            routes[claim_id].append("same_page")

        exact_claims: list[tuple[float, str]] = []
        for card in exact_cards:
            for claim_id in claim_ids_by_page.get(str(card.get("id") or ""), []):
                exact_claims.append((_rough_relevance(query, by_claim_id[claim_id]), claim_id))
        exact_claims.sort(key=lambda item: (-item[0], item[1]))
        for rank, (_, claim_id) in enumerate(exact_claims, start=1):
            scores[claim_id] += 3.2 / (self.rrf_k + rank)
            routes[claim_id].append("entity_or_alias_exact")

        self._merge_ranked(lexical, "claim_fts", 1.0, by_claim_id, scores, routes)
        self._merge_ranked(semantic, "claim_vector", 1.0, by_claim_id, scores, routes)

        ranked_ids = sorted(scores, key=lambda claim_id: (-scores[claim_id], claim_id))
        output: list[dict[str, Any]] = []
        for claim_id in ranked_ids:
            claim = by_claim_id.get(claim_id)
            if not claim:
                continue
            item = dict(claim)
            item["_retrieval_score"] = round(scores[claim_id], 8)
            item["_retrieval_routes"] = _unique(routes[claim_id])
            output.append(item)
            if len(output) >= limit:
                break
        return output

    def _exact_cards(
        self, page_title: str, subject: str, limit: int
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        for term in _unique([subject.strip(), page_title.strip()]):
            if not term:
                continue
            try:
                cards = self.wiki_store.find_exact_resolution_candidates(
                    term, normalize_alias(term), limit=limit
                )
            except (AttributeError, RuntimeError):
                cards = []
            for card in cards:
                page_id = str(card.get("id") or "")
                if page_id and page_id not in seen:
                    seen.add(page_id)
                    output.append(card)
        return output

    @staticmethod
    def _query(page_title: str, incoming: CandidateClaim) -> str:
        scope = " ".join(
            f"{key} {value}" for key, value in incoming.scope.items()
            if str(value).strip()
        )
        return "\n".join(part for part in (
            incoming.subject or page_title,
            incoming.aspect,
            incoming.predicate,
            incoming.value,
            scope,
            incoming.claim,
        ) if part).strip()

    def _merge_ranked(
        self,
        rows: list[dict[str, Any]],
        route: str,
        weight: float,
        claims: dict[str, dict[str, Any]],
        scores: dict[str, float],
        routes: dict[str, list[str]],
    ) -> None:
        seen: set[str] = set()
        for rank, row in enumerate(rows, start=1):
            claim_id = str(row.get("claim_id") or "")
            if not claim_id or claim_id in seen or claim_id not in claims:
                continue
            seen.add(claim_id)
            scores[claim_id] += weight / (self.rrf_k + rank)
            routes[claim_id].append(route)


def _rough_relevance(query: str, claim: dict[str, Any]) -> float:
    left = set(_tokens(query))
    right = set(_tokens(" ".join(
        str(claim.get(key) or "")
        for key in ("subject", "aspect", "predicate", "value", "statement")
    )))
    return len(left & right) / max(len(left | right), 1)


def _tokens(value: str) -> list[str]:
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.findall(r"[a-z][a-z0-9_.+-]{1,}|\d+(?:\.\d+)?|[\u3400-\u9fff]", value)


def _unique(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            output.append(text)
    return output

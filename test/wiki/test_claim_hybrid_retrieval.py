from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from system.wiki.claim_candidate_retriever import ClaimCandidateRetriever
from system.wiki.claim_relation_resolver import ClaimRelationResolver
from system.wiki.hierarchical_compiler import HierarchicalWikiCompiler
from system.wiki.paper_pipeline.models import (
    CandidateClaim,
    DistilledCandidate,
    SourcePacket,
)
from system.wiki.wiki_search_index import WikiSearchIndex
from system.wiki.wiki_store import WikiStore


class FakeClaimEmbedder:
    model = "fake-claim-embedding"

    @staticmethod
    def _vector(text: str) -> list[float]:
        lowered = text.casefold()
        if "critic" in lowered or "value network" in lowered or "grpo" in lowered:
            return [1.0, 0.0, 0.0]
        return [0.0, 1.0, 0.0]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


def _insert_card(db_path: str, card: dict) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO wiki_pages
               (id, title, page_type, summary, content_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                card["id"], card["title"], card["page_type"], card["summary"],
                json.dumps(card["content_json"], ensure_ascii=False),
                "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
            ),
        )
        conn.commit()


def test_claim_index_backfill_and_hybrid_recall_find_cross_page_claim() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db_path = str(Path(tmp) / "claims.db")
        store = WikiStore(db_path=db_path)
        card = {
            "id": "card-old",
            "title": "Group Relative Policy Optimization",
            "page_type": "MethodPage",
            "summary": "GRPO training mechanism.",
            "related_topics": ["GRPO"],
            "content_json": {
                "aliases": ["GRPO", "组相对策略优化"],
                "claims": [{
                    "id": "claim-old",
                    "statement": "GRPO requires an independently trained critic model.",
                    "subject": "GRPO",
                    "aspect": "critic requirement",
                    "predicate": "requires",
                    "value": "true",
                    "scope": {"algorithm": "GRPO"},
                    "status": "supported",
                }],
            },
        }
        _insert_card(db_path, card)

        # Re-opening the derived index upgrades an older database and creates
        # one searchable unit per persisted Claim.
        index = WikiSearchIndex(db_path=db_path, embedder=FakeClaimEmbedder())
        index.backfill_embeddings(limit=20)
        store.search_index = index

        incoming = CandidateClaim(
            claim="GRPO does not need a separate Critic.",
            evidence="GRPO removes the need for a separate critic model.",
            subject="GRPO",
            aspect="critic requirement",
            predicate="does not require",
            value="false",
            scope={"algorithm": "GRPO"},
        )
        recalled = ClaimCandidateRetriever(
            store, embedder=FakeClaimEmbedder()
        ).retrieve(page_title="GRPO training", incoming=incoming, limit=20)

        old = next(item for item in recalled if item["id"] == "claim-old")
        assert "claim_fts" in old["_retrieval_routes"]
        assert "claim_vector" in old["_retrieval_routes"]
        assert old["_page_id"] == "card-old"


def test_cross_page_conflict_is_preserved_for_review() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db_path = str(Path(tmp) / "claims.db")
        store = WikiStore(db_path=db_path)
        card = {
            "id": "card-old",
            "title": "GRPO",
            "page_type": "MethodPage",
            "summary": "GRPO training mechanism.",
            "related_topics": [],
            "content_json": {
                "aliases": ["Group Relative Policy Optimization"],
                "claims": [{
                    "id": "claim-old",
                    "statement": "GRPO requires an independently trained critic model.",
                    "subject": "GRPO",
                    "aspect": "critic requirement",
                    "predicate": "requires",
                    "value": "true",
                    "scope": {"algorithm": "GRPO"},
                    "status": "supported",
                }],
            },
        }
        _insert_card(db_path, card)
        store.search_index = WikiSearchIndex(db_path=db_path)
        retriever = ClaimCandidateRetriever(store)
        resolver = ClaimRelationResolver(retriever=retriever)
        incoming = CandidateClaim(
            claim="GRPO does not require an independently trained critic model.",
            evidence="GRPO removes the need for a critic model.",
            subject="GRPO",
            aspect="critic requirement",
            predicate="does not require",
            value="false",
            scope={"algorithm": "GRPO"},
        )

        decision = resolver.resolve(
            page_title="Policy optimization without a value network",
            incoming_claims=[incoming],
            existing_claims=[],
        )[0]
        assert decision.relation == "contradicts"
        assert decision.existing_claim_id == "claim-old"
        assert decision.existing_page_id == "card-old"
        assert decision.requires_review is True

        candidate = DistilledCandidate(
            id="candidate-new",
            source_packet_id="packet-new",
            candidate_type="concept_card",
            page_type="ConceptPage",
            title="Policy optimization without a value network",
            claims=[incoming],
        )
        compiled = HierarchicalWikiCompiler().compile_claims(
            content_json={},
            existing_claims=[],
            candidate=candidate,
            packet=SourcePacket(source_id="packet-new", title="New paper"),
            relation_decisions=[decision],
        )
        affected = compiled.affected_claims[0]
        claim = compiled.content_json["claims"][0]
        assert affected["action"] == "challenge_claim"
        assert affected["compared_claim_id"] == "claim-old"
        assert affected["requires_review"] is True
        assert claim["conflicts_with_claim_id"] == "claim-old"
        assert claim["compared_page_id"] == "card-old"


def test_relation_resolver_does_not_stop_at_a_higher_ranked_non_conflict() -> None:
    incoming = CandidateClaim(
        claim="GRPO does not require an independently trained critic model.",
        evidence="GRPO removes the need for a critic model.",
        subject="GRPO",
        aspect="critic requirement",
        predicate="does not require",
        value="false",
        scope={"algorithm": "GRPO"},
    )
    existing = [
        {
            "id": "claim-compatible",
            "statement": "GRPO estimates advantages from group-relative rewards.",
            "subject": "GRPO",
            "aspect": "advantage estimation",
            "predicate": "uses",
            "value": "group-relative rewards",
            "scope": {"algorithm": "GRPO"},
        },
        {
            "id": "claim-conflict",
            "statement": "GRPO requires an independently trained critic model.",
            "subject": "GRPO",
            "aspect": "critic requirement",
            "predicate": "requires",
            "value": "true",
            "scope": {"algorithm": "GRPO"},
        },
    ]
    decision = ClaimRelationResolver().resolve(
        page_title="GRPO", incoming_claims=[incoming], existing_claims=existing
    )[0]
    assert decision.relation == "contradicts"
    assert decision.existing_claim_id == "claim-conflict"

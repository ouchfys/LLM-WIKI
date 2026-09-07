"""Claim-aware, incremental compiler for canonical Wiki Markdown."""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from system.wiki.paper_pipeline.models import (
    CandidateClaim,
    ClaimRelationDecision,
    DistilledCandidate,
    SourcePacket,
)


@dataclass
class CompilationResult:
    content_json: dict[str, Any]
    affected_claims: list[dict[str, Any]] = field(default_factory=list)


class HierarchicalWikiCompiler:
    """Compile section candidates into an append-only claim ledger.

    Page prose remains organized by the existing typed section schema.  This
    compiler adds the smaller semantic unit needed for source binding and page
    evolution: stable claims with add/strengthen/challenge/supersede actions.
    """

    def compile_claims(
        self,
        *,
        content_json: dict[str, Any],
        existing_claims: list[dict[str, Any]] | None,
        candidate: DistilledCandidate,
        packet: SourcePacket,
        relation_decisions: list[ClaimRelationDecision | dict[str, Any]] | None = None,
    ) -> CompilationResult:
        ledger = []
        for item in existing_claims or []:
            if not isinstance(item, dict):
                continue
            existing = dict(item)
            source_ids = [str(value) for value in existing.get("source_packet_ids") or [] if str(value)]
            # Recompiling the same source replaces claims that were supported
            # only by its previous parser output. Keeping them would leave
            # dangling evidence IDs after the evidence projection is replaced.
            if source_ids and set(source_ids) == {packet.source_id}:
                continue
            ledger.append(existing)
        affected: list[dict[str, Any]] = []
        decisions = {
            int(payload.get("incoming_index", -1)): payload
            for item in relation_decisions or []
            if (payload := _decision_payload(item)) and int(payload.get("incoming_index", -1)) >= 0
        }
        for index, claim in enumerate(candidate.claims):
            incoming = self._claim_payload(claim, candidate, packet, index)
            decision = decisions.get(index) or {
                "incoming_index": index,
                "existing_claim_id": "",
                "relation": "new",
                "confidence": 1.0,
                "reason": "no merge relation decision was required",
                "requires_review": False,
            }
            existing_id = str(decision.get("existing_claim_id") or "")
            match = next((item for item in ledger if str(item.get("id") or "") == existing_id), None)
            relation = str(decision.get("relation") or "new")
            action = "add_claim"
            if match and relation in {"equivalent", "supports"}:
                action = "strengthen_claim"
                merged_refs = _unique([*(match.get("evidence_ids") or []), *incoming["evidence_ids"]])
                match["evidence_ids"] = merged_refs
                match["confidence"] = max(float(match.get("confidence") or 0), incoming["confidence"])
                match.setdefault("source_packet_ids", [])
                match["source_packet_ids"] = _unique([*match["source_packet_ids"], packet.source_id])
                for field in ("subject", "aspect", "predicate", "value", "scope", "qualifiers"):
                    if not match.get(field) and incoming.get(field):
                        match[field] = incoming[field]
                affected.append({
                    "action": action,
                    "claim_id": match.get("id", ""),
                    "statement": match.get("statement", ""),
                    "compared_claim_id": match.get("id", ""),
                    "relation_decision": decision,
                    "requires_review": False,
                })
                continue
            if match:
                if relation == "supersedes":
                    action = "supersede_claim"
                    match["status"] = "superseded"
                    incoming["supersedes_claim_id"] = match.get("id", "")
                    incoming["comparison_reason"] = str(decision.get("reason") or "incoming claim supersedes an existing claim")
                elif relation == "contradicts":
                    action = "challenge_claim"
                    incoming["status"] = "conflicting"
                    incoming["conflicts_with_claim_id"] = match.get("id", "")
                    incoming["comparison_reason"] = str(decision.get("reason") or "incoming claim contradicts an existing claim")
            incoming["merge_relation"] = relation
            incoming["relation_decision"] = decision
            ledger.append(incoming)
            affected.append({
                "action": action,
                "claim_id": incoming["id"],
                "statement": incoming["statement"],
                "compared_claim_id": str(
                    incoming.get("conflicts_with_claim_id")
                    or incoming.get("supersedes_claim_id")
                    or ""
                ),
                "relation": relation,
                "relation_confidence": float(decision.get("confidence") or 0),
                "relation_reason": str(decision.get("reason") or ""),
                "same_entity": bool(decision.get("same_entity", True)),
                "same_aspect": bool(decision.get("same_aspect", False)),
                "scope_overlap": bool(decision.get("scope_overlap", True)),
                "requires_review": bool(decision.get("requires_review", False)),
                "comparison_subject": str(decision.get("comparison_subject") or incoming.get("subject") or candidate.title),
                "comparison_aspect": str(decision.get("comparison_aspect") or incoming.get("aspect") or ""),
                "comparison_scope": decision.get("comparison_scope") if isinstance(decision.get("comparison_scope"), dict) else incoming.get("scope", {}),
                "relation_decision": decision,
            })

        next_content = dict(content_json)
        next_content["claims"] = ledger
        next_content["affected_claims"] = affected
        next_content["compiler"] = {
            "name": "hierarchical-claim-compiler",
            "version": "1",
            "source_packet_id": packet.source_id,
        }
        return CompilationResult(content_json=next_content, affected_claims=affected)

    @staticmethod
    def unified_patch(before: str, after: str, page_id: str) -> str:
        return "".join(difflib.unified_diff(
            (before or "").splitlines(keepends=True),
            (after or "").splitlines(keepends=True),
            fromfile=f"{page_id}@previous.md",
            tofile=f"{page_id}@proposed.md",
        ))

    @staticmethod
    def _claim_payload(
        claim: CandidateClaim,
        candidate: DistilledCandidate,
        packet: SourcePacket,
        index: int,
    ) -> dict[str, Any]:
        seed = f"{packet.source_id}|{candidate.id}|{index}|{_normalize(claim.claim)}"
        claim_id = f"clm-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]}"
        return {
            "id": claim_id,
            "statement": claim.claim.strip(),
            "status": "supported",
            "relation": "supports",
            "subject": claim.subject.strip() or candidate.title,
            "aspect": claim.aspect.strip(),
            "predicate": claim.predicate.strip(),
            "value": claim.value.strip(),
            "scope": dict(claim.scope),
            "qualifiers": list(claim.qualifiers),
            "evidence_ids": _unique(claim.evidence_ids),
            "evidence_excerpt": claim.evidence.strip()[:1000],
            "section_id": claim.section_id,
            "page_start": claim.page_start,
            "confidence": max(0.0, min(float(claim.confidence), 1.0)),
            "verifier_result": claim.verifier_result,
            "verifier_reason": claim.verifier_reason,
            "entailment_score": max(0.0, min(float(claim.entailment_score), 1.0)),
            "semantic_verification_required": bool(packet.elements),
            "source_packet_ids": [packet.source_id],
            "candidate_id": candidate.id,
            "supersedes_claim_id": "",
            "conflicts_with_claim_id": "",
            "comparison_reason": "",
        }


def _tokens(value: str) -> list[str]:
    return re.findall(r"[0-9a-zA-Z]+|[\u4e00-\u9fff]", (value or "").lower())


def _normalize(value: str) -> str:
    return " ".join(_tokens(value))


def _unique(values: list[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _decision_payload(item: ClaimRelationDecision | dict[str, Any]) -> dict[str, Any]:
    if isinstance(item, dict):
        return dict(item)
    if hasattr(item, "model_dump"):
        return item.model_dump()
    if hasattr(item, "dict"):
        return item.dict()
    return {}

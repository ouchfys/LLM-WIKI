"""Independent verification against persisted source spans."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from system.core.llm_call import invoke_structured

from system.wiki.paper_pipeline.models import CandidateClaim, DistilledCandidate
from system.wiki.paper_pipeline.store import PaperWikiPipelineStore


@dataclass
class ClaimVerification:
    claim_id: str = ""
    statement: str = ""
    result: str = "unsupported"
    reason: str = ""
    evidence_ids: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    semantic_result: str = "not_run"
    semantic_reason: str = ""
    entailment_score: float = 0.0
    error_code: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "statement": self.statement,
            "result": self.result,
            "reason": self.reason,
            "evidence_ids": self.evidence_ids,
            "evidence": self.evidence,
            "semantic_result": self.semantic_result,
            "semantic_reason": self.semantic_reason,
            "entailment_score": self.entailment_score,
            "error_code": self.error_code,
            "details": self.details,
        }


SEMANTIC_VERIFICATION_PROMPT = """\
You are an evidence entailment verifier. Judge only whether the SOURCE EVIDENCE
entails the CLAIM. Do not use outside knowledge. Pay special attention to
negation, comparison direction, conditions, scope, causality, and whether a
number belongs to the stated model/dataset/metric. Interpret quantities with their
units, denominators and conditions. Accept justified unit conversions, counts and
faithful paraphrases even when their numeric strings differ from the source.
Distinguish list numbers and source/version metadata from factual quantities.
Evaluate support from context, never from numeric-string presence alone.

Return strict JSON only:
{{"label":"entailed|contradicted|insufficient","score":0.0,"reason":"short reason"}}

CLAIM:
{claim}

SOURCE EVIDENCE (persisted parser spans):
{evidence}
"""


class EvidenceVerifier:
    def __init__(self, store: PaperWikiPipelineStore, llm=None):
        self.store = store
        self.llm = llm

    def verify_claim_payloads(self, claims):
        return [self.verify_claim_payload(claim) for claim in claims]

    def bind_and_verify_candidate(self, candidate: DistilledCandidate) -> list[ClaimVerification]:
        packet = self.store.get_source_packet(candidate.source_packet_id)
        has_structured_evidence = bool(packet and packet.elements)
        results = []
        for claim in candidate.claims:
            refs = list(claim.evidence_ids)
            persisted = self.store.get_evidence(refs)
            persisted_text = " ".join(
                str(item.get("text") or item.get("caption") or "") for item in persisted
            )
            needs_rebind = (
                not refs
                or len(persisted) != len(set(refs))
                or (claim.evidence and _overlap(claim.evidence, persisted_text) < 0.35)
            )
            if needs_rebind:
                matches = self.store.find_evidence(
                    candidate.source_packet_id,
                    section_id=claim.section_id,
                    text=claim.evidence,
                    limit=3,
                )
                refs = [str(item.get("id") or "") for item in matches if item.get("id")]
                claim.evidence_ids = refs
            result = self.verify_claim(
                statement=claim.claim,
                evidence_excerpt=claim.evidence,
                evidence_ids=refs,
                structured_required=has_structured_evidence,
            )
            claim.verifier_result = (
                result.result
                if result.result == "legacy_unverified"
                else result.semantic_result
                if result.semantic_result != "not_run"
                else result.result
            )
            claim.verifier_reason = result.semantic_reason or result.reason
            claim.entailment_score = result.entailment_score
            results.append(result)
        return results

    def verify_claim_payload(self, claim: dict[str, Any]) -> ClaimVerification:
        if claim.get("verification_profile") == "repository":
            from system.wiki.repository_verification import RepositoryEvidenceVerifier
            return RepositoryEvidenceVerifier(self.store, self.llm).verify_claim_payload(claim)
        source_ids = [str(value) for value in claim.get("source_packet_ids") or [] if str(value)]
        expected = str(claim.get("verifier_result") or "").lower()
        structured_required = False
        for source_id in source_ids:
            packet = self.store.get_source_packet(source_id)
            structured_required = structured_required or bool(packet and packet.elements)
        result = self.verify_claim(
            statement=str(claim.get("statement") or ""),
            evidence_excerpt=str(claim.get("evidence_excerpt") or ""),
            evidence_ids=[str(value) for value in claim.get("evidence_ids") or [] if str(value)],
            structured_required=structured_required,
            semantic_preverified=expected in {"entailed", "supported"},
        )
        result.claim_id = str(claim.get("id") or "")
        if result.result == "supported" and claim.get("semantic_verification_required") and not expected:
            result.result = "unsupported"
            result.reason = "structured claim is missing semantic entailment verification"
            result.semantic_result = "missing"
            return result
        if result.result == "supported" and expected:
            result.semantic_result = expected
            result.semantic_reason = str(claim.get("verifier_reason") or "")
            result.entailment_score = float(claim.get("entailment_score") or 0.0)
            if expected in {"legacy_unverified", "verification_deferred"}:
                result.result = "legacy_unverified"
                result.reason = result.semantic_reason or "semantic verification deferred"
            elif expected not in {"entailed", "supported"}:
                result.result = "unsupported"
                result.reason = f"semantic verifier result is {expected}: {result.semantic_reason}"
        return result

    def verify_claim(
        self,
        *,
        statement: str,
        evidence_excerpt: str,
        evidence_ids: list[str],
        structured_required: bool,
        semantic_preverified: bool = False,
    ) -> ClaimVerification:
        evidence = self.store.get_evidence(evidence_ids)
        compact = [self._evidence_view(item) for item in evidence]
        if evidence_ids and len(evidence) != len(set(evidence_ids)):
            return ClaimVerification(statement=statement, result="unsupported", reason="one or more evidence ids do not exist", evidence_ids=evidence_ids, evidence=compact)
        if structured_required and not evidence:
            return ClaimVerification(statement=statement, result="unsupported", reason="structured source exists but claim has no resolvable evidence id", evidence_ids=evidence_ids)
        if not evidence:
            # Legacy/fallback parsers can still be imported, but the revision is
            # explicitly labelled instead of being misrepresented as verified.
            return ClaimVerification(statement=statement, result="legacy_unverified", reason="source has no structured elements", evidence_ids=[])

        source_text = " ".join(str(item.get("text") or item.get("caption") or "") for item in evidence)
        overlap = _overlap(evidence_excerpt, source_text)
        cross_script = _is_cross_script(evidence_excerpt, source_text)
        if evidence_excerpt and overlap < 0.35 and not (self.llm or semantic_preverified):
            return ClaimVerification(statement=statement, result="unsupported", reason=f"evidence excerpt/source overlap too low ({overlap:.2f})", evidence_ids=evidence_ids, evidence=compact)

        result = ClaimVerification(statement=statement, result="supported", reason="evidence ids resolved and source span was re-read", evidence_ids=evidence_ids, evidence=compact)
        if self.llm:
            strong_deterministic_binding = overlap >= 0.75 and not cross_script
            # The display preview is bounded; the review must see the actual
            # cited span, including conditions or quantities near its tail.
            review_evidence = [{**self._evidence_view(item), "text": str(item.get("text") or ""),
                                "metadata": item.get("metadata") or {}} for item in evidence]
            self._semantic_verify(
                result,
                statement,
                review_evidence,
                fallback_allowed=strong_deterministic_binding,
            )
        return result

    def _semantic_verify(
        self,
        result: ClaimVerification,
        statement: str,
        evidence: list[dict[str, Any]],
        *,
        fallback_allowed: bool = False,
    ) -> None:
        prompt = SEMANTIC_VERIFICATION_PROMPT.format(
            claim=statement,
            evidence=json.dumps(evidence, ensure_ascii=False, indent=2),
        )
        try:
            raw = invoke_structured(self.llm, prompt, temperature=0.0, max_tokens=500)
            payload = _json_object(raw)
        except Exception as exc:
            result.semantic_result = "error"
            result.semantic_reason = f"semantic verifier unavailable: {exc}"
            result.entailment_score = 0.0
            if fallback_allowed:
                # Preserve strongly bound source text during provider outages,
                # but label it honestly: semantic verification is deferred.
                result.result = "legacy_unverified"
                result.reason = (
                    "semantic verification deferred; deterministic evidence-id, "
                    "text-overlap checks passed; content review is still pending"
                )
            else:
                result.result = "unsupported"
                result.reason = result.semantic_reason
            return
        label = str(payload.get("label") or "insufficient").lower()
        if label not in {"entailed", "contradicted", "insufficient"}:
            label = "insufficient"
        try:
            score = max(0.0, min(float(payload.get("score") or 0.0), 1.0))
        except (TypeError, ValueError):
            score = 0.0
        result.semantic_result = label
        result.semantic_reason = str(payload.get("reason") or "")[:600]
        result.entailment_score = score
        if label != "entailed":
            result.result = "unsupported"
            result.reason = f"semantic entailment {label} ({score:.2f}): {result.semantic_reason}"

    @staticmethod
    def _evidence_view(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": item.get("id", ""),
            "kind": item.get("evidence_kind", "element"),
            # A parser may store an entire abstract or table note as one text
            # element. A 500-character preview silently removed the tail that
            # contained the exact evidence, causing systematic false rejects.
            "text": str(item.get("text") or "")[:2400],
            "page": int(item.get("page") or 0),
            "bbox": item.get("bbox") or {},
            "docling_ref": item.get("docling_ref", ""),
        }


def _overlap(left: str, right: str) -> float:
    left_tokens = set(_tokens(left))
    right_tokens = set(_tokens(right))
    if not left_tokens:
        return 1.0
    return len(left_tokens & right_tokens) / len(left_tokens)


def _tokens(value: str) -> list[str]:
    return re.findall(r"[0-9a-zA-Z]+|[\u4e00-\u9fff]", (value or "").lower())


def _is_cross_script(left: str, right: str) -> bool:
    left_cjk = bool(re.search(r"[\u4e00-\u9fff]", left or ""))
    right_cjk = bool(re.search(r"[\u4e00-\u9fff]", right or ""))
    left_latin = bool(re.search(r"[A-Za-z]{3,}", left or ""))
    right_latin = bool(re.search(r"[A-Za-z]{3,}", right or ""))
    return (left_cjk and right_latin and not right_cjk) or (right_cjk and left_latin and not left_cjk)



def _json_object(value: Any) -> dict[str, Any]:
    text = str(value or "").strip()
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return {}
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}

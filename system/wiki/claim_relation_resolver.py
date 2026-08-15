"""Resolve new-to-existing claim relations during Wiki merge.

Distillation is deliberately source-local: it extracts what one paper claims.
This resolver owns the cross-source decision because only the merge stage knows
the target Wiki page, its existing claim ledger, and both sides' provenance.
"""

from __future__ import annotations

import json
import re
from typing import Any

from system.core.llm_call import invoke_structured
from system.wiki.paper_pipeline.models import (
    CandidateClaim,
    ClaimRelationDecision,
)


RELATIONS = {
    "new", "equivalent", "supports", "complements", "contradicts",
    "supersedes", "unrelated", "uncertain",
}
CONFLICT_RELATIONS = {"contradicts", "supersedes"}
REVIEW_CONFIDENCE = 0.75
MAX_PAIRS_PER_CLAIM = 5

RELATION_PROMPT = """\
You are the claim relation resolver inside a Wiki merge operation.
The incoming claims have already been verified against their own paper. Decide
how each incoming claim relates to a candidate claim already stored on the SAME
Wiki page.

A contradiction requires all of these conditions:
1. both claims refer to the same knowledge entity;
2. both discuss the same aspect/property;
3. their scopes overlap (version, dataset, model, task, experiment and time);
4. their conclusions cannot both be true.

Different aspects, datasets, versions, metrics or experimental settings are not
contradictions. Use supersedes only when the incoming claim explicitly replaces
an older version or conclusion. Do not infer factual support from lexical overlap.

Allowed relations: equivalent, supports, complements, contradicts, supersedes,
unrelated, uncertain.

Return strict JSON:
{{
  "decisions": [
    {{
      "incoming_index": 0,
      "existing_claim_id": "...",
      "relation": "complements",
      "confidence": 0.0,
      "same_entity": true,
      "same_aspect": false,
      "scope_overlap": true,
      "comparison_subject": "...",
      "comparison_aspect": "...",
      "comparison_scope": {{}},
      "reason": "..."
    }}
  ]
}}

Pairs:
{pairs}
"""


class ClaimRelationResolver:
    """Find comparable old claims and classify their merge relationship."""

    def __init__(self, llm: Any = None):
        self.llm = llm

    def resolve(
        self,
        *,
        page_title: str,
        incoming_claims: list[CandidateClaim],
        existing_claims: list[dict[str, Any]],
    ) -> list[ClaimRelationDecision]:
        decisions: list[ClaimRelationDecision] = []
        uncertain_pairs: list[dict[str, Any]] = []
        pair_defaults: dict[tuple[int, str], ClaimRelationDecision] = {}

        for index, incoming in enumerate(incoming_claims):
            candidates = self._candidate_pairs(page_title, incoming, existing_claims)
            if not candidates:
                decisions.append(self._new_decision(index, page_title, incoming))
                continue

            existing, score = candidates[0]
            deterministic = self._deterministic_decision(
                page_title, index, incoming, existing, score
            )
            if deterministic.relation in {"equivalent", "supports", "contradicts", "supersedes"}:
                decisions.append(deterministic)
                continue

            if not self.llm:
                decisions.append(deterministic)
                continue

            for old, pair_score in candidates:
                default = self._deterministic_decision(
                    page_title, index, incoming, old, pair_score
                )
                existing_id = str(old.get("id") or "")
                pair_defaults[(index, existing_id)] = default
                uncertain_pairs.append({
                    "incoming_index": index,
                    "incoming": _incoming_payload(page_title, incoming),
                    "existing": _existing_payload(page_title, old),
                    "candidate_score": round(pair_score, 4),
                })

        llm_decisions = self._resolve_with_llm(uncertain_pairs, pair_defaults)
        decided_indexes = {item.incoming_index for item in decisions}
        for index, incoming in enumerate(incoming_claims):
            if index in decided_indexes:
                continue
            candidates = [item for item in llm_decisions if item.incoming_index == index]
            if candidates:
                candidates.sort(key=lambda item: (-_decision_priority(item), -item.confidence))
                decisions.append(candidates[0])
            else:
                existing_candidates = self._candidate_pairs(page_title, incoming, existing_claims)
                if existing_candidates:
                    existing, score = existing_candidates[0]
                    decisions.append(self._deterministic_decision(
                        page_title, index, incoming, existing, score
                    ))
                else:
                    decisions.append(self._new_decision(index, page_title, incoming))

        decisions.sort(key=lambda item: item.incoming_index)
        return decisions

    @staticmethod
    def _candidate_pairs(
        page_title: str,
        incoming: CandidateClaim,
        existing_claims: list[dict[str, Any]],
    ) -> list[tuple[dict[str, Any], float]]:
        ranked: list[tuple[dict[str, Any], float]] = []
        for existing in existing_claims:
            if not isinstance(existing, dict) or not str(existing.get("id") or ""):
                continue
            score = _pair_score(page_title, incoming, existing)
            if score >= 0.35:
                ranked.append((existing, score))
        ranked.sort(key=lambda item: (-item[1], str(item[0].get("id") or "")))
        return ranked[:MAX_PAIRS_PER_CLAIM]

    @staticmethod
    def _deterministic_decision(
        page_title: str,
        incoming_index: int,
        incoming: CandidateClaim,
        existing: dict[str, Any],
        pair_score: float,
    ) -> ClaimRelationDecision:
        existing_id = str(existing.get("id") or "")
        subject = incoming.subject.strip() or str(existing.get("subject") or "").strip() or page_title
        aspect = incoming.aspect.strip() or str(existing.get("aspect") or "").strip()
        scope, scope_overlap = _merged_scope(incoming.scope, _dict(existing.get("scope")))
        same_aspect = _same_aspect(incoming, existing, pair_score)
        relation = "complements"
        confidence = max(0.5, min(pair_score, 0.9))
        reason = "claims belong to the same page but discuss compatible or distinct knowledge"

        incoming_statement = _normalize(incoming.claim)
        existing_statement = _normalize(str(existing.get("statement") or ""))
        if incoming_statement and incoming_statement == existing_statement:
            relation = "equivalent"
            confidence = 1.0
            same_aspect = True
            reason = "normalized statements are equivalent"
        elif same_aspect and scope_overlap and _structured_opposition(incoming, existing):
            relation = "contradicts"
            confidence = 0.94
            reason = "same entity, aspect and scope have incompatible structured values"
        elif same_aspect and scope_overlap and pair_score >= 0.55 and _polarity_conflict(
            incoming.claim, str(existing.get("statement") or "")
        ):
            relation = "contradicts"
            confidence = min(0.9, max(0.78, pair_score))
            reason = "same entity, aspect and scope have opposite polarity"
        elif (
            incoming.relation in {"challenges", "supersedes"}
            and same_aspect and scope_overlap and pair_score >= 0.55
        ):
            # Backward compatibility for candidates persisted before relation
            # ownership moved from Distiller to Merge. New prompts never emit it.
            relation = "supersedes" if incoming.relation == "supersedes" else "contradicts"
            confidence = min(0.86, max(0.76, pair_score))
            reason = "legacy source relation hint confirmed against a comparable existing claim"

        requires_review = (
            relation in CONFLICT_RELATIONS
            and same_aspect
            and scope_overlap
            and confidence >= REVIEW_CONFIDENCE
        )
        return ClaimRelationDecision(
            incoming_index=incoming_index,
            existing_claim_id=existing_id,
            relation=relation,
            confidence=round(confidence, 4),
            reason=reason,
            same_entity=True,
            same_aspect=same_aspect,
            scope_overlap=scope_overlap,
            requires_review=requires_review,
            comparison_subject=subject,
            comparison_aspect=aspect or "same knowledge question",
            comparison_scope=scope,
        )

    @staticmethod
    def _new_decision(
        incoming_index: int, page_title: str, incoming: CandidateClaim
    ) -> ClaimRelationDecision:
        return ClaimRelationDecision(
            incoming_index=incoming_index,
            relation="new",
            confidence=1.0,
            reason="no comparable existing claim on the resolved Wiki page",
            same_entity=True,
            same_aspect=False,
            scope_overlap=True,
            requires_review=False,
            comparison_subject=incoming.subject.strip() or page_title,
            comparison_aspect=incoming.aspect.strip(),
            comparison_scope=dict(incoming.scope),
        )

    def _resolve_with_llm(
        self,
        pairs: list[dict[str, Any]],
        defaults: dict[tuple[int, str], ClaimRelationDecision],
    ) -> list[ClaimRelationDecision]:
        if not pairs or not self.llm:
            return []
        prompt = RELATION_PROMPT.format(
            pairs=json.dumps(pairs, ensure_ascii=False, indent=2)
        )
        try:
            raw = invoke_structured(self.llm, prompt, temperature=0.0, max_tokens=2800)
            payload = _json_object(raw)
        except Exception as exc:
            print(f"[wiki.claim_relation_resolver] LLM relation resolution failed: {exc}")
            return []
        output: list[ClaimRelationDecision] = []
        for item in payload.get("decisions") or []:
            if not isinstance(item, dict):
                continue
            index = _int(item.get("incoming_index"), -1)
            existing_id = str(item.get("existing_claim_id") or "")
            default = defaults.get((index, existing_id))
            relation = str(item.get("relation") or "uncertain")
            if not default or relation not in RELATIONS - {"new"}:
                continue
            confidence = max(0.0, min(_float(item.get("confidence"), 0.0), 1.0))
            same_entity = bool(item.get("same_entity", default.same_entity))
            same_aspect = bool(item.get("same_aspect", default.same_aspect))
            scope_overlap = bool(item.get("scope_overlap", default.scope_overlap))
            requires_review = (
                relation in CONFLICT_RELATIONS
                and same_entity and same_aspect and scope_overlap
                and confidence >= REVIEW_CONFIDENCE
            )
            output.append(ClaimRelationDecision(
                incoming_index=index,
                existing_claim_id=existing_id,
                relation=relation,
                confidence=round(confidence, 4),
                reason=str(item.get("reason") or default.reason)[:1000],
                same_entity=same_entity,
                same_aspect=same_aspect,
                scope_overlap=scope_overlap,
                requires_review=requires_review,
                comparison_subject=str(item.get("comparison_subject") or default.comparison_subject),
                comparison_aspect=str(item.get("comparison_aspect") or default.comparison_aspect),
                comparison_scope=_dict(item.get("comparison_scope")) or default.comparison_scope,
            ))
        return output


def _incoming_payload(page_title: str, claim: CandidateClaim) -> dict[str, Any]:
    return {
        "statement": claim.claim,
        "subject": claim.subject or page_title,
        "aspect": claim.aspect,
        "predicate": claim.predicate,
        "value": claim.value,
        "scope": claim.scope,
        "qualifiers": claim.qualifiers,
        "evidence_excerpt": claim.evidence[:800],
    }


def _existing_payload(page_title: str, claim: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(claim.get("id") or ""),
        "statement": str(claim.get("statement") or ""),
        "subject": str(claim.get("subject") or page_title),
        "aspect": str(claim.get("aspect") or ""),
        "predicate": str(claim.get("predicate") or ""),
        "value": str(claim.get("value") or ""),
        "scope": _dict(claim.get("scope")),
        "qualifiers": list(claim.get("qualifiers") or []),
        "evidence_excerpt": str(claim.get("evidence_excerpt") or "")[:800],
    }


def _pair_score(page_title: str, incoming: CandidateClaim, existing: dict[str, Any]) -> float:
    incoming_statement = set(_tokens(incoming.claim))
    existing_statement = set(_tokens(str(existing.get("statement") or "")))
    statement_score = _jaccard(incoming_statement, existing_statement)
    incoming_aspect = set(_tokens(incoming.aspect))
    existing_aspect = set(_tokens(str(existing.get("aspect") or "")))
    aspect_score = _jaccard(incoming_aspect, existing_aspect) if incoming_aspect and existing_aspect else 0.0
    incoming_subject = set(_tokens(incoming.subject or page_title))
    existing_subject = set(_tokens(str(existing.get("subject") or page_title)))
    subject_score = _jaccard(incoming_subject, existing_subject)
    if aspect_score:
        return 0.5 * aspect_score + 0.35 * statement_score + 0.15 * subject_score
    return 0.8 * statement_score + 0.2 * subject_score


def _same_aspect(incoming: CandidateClaim, existing: dict[str, Any], pair_score: float) -> bool:
    left = set(_tokens(incoming.aspect))
    right = set(_tokens(str(existing.get("aspect") or "")))
    if left and right:
        return _jaccard(left, right) >= 0.65
    return pair_score >= 0.55


def _structured_opposition(incoming: CandidateClaim, existing: dict[str, Any]) -> bool:
    left_predicate = _normalize(incoming.predicate)
    right_predicate = _normalize(str(existing.get("predicate") or ""))
    if left_predicate and right_predicate and left_predicate != right_predicate:
        opposite_pairs = {
            ("requires", "does not require"), ("supports", "does not support"),
            ("increases", "decreases"), ("higher", "lower"),
            ("有", "无"), ("需要", "不需要"), ("提高", "降低"),
        }
        if (left_predicate, right_predicate) in opposite_pairs or (right_predicate, left_predicate) in opposite_pairs:
            return True
    left_value = _canonical_value(incoming.value)
    right_value = _canonical_value(existing.get("value"))
    return bool(left_value and right_value and left_value != right_value and {left_value, right_value} <= {"true", "false"})


def _merged_scope(left: Any, right: Any) -> tuple[dict[str, str], bool]:
    left_scope = {str(k): str(v) for k, v in _dict(left).items() if str(v).strip()}
    right_scope = {str(k): str(v) for k, v in _dict(right).items() if str(v).strip()}
    overlap = True
    for key in set(left_scope) & set(right_scope):
        if _normalize(left_scope[key]) != _normalize(right_scope[key]):
            overlap = False
    merged = dict(right_scope)
    merged.update(left_scope)
    return merged, overlap


def _polarity_conflict(left: str, right: str) -> bool:
    negation = {"not", "no", "never", "without", "cannot", "不", "没有", "并非", "无法", "无需", "不需要"}
    return bool(set(_tokens(left)) & negation) != bool(set(_tokens(right)) & negation)


def _decision_priority(item: ClaimRelationDecision) -> int:
    return {
        "supersedes": 7, "contradicts": 6, "equivalent": 5, "supports": 4,
        "complements": 3, "uncertain": 2, "unrelated": 1, "new": 0,
    }.get(item.relation, 0)


def _canonical_value(value: Any) -> str:
    text = _normalize(str(value or ""))
    if text in {"true", "yes", "required", "1", "是", "需要", "有"}:
        return "true"
    if text in {"false", "no", "not required", "0", "否", "不需要", "无"}:
        return "false"
    return text


def _tokens(value: str) -> list[str]:
    return re.findall(r"[0-9a-zA-Z]+|[\u4e00-\u9fff]", (value or "").lower())


def _normalize(value: str) -> str:
    return " ".join(_tokens(value))


def _jaccard(left: set[str], right: set[str]) -> float:
    return len(left & right) / max(len(left | right), 1)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _json_object(raw: Any) -> dict[str, Any]:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            payload = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return {}
    return payload if isinstance(payload, dict) else {}

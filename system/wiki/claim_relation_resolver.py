"""Resolve new-to-existing claim relations during Wiki merge.

Distillation is deliberately source-local: it extracts what one paper claims.
This resolver owns the cross-source decision because only the merge stage knows
the target Wiki page, its existing claim ledger, and both sides' provenance.
"""

from __future__ import annotations

import json
import os
import re

import requests
from typing import Any

from system.core.llm_call import invoke_structured
from system.agent_runtime.control import RunCancelled, RunInterrupted
from system.agent_runtime.tracing import get_current_trace
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
MAX_PAIRS_PER_CLAIM = 20
DEFAULT_COMPARISON_BATCH_SIZE = 8

RELATION_PROMPT = """\
You are the claim relation resolver inside a Wiki merge operation.
The incoming claims have already been verified against their own paper. Decide
how each incoming claim relates to a candidate claim retrieved from the Wiki.
Candidates may come from another page, so page proximity is not evidence that
two claims discuss the same entity or aspect.

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

The input contains deduplicated incoming_claims and existing_claims dictionaries,
and pairs referencing their keys. Return exactly one decision for EVERY listed
pair, including unrelated pairs; do not omit pairs or invent IDs. Keep reasons
concise. Source/evidence IDs identify provenance; they are not extra claims.

Comparison batch:
{pairs}
"""


class ClaimRelationResolver:
    """Find comparable old claims and classify their merge relationship."""

    def __init__(self, llm: Any = None, retriever: Any = None):
        self.llm = llm
        self.retriever = retriever
        self._transport_failure = ""

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
        candidates_by_index: dict[int, list[tuple[dict[str, Any], float]]] = {}

        for index, incoming in enumerate(incoming_claims):
            try:
                comparison_claims = self._comparison_claims(
                    page_title, incoming, existing_claims
                )
            except (RunCancelled, RunInterrupted):
                raise
            except Exception as exc:
                failure = f"{type(exc).__name__}: {exc}"[:800]
                decisions.append(_failed_decision(
                    self._new_decision(index, page_title, incoming), failure,
                    status="retrieval_failed",
                ))
                _record_failure("claim.retrieval.failed", failure, {"incoming_index": index})
                continue
            candidates = self._candidate_pairs(page_title, incoming, comparison_claims)
            candidates_by_index[index] = candidates
            if not candidates:
                decisions.append(self._new_decision(index, page_title, incoming))
                continue

            defaults = [
                self._deterministic_decision(
                    page_title, index, incoming, existing, score
                )
                for existing, score in candidates
            ]
            deterministic_conflicts = [
                item for item in defaults
                if item.relation in CONFLICT_RELATIONS and item.requires_review
            ]
            if deterministic_conflicts:
                deterministic_conflicts.sort(
                    key=lambda item: (-_decision_priority(item), -item.confidence)
                )
                decisions.append(deterministic_conflicts[0])
                continue

            if not self.llm:
                defaults.sort(
                    key=lambda item: (-_decision_priority(item), -item.confidence)
                )
                # Exact equality or explicit disjoint scopes can be resolved
                # without a model; lexical similarity alone cannot prove safety.
                unresolved = [item for item in defaults if item.relation != "equivalent" and item.scope_overlap]
                decisions.append(
                    _failed_decision(unresolved[0], "relation model is unavailable", status="model_unavailable")
                    if unresolved else defaults[0]
                )
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
                existing_candidates = candidates_by_index.get(index, [])
                if existing_candidates:
                    fallback_decisions = [
                        self._deterministic_decision(
                            page_title, index, incoming, existing, score
                        )
                        for existing, score in existing_candidates
                    ]
                    fallback_decisions.sort(
                        key=lambda item: (-_decision_priority(item), -item.confidence)
                    )
                    decisions.append(_failed_decision(
                        fallback_decisions[0], "no complete relation decision was produced"
                    ))
                else:
                    decisions.append(self._new_decision(index, page_title, incoming))

        decisions.sort(key=lambda item: item.incoming_index)
        return decisions

    def _comparison_claims(
        self,
        page_title: str,
        incoming: CandidateClaim,
        existing_claims: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not self.retriever:
            return existing_claims
        recalled = self.retriever.retrieve(
            page_title=page_title,
            incoming=incoming,
            local_claims=existing_claims,
            limit=MAX_PAIRS_PER_CLAIM,
        )
        merged: dict[str, dict[str, Any]] = {}
        for claim in [*existing_claims, *recalled]:
            if not isinstance(claim, dict):
                continue
            claim_id = str(claim.get("id") or "")
            if not claim_id:
                continue
            merged[claim_id] = {**merged.get(claim_id, {}), **claim}
        return list(merged.values())

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
            retrieved = bool(existing.get("_retrieval_routes"))
            if score >= 0.35 or retrieved:
                score = max(score, 0.36) if retrieved else score
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
        same_entity = _same_entity(page_title, incoming, existing)
        same_aspect = _same_aspect(incoming, existing, pair_score)
        relation = "complements" if same_entity else "unrelated"
        confidence = max(0.5, min(pair_score, 0.9))
        reason = (
            "claims discuss compatible or distinct knowledge"
            if same_entity else "retrieval candidate does not appear to describe the same entity"
        )

        incoming_statement = _normalize(incoming.claim)
        existing_statement = _normalize(str(existing.get("statement") or ""))
        if same_entity and incoming_statement and incoming_statement == existing_statement:
            relation = "equivalent"
            confidence = 1.0
            same_aspect = True
            reason = "normalized statements are equivalent"
        elif same_entity and same_aspect and scope_overlap and _structured_opposition(incoming, existing):
            relation = "contradicts"
            confidence = 0.94
            reason = "same entity, aspect and scope have incompatible structured values"
        elif same_entity and same_aspect and scope_overlap and pair_score >= 0.55 and _polarity_conflict(
            incoming.claim, str(existing.get("statement") or "")
        ):
            relation = "contradicts"
            confidence = min(0.9, max(0.78, pair_score))
            reason = "same entity, aspect and scope have opposite polarity"
        elif (
            incoming.relation in {"challenges", "supersedes"}
            and same_entity and same_aspect and scope_overlap and pair_score >= 0.55
        ):
            # Backward compatibility for candidates persisted before relation
            # ownership moved from Distiller to Merge. New prompts never emit it.
            relation = "supersedes" if incoming.relation == "supersedes" else "contradicts"
            confidence = min(0.86, max(0.76, pair_score))
            reason = "legacy source relation hint confirmed against a comparable existing claim"

        requires_review = (
            relation in CONFLICT_RELATIONS
            and same_entity and same_aspect
            and scope_overlap
            and confidence >= REVIEW_CONFIDENCE
        )
        return ClaimRelationDecision(
            incoming_index=incoming_index,
            existing_claim_id=existing_id,
            relation=relation,
            confidence=round(confidence, 4),
            reason=reason,
            same_entity=same_entity,
            same_aspect=same_aspect,
            scope_overlap=scope_overlap,
            requires_review=requires_review,
            comparison_subject=subject,
            comparison_aspect=aspect or "same knowledge question",
            comparison_scope=scope,
            existing_page_id=str(existing.get("_page_id") or ""),
            existing_page_title=str(existing.get("_page_title") or ""),
            retrieval_routes=[str(item) for item in existing.get("_retrieval_routes") or []],
        )

    @staticmethod
    def _new_decision(
        incoming_index: int, page_title: str, incoming: CandidateClaim
    ) -> ClaimRelationDecision:
        return ClaimRelationDecision(
            incoming_index=incoming_index,
            relation="new",
            confidence=1.0,
            reason="candidate retrieval found no comparable existing claim",
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
        if self._transport_failure:
            return [_failed_decision(defaults[(p["incoming_index"], p["existing"]["id"])], self._transport_failure) for p in pairs]
        initial_budget = _positive_setting("PAPERWIKI_RELATION_OUTPUT_TOKENS", 16384)
        retry_budget = max(initial_budget, _positive_setting("PAPERWIKI_RELATION_RETRY_OUTPUT_TOKENS", 32768))
        batch_size = _positive_setting("PAPERWIKI_RELATION_BATCH_PAIRS", DEFAULT_COMPARISON_BATCH_SIZE)
        output: list[ClaimRelationDecision] = []
        for start in range(0, len(pairs), batch_size):
            batch = pairs[start:start + batch_size]
            expected = {(item["incoming_index"], item["existing"]["id"]) for item in batch}
            prompt = RELATION_PROMPT.format(pairs=json.dumps(_comparison_batch(batch), ensure_ascii=False, separators=(",", ":")))
            failure = ""
            for attempt, budget in enumerate((initial_budget, retry_budget), start=1):
                request = prompt
                if failure:
                    request += "\nPrevious response was unusable: " + failure + "\nReturn complete JSON for every listed pair."
                try:
                    raw = invoke_structured(self.llm, request, temperature=0.0, max_tokens=budget, max_attempts=1)
                    resolved = self._parse_decisions(_json_object(raw), expected, defaults)
                    output.extend(resolved)
                    break
                except (RunCancelled, RunInterrupted):
                    raise
                except Exception as exc:
                    failure = f"{type(exc).__name__}: {exc}"[:800]
                    if isinstance(exc, (requests.RequestException, TimeoutError, ConnectionError)):
                        self._transport_failure = failure
                        _record_failure("claim.relation.failed", failure, {"batch_start": start, "remaining_pairs": len(pairs) - start, "circuit_open": True})
                        output.extend(_failed_decision(defaults[(p["incoming_index"], p["existing"]["id"])], failure) for p in pairs[start:])
                        return output
                    _record_failure(
                        "claim.relation.retry" if attempt == 1 else "claim.relation.failed",
                        failure, {"attempt": attempt, "max_attempts": 2, "max_tokens": budget,
                                  "batch_start": start, "pair_count": len(batch), "prompt_chars": len(request)},
                    )
            else:
                # Preserve every unassessed pair as an explicit review blocker.
                # Another successful pair for the same incoming claim must not
                # hide a failed comparison with a different Wiki page.
                output.extend(_failed_decision(defaults[key], failure) for key in sorted(expected))
        return output

    @staticmethod
    def _parse_decisions(
        payload: dict[str, Any], expected: set[tuple[int, str]],
        defaults: dict[tuple[int, str], ClaimRelationDecision],
    ) -> list[ClaimRelationDecision]:
        items = payload.get("decisions")
        if not isinstance(items, list):
            raise ValueError("relation response must contain a decisions array")
        output = []
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("relation decision must be an object")
            index = _int(item.get("incoming_index"), -1)
            existing_id = str(item.get("existing_claim_id") or "")
            key = (index, existing_id)
            if key not in expected or key in seen:
                raise ValueError(f"unexpected or duplicate relation pair: {key}")
            seen.add(key)
            default = defaults[key]
            relation = str(item.get("relation") or "")
            if relation not in RELATIONS - {"new"}:
                raise ValueError(f"invalid relation for pair {key}: {relation}")
            if any(not isinstance(item.get(field), bool) for field in ("same_entity", "same_aspect", "scope_overlap")):
                raise ValueError(f"relation flags must be booleans for pair {key}")
            confidence = max(0.0, min(_float(item.get("confidence"), 0.0), 1.0))
            same_entity = item["same_entity"]
            same_aspect = item["same_aspect"]
            scope_overlap = item["scope_overlap"]
            if relation in CONFLICT_RELATIONS and not (
                same_entity and same_aspect and scope_overlap
            ):
                relation = "uncertain"
            requires_review = relation in CONFLICT_RELATIONS or relation == "uncertain"
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
                existing_page_id=default.existing_page_id,
                existing_page_title=default.existing_page_title,
                retrieval_routes=list(default.retrieval_routes),
            ))
        if seen != expected:
            raise ValueError(f"incomplete relation response: {len(expected - seen)} of {len(expected)} pairs missing")
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
        "evidence_ids": list(claim.evidence_ids),
        "section_id": claim.section_id,
        "page_start": claim.page_start,
    }


def _existing_payload(page_title: str, claim: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(claim.get("id") or ""),
        "page_id": str(claim.get("_page_id") or ""),
        "page_title": str(claim.get("_page_title") or page_title),
        "statement": str(claim.get("statement") or ""),
        "subject": str(claim.get("subject") or page_title),
        "aspect": str(claim.get("aspect") or ""),
        "predicate": str(claim.get("predicate") or ""),
        "value": str(claim.get("value") or ""),
        "scope": _dict(claim.get("scope")),
        "qualifiers": list(claim.get("qualifiers") or []),
        "evidence_excerpt": str(claim.get("evidence_excerpt") or "")[:800],
        "evidence_ids": list(claim.get("evidence_ids") or []),
        "source_packet_ids": list(claim.get("source_packet_ids") or []),
        "retrieval_routes": [str(item) for item in claim.get("_retrieval_routes") or []],
    }


def _comparison_batch(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """Deduplicate repeated claim bodies without dropping any candidate pair."""
    incoming = {}
    existing = {}
    references = []
    for pair in pairs:
        index = pair["incoming_index"]
        old_id = pair["existing"]["id"]
        incoming[str(index)] = pair["incoming"]
        existing[old_id] = pair["existing"]
        references.append({"incoming_index": index, "existing_claim_id": old_id})
    return {"incoming_claims": incoming, "existing_claims": existing, "pairs": references}


def _failed_decision(
    default: ClaimRelationDecision, error: str, *, status: str = "model_failed",
) -> ClaimRelationDecision:
    payload = default.model_dump() if hasattr(default, "model_dump") else default.dict()
    payload.update(
        relation="uncertain", confidence=0.0, requires_review=True,
        resolution_status=status,
        reason=f"Relation comparison incomplete ({status}); no semantic conflict was established. {error}"[:1000],
    )
    return ClaimRelationDecision(**payload)


def _record_failure(event_type: str, error: str, data: dict[str, Any]) -> None:
    trace = get_current_trace()
    if trace:
        trace.event(event_type, name="claim_relation", status="retrying" if event_type.endswith("retry") else "failed", data=data, error=error)
    print(f"[wiki.claim_relation_resolver] {event_type}: {error}")


def _positive_setting(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


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


def _same_entity(
    page_title: str, incoming: CandidateClaim, existing: dict[str, Any]
) -> bool:
    left = set(_tokens(incoming.subject or page_title))
    right = set(_tokens(str(
        existing.get("subject") or existing.get("_page_title") or page_title
    )))
    if left and right:
        return bool(left & right) or _normalize(incoming.subject or page_title) == _normalize(
            str(existing.get("subject") or existing.get("_page_title") or page_title)
        )
    return False


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
    if item.resolution_status != "resolved":
        return 100
    return (10 if item.requires_review else 0) + {
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

from __future__ import annotations

import json
from typing import Any

from system.wiki.paper_pipeline.distiller import parse_json_object
from system.wiki.paper_pipeline.models import DistilledCandidate, ReviewReport
from system.wiki.paper_pipeline.store import PaperWikiPipelineStore, normalize_alias
from system.wiki.wiki_builder import sanitize_wiki_text
from system.wiki.wiki_store import CARD_TYPES, WikiStore

ALLOWED_STATUSES = {"approved", "needs_revision", "rejected"}
ALLOWED_ACTIONS = {"create_new", "update_existing", "link_only", "skip_duplicate", "needs_human_review"}
TARGET_ACTIONS = {"update_existing", "link_only", "skip_duplicate"}
MIN_ACTION_CONFIDENCE = 0.8

REVIEW_PROMPT = """\
You are the reviewer agent for a paper-to-wiki pipeline.
Return strict JSON only. Do not write markdown or explanations outside JSON.

Your job:
- Check whether the candidate is schema-valid and evidence-supported.
- Detect likely duplicates or related existing cards.
- Recommend create_new, update_existing, link_only, or needs_human_review.
- The database will be updated only by Python after your JSON is validated.

Allowed status values: approved, needs_revision, rejected.
Allowed merge_recommendation.action values: create_new, update_existing, link_only, skip_duplicate, needs_human_review.
Use update_existing only when the candidate is the same reusable concept/method as an existing card.
Use link_only when it is related but should stay separate.
Use skip_duplicate when the existing card already contains the same reusable knowledge and only source provenance should be recorded.
Use needs_human_review for low confidence or ambiguous matches.

Return this exact shape:
{{
  "status": "approved",
  "schema_errors": [],
  "unsupported_claims": [],
  "evidence_quality": "good",
  "duplicate_candidates": [
    {{
      "existing_card_id": "",
      "existing_title": "",
      "confidence": 0.0,
      "reason": ""
    }}
  ],
  "merge_recommendation": {{
    "action": "create_new",
    "target_card_id": "",
    "confidence": 0.0,
    "reason": ""
  }}
}}

Input:
{payload}
"""


class PaperReviewAgent:
    def __init__(self, pipeline_store: PaperWikiPipelineStore, wiki_store: WikiStore, llm=None):
        self.pipeline_store = pipeline_store
        self.wiki_store = wiki_store
        self.llm = llm
        self.llm_calls = 0

    def review(self, candidate: DistilledCandidate) -> ReviewReport:
        schema_errors = self._schema_errors(candidate)
        unsupported_claims = self._unsupported_claims(candidate)
        duplicate = self._duplicate_candidate(candidate)
        deterministic = self._deterministic_report(candidate, schema_errors, unsupported_claims, duplicate)
        similar_cards = self._similar_cards(candidate) if self._should_use_llm(candidate, schema_errors, unsupported_claims, duplicate) else []
        report = self._llm_report(candidate, schema_errors, unsupported_claims, duplicate, similar_cards) if similar_cards or duplicate else None
        report = report or deterministic
        report = self._validated_or_fallback(candidate, report, deterministic)
        self.pipeline_store.insert_review_report(report)
        self.pipeline_store.update_candidate_status(candidate.id, report.status)
        return report

    def _deterministic_report(
        self,
        candidate: DistilledCandidate,
        schema_errors: list[str],
        unsupported_claims: list[str],
        duplicate: dict | None,
    ) -> ReviewReport:
        status = "approved"
        if schema_errors or unsupported_claims:
            status = "needs_revision" if candidate.candidate_type == "paper_page" else "rejected"
        recommendation = self._merge_recommendation(candidate, duplicate, status)
        report = ReviewReport(
            candidate_id=candidate.id,
            status=status,
            schema_errors=schema_errors,
            unsupported_claims=unsupported_claims,
            evidence_quality="good" if candidate.claims and not unsupported_claims else "weak",
            duplicate_candidates=[duplicate] if duplicate else [],
            merge_recommendation=recommendation,
        )
        return report

    def _llm_report(
        self,
        candidate: DistilledCandidate,
        schema_errors: list[str],
        unsupported_claims: list[str],
        duplicate: dict | None,
        similar_cards: list[dict[str, Any]],
    ) -> ReviewReport | None:
        if not self.llm:
            return None
        source_packet = self.pipeline_store.get_source_packet(candidate.source_packet_id)
        payload = {
            "candidate": _model_dump(candidate),
            "candidate_claims": [_model_dump(claim) for claim in candidate.claims],
            "candidate_evidence": [_model_dump(claim) for claim in candidate.claims if sanitize_wiki_text(claim.evidence)],
            "source_title": source_packet.title if source_packet else "",
            "source_section_ids": [claim.section_id for claim in candidate.claims if claim.section_id],
            "top_similar_existing_cards": similar_cards,
            "alias_match_result": duplicate or {},
            "deterministic_schema_check": {
                "schema_errors": schema_errors,
                "unsupported_claims": unsupported_claims,
            },
        }
        prompt = REVIEW_PROMPT.format(payload=json.dumps(payload, ensure_ascii=False, indent=2))
        try:
            self.llm_calls += 1
            raw = self.llm.invoke(prompt, temperature=0.0, max_tokens=2200)
        except Exception as exc:
            print(f"[paper_pipeline.reviewer] review LLM failed: {exc}")
            return None
        parsed = parse_json_object(raw)
        if not parsed:
            return None
        try:
            return ReviewReport(
                candidate_id=candidate.id,
                status=str(parsed.get("status") or "needs_revision"),
                schema_errors=[str(item) for item in parsed.get("schema_errors") or []],
                unsupported_claims=[str(item) for item in parsed.get("unsupported_claims") or []],
                evidence_quality=str(parsed.get("evidence_quality") or "unknown"),
                duplicate_candidates=[
                    item for item in (parsed.get("duplicate_candidates") or []) if isinstance(item, dict)
                ],
                merge_recommendation=parsed.get("merge_recommendation") if isinstance(parsed.get("merge_recommendation"), dict) else {},
            )
        except Exception:
            return None

    def _validated_or_fallback(
        self,
        candidate: DistilledCandidate,
        report: ReviewReport,
        deterministic: ReviewReport,
    ) -> ReviewReport:
        if report.status not in ALLOWED_STATUSES:
            return deterministic

        action = str((report.merge_recommendation or {}).get("action") or "")
        target_id = str((report.merge_recommendation or {}).get("target_card_id") or "")
        confidence = _float((report.merge_recommendation or {}).get("confidence"), 0.0)

        if action not in ALLOWED_ACTIONS:
            return deterministic
        if report.status == "approved" and not candidate.claims and candidate.candidate_type != "paper_page":
            return deterministic
        if action in TARGET_ACTIONS:
            if not target_id or not self.wiki_store.get_card(target_id):
                return deterministic
            if confidence < MIN_ACTION_CONFIDENCE:
                return deterministic
        if action == "create_new" and target_id:
            report.merge_recommendation["target_card_id"] = ""
        report.schema_errors = list(dict.fromkeys(deterministic.schema_errors + report.schema_errors))
        report.unsupported_claims = list(dict.fromkeys(deterministic.unsupported_claims + report.unsupported_claims))
        if report.schema_errors or report.unsupported_claims:
            report.status = deterministic.status
        return report

    def _should_use_llm(
        self,
        candidate: DistilledCandidate,
        schema_errors: list[str],
        unsupported_claims: list[str],
        duplicate: dict | None,
    ) -> bool:
        if not self.llm:
            return False
        if candidate.candidate_type == "paper_page":
            return False
        if schema_errors or unsupported_claims:
            return False
        if duplicate:
            return False
        return False

    @staticmethod
    def _schema_errors(candidate: DistilledCandidate) -> list[str]:
        errors = []
        if candidate.page_type not in CARD_TYPES:
            errors.append(f"invalid page_type: {candidate.page_type}")
        if candidate.candidate_type == "paper_page" and candidate.page_type != "PaperPage":
            errors.append("paper_page candidate must be PaperPage")
        if candidate.candidate_type == "concept_card" and candidate.page_type != "ConceptPage":
            errors.append("concept_card candidate must be ConceptPage")
        if candidate.candidate_type == "method_card" and candidate.page_type != "MethodPage":
            errors.append("method_card candidate must be MethodPage")
        if not sanitize_wiki_text(candidate.title):
            errors.append("title is empty")
        if len(sanitize_wiki_text(candidate.summary)) < 20:
            errors.append("summary too short")
        if candidate.candidate_type != "paper_page" and not candidate.aliases:
            errors.append("knowledge candidate has no aliases")
        return errors

    @staticmethod
    def _unsupported_claims(candidate: DistilledCandidate) -> list[str]:
        unsupported = []
        for claim in candidate.claims:
            if not sanitize_wiki_text(claim.claim) or not sanitize_wiki_text(claim.evidence):
                unsupported.append(claim.claim or "(empty claim)")
            if _looks_like_parser_garbage(claim.evidence):
                unsupported.append(claim.claim or "(parser garbage)")
        if candidate.candidate_type != "paper_page" and not candidate.claims:
            unsupported.append("knowledge candidate has no evidence-backed claims")
        return unsupported

    def _duplicate_candidate(self, candidate: DistilledCandidate) -> dict | None:
        aliases = list(dict.fromkeys([candidate.title] + candidate.aliases))
        alias_hit = self.pipeline_store.find_card_by_alias(aliases)
        if alias_hit:
            card = self.wiki_store.get_card(alias_hit["card_id"])
            return {
                "existing_card_id": alias_hit["card_id"],
                "existing_title": card.get("title", alias_hit["alias"]) if card else alias_hit["alias"],
                "confidence": 0.98,
                "reason": "normalized alias match",
            }
        existing = self.wiki_store.find_duplicate(
            title=candidate.title,
            page_type=candidate.page_type,
            source_urls=[] if candidate.candidate_type != "paper_page" else [],
        )
        if existing and candidate.candidate_type != "paper_page":
            return {
                "existing_card_id": existing["id"],
                "existing_title": existing["title"],
                "confidence": 0.9,
                "reason": "title dedupe match",
            }
        return None

    def _similar_cards(self, candidate: DistilledCandidate) -> list[dict[str, Any]]:
        queries = [candidate.title] + candidate.aliases[:4]
        cards: list[dict[str, Any]] = []
        seen: set[str] = set()
        for query in queries:
            for card in self.wiki_store.search_cards(query, limit=6):
                if card["id"] in seen or card.get("page_type") == "PaperPage":
                    continue
                seen.add(card["id"])
                cards.append({
                    "id": card["id"],
                    "title": card.get("title", ""),
                    "page_type": card.get("page_type", ""),
                    "summary": sanitize_wiki_text(card.get("summary", ""))[:300],
                    "aliases": (card.get("content_json") or {}).get("aliases", [])[:8]
                    if isinstance((card.get("content_json") or {}).get("aliases", []), list)
                    else [],
                })
                if len(cards) >= 8:
                    return cards
        return cards

    @staticmethod
    def _merge_recommendation(candidate: DistilledCandidate, duplicate: dict | None, status: str) -> dict:
        if status != "approved":
            return {"action": "needs_human_review", "target_card_id": "", "confidence": 0.0}
        if candidate.candidate_type == "paper_page":
            return {"action": "create_new", "target_card_id": "", "confidence": 1.0}
        if (
            duplicate
            and duplicate.get("confidence", 0) >= 0.98
            and str((candidate.content_json or {}).get("compile_status") or "") == "seeded_candidate"
        ):
            return {
                "action": "skip_duplicate",
                "target_card_id": duplicate["existing_card_id"],
                "confidence": duplicate.get("confidence", 0.98),
                "reason": "seeded candidate duplicates an existing aliased card",
            }
        if duplicate and duplicate.get("confidence", 0) >= 0.9:
            return {
                "action": "update_existing",
                "target_card_id": duplicate["existing_card_id"],
                "confidence": duplicate.get("confidence", 0.9),
                "reason": duplicate.get("reason", "duplicate existing card"),
            }
        return {"action": "create_new", "target_card_id": "", "confidence": 0.85}


def _looks_like_parser_garbage(text: str) -> bool:
    text = sanitize_wiki_text(str(text or ""))
    if len(text) < 256:
        return False
    if "\ufffd" in text:
        return True
    tokens = text.split()
    longest_token = max((len(token) for token in tokens), default=0)
    if longest_token < 160:
        return False
    compact = "".join(tokens)
    allowed = sum(1 for ch in compact if ch.isalnum() or ch in "+/=_-")
    return allowed / max(len(compact), 1) > 0.92


def _model_dump(model: Any) -> Any:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    if hasattr(model, "dict"):
        return model.dict()
    return model


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

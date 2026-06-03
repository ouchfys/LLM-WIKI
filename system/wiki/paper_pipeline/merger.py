from __future__ import annotations

import json
from typing import Any

from system.wiki.paper_pipeline.distiller import parse_json_object
from system.wiki.paper_pipeline.models import DistilledCandidate, MergePlan, MergeResult, ReviewReport, SourcePacket
from system.wiki.paper_pipeline.store import PaperWikiPipelineStore
from system.wiki.wiki_builder import sanitize_wiki_text
from system.wiki.wiki_store import WikiStore

ALLOWED_ACTIONS = {"create_new", "update_existing", "link_only", "skip_duplicate", "needs_human_review"}
ALLOWED_UPDATE_MODES = {"keep", "replace", "append", "append_list"}
ALLOWED_UPDATE_FIELDS = {"definition", "mechanism", "method", "findings", "limitations", "key_takeaways"}
ALLOWED_RELATION_TYPES = {"introduces", "uses", "extends", "compares", "evidence_for", "related"}
MIN_MERGE_CONFIDENCE = 0.8
HIGH_CONFIDENCE_DETERMINISTIC_MERGE = 0.95

MERGE_PROMPT = """\
You are the merge planner agent for a paper-to-wiki pipeline.
Return strict JSON only. Do not write markdown or explanations outside JSON.

Your job:
- Decide how an approved candidate should affect the Wiki.
- Produce a merge_plan only. Python will execute all database writes.
- Prefer append-only updates when incoming evidence extends an existing card.
- Use skip_duplicate when the target card already contains the same knowledge and only provenance should be recorded.
- Do not invent claims, metrics, datasets, authors, or results.

Allowed action values: create_new, update_existing, link_only, skip_duplicate, needs_human_review.
Allowed field update modes: keep, replace, append, append_list.

Return this exact shape:
{{
  "action": "update_existing",
  "target_card_id": "",
  "field_updates": {{
    "definition": {{"mode": "keep", "text": ""}},
    "mechanism": {{"mode": "append", "text": ""}},
    "method": {{"mode": "append", "text": ""}},
    "findings": {{"mode": "append", "text": ""}},
    "limitations": {{"mode": "append", "text": ""}},
    "key_takeaways": {{"mode": "append_list", "items": []}}
  }},
  "aliases_to_add": [],
  "links_to_add": [
    {{"to_card_id": "", "relation_type": "introduces", "reason": ""}}
  ],
  "reason": "",
  "confidence": 0.0
}}

Input:
{payload}
"""


def _fallback_merge_plan(report: ReviewReport) -> MergePlan:
    recommendation = report.merge_recommendation or {}
    action = recommendation.get("action") if recommendation.get("action") in ALLOWED_ACTIONS else "needs_human_review"
    return MergePlan(
        action=action,
        target_card_id=str(recommendation.get("target_card_id") or ""),
        reason=str(recommendation.get("reason") or "deterministic reviewer recommendation"),
        confidence=_float(recommendation.get("confidence"), 0.0),
    )


def _plan_from_payload(payload: dict[str, Any]) -> MergePlan | None:
    try:
        return MergePlan(
            action=str(payload.get("action") or "needs_human_review"),
            target_card_id=str(payload.get("target_card_id") or ""),
            field_updates=payload.get("field_updates") if isinstance(payload.get("field_updates"), dict) else {},
            aliases_to_add=[str(item) for item in payload.get("aliases_to_add") or [] if str(item).strip()],
            links_to_add=[item for item in payload.get("links_to_add") or [] if isinstance(item, dict)],
            reason=str(payload.get("reason") or ""),
            confidence=_float(payload.get("confidence"), 0.0),
        )
    except Exception:
        return None


def _validated_field_updates(field_updates: Any) -> dict[str, Any]:
    if not isinstance(field_updates, dict):
        return {}
    clean: dict[str, Any] = {}
    for field, update in field_updates.items():
        field = str(field)
        if field not in ALLOWED_UPDATE_FIELDS:
            continue
        if not isinstance(update, dict):
            continue
        mode = str(update.get("mode") or "")
        if mode not in ALLOWED_UPDATE_MODES:
            continue
        if mode == "append_list":
            items = update.get("items") if isinstance(update.get("items"), list) else []
            clean[field] = {
                "mode": mode,
                "items": [sanitize_wiki_text(str(item)) for item in items if str(item).strip()],
            }
        else:
            clean[field] = {"mode": mode, "text": sanitize_wiki_text(str(update.get("text") or ""))}
    return clean


def _apply_field_updates(content: dict[str, Any], field_updates: dict[str, Any]) -> dict[str, Any]:
    next_content = dict(content)
    for field, update in _validated_field_updates(field_updates).items():
        mode = update.get("mode")
        if mode == "keep":
            continue
        if mode == "replace":
            text = sanitize_wiki_text(update.get("text") or "")
            if text:
                next_content[field] = text
        elif mode == "append":
            text = sanitize_wiki_text(update.get("text") or "")
            if not text:
                continue
            existing = sanitize_wiki_text(str(next_content.get(field) or ""))
            if not existing:
                next_content[field] = text
            elif text not in existing:
                next_content[field] = f"{existing}\n\n{text}"
        elif mode == "append_list":
            next_content[field] = _unique_list(_as_list(next_content.get(field)) + _as_list(update.get("items")))
    return next_content


def _summarize_card(card: dict[str, Any] | None) -> dict[str, Any]:
    if not card:
        return {}
    return {
        "id": card.get("id", ""),
        "title": card.get("title", ""),
        "page_type": card.get("page_type", ""),
        "summary": sanitize_wiki_text(card.get("summary", ""))[:400],
        "related_topics": card.get("related_topics") or [],
    }


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


class PaperMergeAgent:
    def __init__(self, pipeline_store: PaperWikiPipelineStore, wiki_store: WikiStore, llm=None):
        self.pipeline_store = pipeline_store
        self.wiki_store = wiki_store
        self.llm = llm
        self.llm_calls = 0

    def merge(
        self,
        packet: SourcePacket,
        candidates: list[DistilledCandidate],
        reports: dict[str, ReviewReport],
    ) -> MergeResult:
        result = MergeResult()
        paper_candidate = next((item for item in candidates if item.candidate_type == "paper_page"), None)
        if paper_candidate:
            paper_report = reports.get(paper_candidate.id)
            result.paper_card_id = self._merge_paper(packet, paper_candidate, paper_report)
            self._record_merge_audit(
                packet=packet,
                candidate=paper_candidate,
                report=paper_report,
                plan=MergePlan(action="create_new", reason="paper page upsert", confidence=1.0),
                paper_card_id=result.paper_card_id,
                result_card_id=result.paper_card_id,
                status="applied",
                result=result,
            )

        for candidate in candidates:
            if candidate.candidate_type == "paper_page":
                continue
            report = reports.get(candidate.id)
            if not report or report.status != "approved":
                result.review_rejections.append({
                    "title": candidate.title,
                    "page_type": candidate.page_type,
                    "status": report.status if report else "missing_review",
                    "errors": (report.schema_errors if report else []) + (report.unsupported_claims if report else []),
                })
                self._record_merge_audit(
                    packet=packet,
                    candidate=candidate,
                    report=report,
                    plan=MergePlan(action="needs_human_review", reason="candidate did not pass review", confidence=0.0),
                    paper_card_id=result.paper_card_id,
                    result_card_id="",
                    status="rejected",
                    result=result,
                )
                continue
            plan = self._plan_merge(packet, candidate, report)
            action = plan.action
            target_id = plan.target_card_id
            if action == "update_existing" and target_id:
                card_id = self._update_existing_card(target_id, packet, candidate, result.paper_card_id, plan)
                _append_card_result(result.updated_cards, card_id, candidate, "updated", plan)
            elif action == "create_new":
                card_id = self._create_knowledge_card(packet, candidate, result.paper_card_id, plan)
                _append_card_result(result.created_cards, card_id, candidate, "created", plan)
            elif action == "link_only" and target_id:
                card_id = target_id
                self._add_sources(card_id, result.paper_card_id, packet, candidate)
            elif action == "skip_duplicate" and target_id:
                card_id = target_id
                self._add_sources(card_id, result.paper_card_id, packet, candidate)
            else:
                result.review_rejections.append({
                    "title": candidate.title,
                    "page_type": candidate.page_type,
                    "status": "needs_human_review",
                    "errors": ["merge recommendation was not actionable"],
                })
                self._record_merge_audit(
                    packet=packet,
                    candidate=candidate,
                    report=report,
                    plan=plan,
                    paper_card_id=result.paper_card_id,
                    result_card_id="",
                    status="needs_human_review",
                    result=result,
                )
                continue
            self._record_merge_audit(
                packet=packet,
                candidate=candidate,
                report=report,
                plan=plan,
                paper_card_id=result.paper_card_id,
                result_card_id=card_id,
                status="applied",
                result=result,
            )
            self.pipeline_store.add_card_link(
                from_card_id=result.paper_card_id,
                to_card_id=card_id,
                relation_type="introduces" if candidate.page_type == "ConceptPage" else "uses",
                source_packet_id=packet.source_id,
                evidence_text=_first_evidence(candidate),
            )
            for extra_link in plan.links_to_add:
                to_card_id = str(extra_link.get("to_card_id") or "")
                relation_type = str(extra_link.get("relation_type") or "related")
                if relation_type not in ALLOWED_RELATION_TYPES:
                    relation_type = "related"
                if not to_card_id or to_card_id == result.paper_card_id or not self.wiki_store.get_card(to_card_id):
                    continue
                self.pipeline_store.add_card_link(
                    from_card_id=card_id,
                    to_card_id=to_card_id,
                    relation_type=relation_type,
                    source_packet_id=packet.source_id,
                    evidence_text=str(extra_link.get("reason") or ""),
                )
            _append_link_result(
                result.linked_cards,
                from_card_id=result.paper_card_id,
                to_card_id=card_id,
                candidate=candidate,
                relation_type="introduces" if candidate.page_type == "ConceptPage" else "uses",
            )

        if result.paper_card_id:
            self._write_import_impact(result.paper_card_id, result)
        return result

    def _merge_paper(
        self,
        packet: SourcePacket,
        candidate: DistilledCandidate,
        report: ReviewReport | None,
    ) -> str:
        content = dict(candidate.content_json or {})
        content.update({
            "schema_version": content.get("schema_version") or "paper-wiki-v1",
            "compile_status": content.get("compile_status") or "llm_refined",
            "pipeline": "four_agent",
            "source_packet_id": packet.source_id,
            "raw_source_path": packet.raw_source_path,
            "pdf_storage_uri": packet.pdf_storage_uri,
            "parser_used": packet.parser_used,
            "aliases": candidate.aliases,
            "review_status": report.status if report else "missing",
        })
        existing = self.wiki_store.find_duplicate(candidate.title, "PaperPage", packet.source_urls)
        card_id = self.wiki_store.create_card(
            title=candidate.title,
            page_type="PaperPage",
            content_json=content,
            summary=candidate.summary[:360],
            source_level="primary",
            source_urls=packet.source_urls,
            related_topics=candidate.related_topics,
        )
        self.pipeline_store.add_aliases(card_id, [candidate.title] + candidate.aliases)
        self.pipeline_store.add_card_source(
            card_id=card_id,
            source_packet_id=packet.source_id,
            source_card_id=card_id,
            raw_source_path=packet.raw_source_path,
            source_url=packet.source_urls[0] if packet.source_urls else "",
            section_id=candidate.claims[0].section_id if candidate.claims else "",
            evidence_text=_first_evidence(candidate),
            claim_text=candidate.claims[0].claim if candidate.claims else candidate.summary,
            confidence=1.0,
        )
        return card_id

    def _create_knowledge_card(
        self,
        packet: SourcePacket,
        candidate: DistilledCandidate,
        paper_card_id: str,
        plan: MergePlan | None = None,
    ) -> str:
        content = self._knowledge_content(packet, candidate, existing={}, plan=plan)
        card_id = self.wiki_store.create_card(
            title=candidate.title,
            page_type=candidate.page_type,
            content_json=content,
            summary=candidate.summary[:280],
            source_level=candidate.source_level or "primary",
            source_urls=[],
            related_topics=candidate.related_topics,
        )
        aliases = [candidate.title] + candidate.aliases + ((plan.aliases_to_add if plan else []) or [])
        self.pipeline_store.add_aliases(card_id, aliases)
        self._add_sources(card_id, paper_card_id, packet, candidate)
        return card_id

    def _update_existing_card(
        self,
        card_id: str,
        packet: SourcePacket,
        candidate: DistilledCandidate,
        paper_card_id: str,
        plan: MergePlan | None = None,
    ) -> str:
        existing = self.wiki_store.get_card(card_id) or {}
        content = self._knowledge_content(packet, candidate, existing=existing.get("content_json") or {}, plan=plan)
        summary = _merge_summary(existing.get("summary", ""), candidate.summary)
        related_topics = _unique_list((existing.get("related_topics") or []) + candidate.related_topics)
        source_urls = existing.get("source_urls") or []
        self.wiki_store.update_card(
            card_id,
            summary=summary[:360],
            content_json=content,
            source_urls_json=source_urls,
            related_topics_json=related_topics,
        )
        aliases = [candidate.title] + candidate.aliases + ((plan.aliases_to_add if plan else []) or [])
        self.pipeline_store.add_aliases(card_id, aliases)
        self._add_sources(card_id, paper_card_id, packet, candidate)
        return card_id

    def _knowledge_content(
        self,
        packet: SourcePacket,
        candidate: DistilledCandidate,
        existing: dict[str, Any],
        plan: MergePlan | None = None,
    ) -> dict[str, Any]:
        content = dict(existing or {})
        incoming = dict(candidate.content_json or {})
        content.setdefault("schema_version", "paper-wiki-v1")
        content["compile_status"] = "merged_knowledge"
        use_planned_updates = bool(plan and plan.field_updates and existing)
        if not use_planned_updates:
            content["definition"] = _prefer_longer(content.get("definition", ""), incoming.get("definition", "") or candidate.summary)
            content["mechanism"] = _prefer_longer(content.get("mechanism", ""), incoming.get("mechanism", ""))
            content["method"] = _prefer_longer(content.get("method", ""), incoming.get("method", ""))
            content["findings"] = _prefer_longer(content.get("findings", ""), incoming.get("findings", ""))
            content["limitations"] = _prefer_longer(content.get("limitations", ""), incoming.get("limitations", ""))
            content["key_takeaways"] = _unique_list(_as_list(content.get("key_takeaways")) + _as_list(incoming.get("key_takeaways")))
        if plan and plan.field_updates:
            content = _apply_field_updates(content, plan.field_updates)
        content["aliases"] = _unique_list(_as_list(content.get("aliases")) + [candidate.title] + candidate.aliases)
        if plan:
            content["aliases"] = _unique_list(_as_list(content.get("aliases")) + plan.aliases_to_add)
        content["source_packet_ids"] = _unique_list(_as_list(content.get("source_packet_ids")) + [packet.source_id])
        content.setdefault("evidence_updates", [])
        updates = _as_list(content.get("evidence_updates"))
        updates.append({
            "source_packet_id": packet.source_id,
            "source_title": packet.title,
            "claim": candidate.claims[0].claim if candidate.claims else candidate.summary,
            "evidence": _first_evidence(candidate),
            "merge_action": plan.action if plan else "",
            "merge_reason": plan.reason if plan else "",
            "merge_confidence": plan.confidence if plan else 0.0,
        })
        content["evidence_updates"] = updates[-12:]
        history = _as_list(content.get("merge_history"))
        history.append({
            "source_packet_id": packet.source_id,
            "candidate_id": candidate.id,
            "candidate_title": candidate.title,
            "action": plan.action if plan else "",
            "reason": plan.reason if plan else "",
            "confidence": plan.confidence if plan else 0.0,
        })
        content["merge_history"] = history[-20:]
        return content

    def _plan_merge(self, packet: SourcePacket, candidate: DistilledCandidate, report: ReviewReport) -> MergePlan:
        fallback = _fallback_merge_plan(report)
        if not self.llm or fallback.action == "create_new" or not fallback.target_card_id:
            return fallback
        target_card = self.wiki_store.get_card(fallback.target_card_id) if fallback.target_card_id else None
        if fallback.confidence >= HIGH_CONFIDENCE_DETERMINISTIC_MERGE:
            return fallback
        payload = {
            "approved_candidate": _model_dump(candidate),
            "reviewer_report": _model_dump(report),
            "target_existing_card": _summarize_card(target_card),
            "existing_card_content_json": (target_card or {}).get("content_json") or {},
            "existing_evidence_summaries": self._evidence_for_card(fallback.target_card_id),
            "incoming_evidence": [_model_dump(claim) for claim in candidate.claims],
            "source_paper_title": packet.title,
            "existing_aliases": ((target_card or {}).get("content_json") or {}).get("aliases", []),
        }
        prompt = MERGE_PROMPT.format(payload=json.dumps(payload, ensure_ascii=False, indent=2))
        try:
            self.llm_calls += 1
            raw = self.llm.invoke(prompt, temperature=0.0, max_tokens=3600)
        except Exception as exc:
            print(f"[paper_pipeline.merger] merge planner LLM failed: {exc}")
            return fallback
        parsed = parse_json_object(raw)
        if not parsed:
            return fallback
        plan = _plan_from_payload(parsed)
        if not plan:
            return fallback
        return self._validated_plan(plan, fallback)

    def _validated_plan(self, plan: MergePlan, fallback: MergePlan) -> MergePlan:
        if plan.action not in ALLOWED_ACTIONS:
            return fallback
        if fallback.action in {"update_existing", "link_only", "skip_duplicate"} and fallback.target_card_id:
            if plan.action == "create_new":
                return fallback
            if plan.action in {"update_existing", "link_only", "skip_duplicate"} and plan.target_card_id != fallback.target_card_id:
                return fallback
        elif plan.action in {"update_existing", "link_only", "skip_duplicate"}:
            return fallback
        if plan.action in {"update_existing", "link_only", "skip_duplicate"}:
            if not plan.target_card_id or not self.wiki_store.get_card(plan.target_card_id):
                return fallback
            if plan.confidence < MIN_MERGE_CONFIDENCE:
                return fallback
        if plan.action == "create_new":
            plan.target_card_id = ""
        plan.field_updates = _validated_field_updates(plan.field_updates)
        plan.links_to_add = [
            item for item in plan.links_to_add
            if isinstance(item, dict) and str(item.get("relation_type") or "related") in ALLOWED_RELATION_TYPES
        ]
        return plan

    def _evidence_for_card(self, card_id: str) -> list[dict[str, Any]]:
        if not card_id:
            return []
        links = self.pipeline_store.list_card_links(card_id)
        return [
            {
                "source_packet_id": item.get("source_packet_id", ""),
                "source_card_title": item.get("source_card_title", ""),
                "section_id": item.get("section_id", ""),
                "claim_text": sanitize_wiki_text(item.get("claim_text", ""))[:300],
                "evidence_text": sanitize_wiki_text(item.get("evidence_text", ""))[:500],
            }
            for item in links.get("sources", [])[:8]
        ]

    def _add_sources(self, card_id: str, paper_card_id: str, packet: SourcePacket, candidate: DistilledCandidate) -> None:
        for claim in candidate.claims[:5]:
            self.pipeline_store.add_card_source(
                card_id=card_id,
                source_packet_id=packet.source_id,
                source_card_id=paper_card_id,
                raw_source_path=packet.raw_source_path,
                source_url=packet.source_urls[0] if packet.source_urls else "",
                section_id=claim.section_id,
                evidence_text=claim.evidence,
                claim_text=claim.claim,
                confidence=1.0,
            )

    def _record_merge_audit(
        self,
        *,
        packet: SourcePacket,
        candidate: DistilledCandidate,
        report: ReviewReport | None,
        plan: MergePlan,
        paper_card_id: str,
        result_card_id: str,
        status: str,
        result: MergeResult,
    ) -> None:
        plan_payload = _model_dump(plan)
        report_payload = _model_dump(report) if report else {}
        audit_id = self.pipeline_store.add_merge_audit(
            source_packet_id=packet.source_id,
            paper_card_id=paper_card_id,
            candidate_id=candidate.id,
            candidate_title=candidate.title,
            candidate_type=candidate.candidate_type,
            action=plan.action,
            target_card_id=plan.target_card_id,
            result_card_id=result_card_id,
            status=status,
            plan=plan_payload,
            report=report_payload,
            evidence_text=_first_evidence(candidate),
        )
        result.merge_audit.append({
            "id": audit_id,
            "source_packet_id": packet.source_id,
            "candidate_id": candidate.id,
            "title": candidate.title,
            "page_type": candidate.page_type,
            "action": plan.action,
            "target_card_id": plan.target_card_id,
            "result_card_id": result_card_id,
            "status": status,
            "reason": plan.reason,
            "confidence": plan.confidence,
        })

    def _write_import_impact(self, paper_card_id: str, result: MergeResult) -> None:
        card = self.wiki_store.get_card(paper_card_id)
        if not card:
            return
        content = dict(card.get("content_json") or {})
        content["import_impact"] = {
            "created_cards": result.created_cards,
            "updated_cards": result.updated_cards,
            "linked_cards": result.linked_cards,
            "review_rejections": result.review_rejections,
            "merge_audit": result.merge_audit,
        }
        self.wiki_store.update_card(paper_card_id, content_json=content)


def _first_evidence(candidate: DistilledCandidate) -> str:
    return candidate.claims[0].evidence if candidate.claims else candidate.summary


def _card_result(card_id: str, candidate: DistilledCandidate, action: str, plan: MergePlan | None = None) -> dict[str, Any]:
    item = {
        "id": card_id,
        "title": candidate.title,
        "page_type": candidate.page_type,
        "action": action,
    }
    if plan:
        item["merge_reason"] = plan.reason
        item["merge_confidence"] = plan.confidence
    return item


def _append_card_result(
    items: list[dict[str, Any]],
    card_id: str,
    candidate: DistilledCandidate,
    action: str,
    plan: MergePlan | None = None,
) -> None:
    if any(item.get("id") == card_id and item.get("action") == action for item in items):
        return
    items.append(_card_result(card_id, candidate, action, plan))


def _append_link_result(
    items: list[dict[str, Any]],
    from_card_id: str,
    to_card_id: str,
    candidate: DistilledCandidate,
    relation_type: str,
) -> None:
    if any(
        item.get("from") == from_card_id
        and item.get("to") == to_card_id
        and item.get("relation_type") == relation_type
        for item in items
    ):
        return
    items.append({
        "from": from_card_id,
        "to": to_card_id,
        "title": candidate.title,
        "page_type": candidate.page_type,
        "relation_type": relation_type,
    })


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _unique_list(items: list[Any]) -> list[Any]:
    seen = set()
    result = []
    for item in items:
        key = str(item).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _prefer_longer(a: Any, b: Any) -> str:
    a_text = sanitize_wiki_text(str(a or ""))
    b_text = sanitize_wiki_text(str(b or ""))
    return b_text if len(b_text) > len(a_text) else a_text


def _merge_summary(existing: str, incoming: str) -> str:
    existing = sanitize_wiki_text(existing)
    incoming = sanitize_wiki_text(incoming)
    if not existing:
        return incoming
    if not incoming or incoming in existing:
        return existing
    return f"{existing}\n\n补充：{incoming}"

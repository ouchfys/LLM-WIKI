from __future__ import annotations

from pathlib import Path
from typing import Any

from system.wiki.maintenance.store import WikiMaintenanceStore
from system.wiki.ingestion_jobs import IngestionJobStore
from system.wiki.paper_pipeline.distiller import parse_json_object
from system.wiki.paper_pipeline.store import PaperWikiPipelineStore
from system.wiki.wiki_builder import sanitize_wiki_text
from system.wiki.wiki_store import WikiStore


ALLOWED_UPDATE_FIELDS = {
    "definition",
    "mechanism",
    "method",
    "findings",
    "limitations",
    "key_takeaways",
    "aliases",
    "evidence_updates",
    "maintenance_notes",
}

CREATE_CARD_TYPES = {
    "concept_card": "ConceptPage",
    "method_card": "MethodPage",
    "interview_note": "InterviewQA",
    "comparison": "ComparePage",
    "open_question": "SourceNote",
    "claim": "SourceNote",
    "source_note": "SourceNote",
}


CANDIDATE_REVIEW_PROMPT = """\
You are the Reviewer and Merge Planner for maintenance candidates in an
LLM-maintained research wiki.

Return strict JSON only. Do not write markdown.

Your job:
- Review whether the candidate is evidence-backed and safe to merge.
- Produce a merge plan only. Python will execute only whitelisted operations.
- Do not invent facts or cite sources not present in the candidate/context.

Allowed review_status values:
- approved
- reviewed
- quarantined

Allowed action values:
- maintenance_card_update
- create_wiki_card
- keep_staged
- quarantine

Return this shape:
{
  "review_status": "approved",
  "reason": "",
  "risk": "low|medium|high",
  "unsupported_claims": [],
  "merge_plan": {
    "action": "maintenance_card_update|create_wiki_card",
    "target_card_id": "",
    "page_type": "",
    "title": "",
    "summary": "",
    "content_json": {},
    "related_topics": []
  }
}

Input:
__PAYLOAD__
"""


class MaintenanceCandidateProcessor:
    """Review and merge staged maintenance candidates conservatively.

    This is a gate, not a free writer. It only auto-applies low-risk card_update
    candidates that target an existing card and contain some evidence basis.
    Other candidate types remain staged for later specialized ingestion/review.
    """

    def __init__(self, db_path: str | Path | None = None, llm: Any = None):
        repo_root = Path(__file__).resolve().parents[3]
        self.db_path = str(Path(db_path) if db_path else repo_root / "sessions.db")
        self.llm = llm
        self.store = WikiMaintenanceStore(db_path=self.db_path)
        self.wiki_store = WikiStore(db_path=self.db_path)
        self.pipeline_store = PaperWikiPipelineStore(db_path=self.db_path)

    def process_pending(self, *, limit: int = 10, auto_merge: bool = True) -> dict[str, Any]:
        items = self.store.list_candidates(status="candidate_ready", limit=limit)
        results = []
        for item in items:
            results.append(self.process_one(item, auto_merge=auto_merge))
        return {
            "ok": True,
            "processed": len(results),
            "items": results,
        }

    def process_one(self, item: dict[str, Any], *, auto_merge: bool = True) -> dict[str, Any]:
        candidate_id = str(item.get("id") or "")
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        deterministic_review = self._review(payload)
        candidate_type = str(payload.get("candidate_type") or "")
        content = payload.get("content_json") if isinstance(payload.get("content_json"), dict) else {}
        is_user_selection = str(content.get("source_type") or "") == "user_selection"
        llm_review = (
            self._llm_review(payload, deterministic_review)
            if self.llm and candidate_type != "web_source_candidate" and not is_user_selection
            else {}
        )
        review = self._combine_reviews(deterministic_review, llm_review)
        if review["status"] != "approved":
            self.store.update_candidate(candidate_id, status=review["status"], review_result=review)
            return {"id": candidate_id, "status": review["status"], "review": review, "merge": {}}
        if not auto_merge:
            self.store.update_candidate(candidate_id, status="reviewed", review_result=review)
            return {"id": candidate_id, "status": "reviewed", "review": review, "merge": {}}
        if candidate_type == "card_update":
            merge = self._merge_card_update(candidate_id, payload, review.get("merge_plan") or {})
        elif candidate_type == "web_source_candidate":
            merge = self._queue_web_source(candidate_id, payload)
        else:
            merge = self._merge_create_card(candidate_id, payload, review.get("merge_plan") or {})
        status = str(merge.get("candidate_status") or ("merged" if merge.get("ok") else "reviewed"))
        self.store.update_candidate(
            candidate_id,
            status=status,
            review_result=review,
            merge_result=merge,
        )
        return {"id": candidate_id, "status": status, "review": review, "merge": merge}

    def _llm_review(self, payload: dict[str, Any], deterministic_review: dict[str, Any]) -> dict[str, Any]:
        target_card_id = str(payload.get("target_card_id") or "")
        target_card = self.wiki_store.get_card(target_card_id) if target_card_id else None
        context = {
            "candidate": payload,
            "deterministic_review": deterministic_review,
            "target_card": {
                "id": target_card.get("id", "") if target_card else "",
                "title": target_card.get("title", "") if target_card else "",
                "page_type": target_card.get("page_type", "") if target_card else "",
                "summary": sanitize_wiki_text(target_card.get("summary", ""))[:800] if target_card else "",
                "content_json": target_card.get("content_json", {}) if target_card else {},
                "related_topics": target_card.get("related_topics", []) if target_card else [],
            },
        }
        prompt = CANDIDATE_REVIEW_PROMPT.replace("__PAYLOAD__", _json(context))
        try:
            raw = self.llm.invoke(prompt, temperature=0.0, max_tokens=2200)
        except Exception as exc:
            return {"review_status": "reviewed", "reason": f"llm review failed: {exc}"}
        parsed = parse_json_object(raw)
        return parsed if isinstance(parsed, dict) else {"review_status": "reviewed", "reason": "invalid llm review json"}

    def _combine_reviews(self, deterministic: dict[str, Any], llm_review: dict[str, Any]) -> dict[str, Any]:
        if not llm_review:
            review = dict(deterministic)
            review["reviewer"] = "deterministic"
            return review
        if deterministic.get("status") != "approved":
            review = dict(deterministic)
            review["llm_review"] = llm_review
            review["reviewer"] = "deterministic_guardrail"
            return review
        review_status = str(llm_review.get("review_status") or "reviewed")
        action = str((llm_review.get("merge_plan") or {}).get("action") or "")
        allowed_action = "maintenance_card_update" if str((deterministic.get("candidate_type") or "")) == "card_update" else "create_wiki_card"
        if review_status != "approved" or action != allowed_action:
            return {
                "status": "reviewed" if review_status != "quarantined" else "quarantined",
                "reason": str(llm_review.get("reason") or "llm did not approve merge"),
                "auto_merge_allowed": False,
                "llm_review": llm_review,
                "reviewer": "llm",
            }
        merge_plan = self._sanitize_merge_plan(llm_review.get("merge_plan") or {}, allowed_action=allowed_action)
        if not merge_plan:
            return {
                "status": "reviewed",
                "reason": "llm merge plan was not executable",
                "auto_merge_allowed": False,
                "llm_review": llm_review,
                "reviewer": "llm",
            }
        return {
            "status": "approved",
            "reason": str(llm_review.get("reason") or deterministic.get("reason") or ""),
            "risk": str(llm_review.get("risk") or deterministic.get("risk") or "medium"),
            "unsupported_claims": llm_review.get("unsupported_claims") if isinstance(llm_review.get("unsupported_claims"), list) else [],
            "auto_merge_allowed": True,
            "merge_plan": merge_plan,
            "llm_review": llm_review,
            "reviewer": "llm_plus_deterministic_guardrail",
        }

    @staticmethod
    def _sanitize_merge_plan(plan: dict[str, Any], *, allowed_action: str) -> dict[str, Any]:
        if str(plan.get("action") or "") != allowed_action:
            return {}
        content = plan.get("content_json") if isinstance(plan.get("content_json"), dict) else {}
        safe_content = {
            key: value
            for key, value in content.items()
            if key in ALLOWED_UPDATE_FIELDS
        }
        page_type = str(plan.get("page_type") or "")
        if page_type and page_type not in set(CREATE_CARD_TYPES.values()):
            page_type = ""
        return {
            "action": allowed_action,
            "target_card_id": str(plan.get("target_card_id") or ""),
            "page_type": page_type,
            "title": sanitize_wiki_text(str(plan.get("title") or ""))[:180],
            "summary": sanitize_wiki_text(str(plan.get("summary") or ""))[:800],
            "content_json": safe_content,
            "related_topics": _as_list(plan.get("related_topics"))[:20],
        }

    def _review(self, payload: dict[str, Any]) -> dict[str, Any]:
        candidate_type = str(payload.get("candidate_type") or "")
        if candidate_type == "web_source_candidate":
            return self._review_web_source(payload)
        if candidate_type != "card_update" and candidate_type not in CREATE_CARD_TYPES:
            return {
                "status": "reviewed",
                "reason": f"{candidate_type or 'unknown'} requires specialized ingestion or manual promotion",
                "auto_merge_allowed": False,
                "candidate_type": candidate_type,
            }
        if candidate_type in CREATE_CARD_TYPES:
            return self._review_create_card(payload, candidate_type)
        target_card_id = str(payload.get("target_card_id") or "")
        if not target_card_id:
            return {"status": "quarantined", "reason": "missing target_card_id", "auto_merge_allowed": False}
        card = self.wiki_store.get_card(target_card_id)
        if not card:
            return {"status": "quarantined", "reason": "target card does not exist", "auto_merge_allowed": False}
        risk = str(payload.get("risk") or "medium")
        if risk == "high":
            return {"status": "reviewed", "reason": "high-risk update requires stronger review", "auto_merge_allowed": False}
        evidence = payload.get("evidence_basis") if isinstance(payload.get("evidence_basis"), list) else []
        if not evidence:
            return {"status": "quarantined", "reason": "missing evidence_basis", "auto_merge_allowed": False}
        changes = payload.get("changes") if isinstance(payload.get("changes"), dict) else {}
        if not changes:
            return {"status": "quarantined", "reason": "empty changes", "auto_merge_allowed": False}
        unsafe_fields = [
            key for key in (changes.get("content_json") or {}).keys()
            if key not in ALLOWED_UPDATE_FIELDS
        ] if isinstance(changes.get("content_json"), dict) else []
        if unsafe_fields:
            return {
                "status": "reviewed",
                "reason": f"unsupported update fields: {', '.join(unsafe_fields)}",
                "auto_merge_allowed": False,
            }
        return {
            "status": "approved",
            "reason": "low-risk evidence-backed card_update",
            "auto_merge_allowed": True,
            "candidate_type": candidate_type,
        }

    def _review_create_card(self, payload: dict[str, Any], candidate_type: str) -> dict[str, Any]:
        title = sanitize_wiki_text(str(payload.get("title") or ""))
        summary = sanitize_wiki_text(str(payload.get("summary") or ""))
        content = payload.get("content_json") if isinstance(payload.get("content_json"), dict) else {}
        evidence = content.get("evidence") if isinstance(content.get("evidence"), list) else []
        if not title:
            return {"status": "quarantined", "reason": "missing title", "auto_merge_allowed": False, "candidate_type": candidate_type}
        if len(summary) < 40 and not content:
            return {"status": "quarantined", "reason": "weak summary and empty content", "auto_merge_allowed": False, "candidate_type": candidate_type}
        if candidate_type not in {"open_question", "source_note"} and not evidence:
            return {"status": "quarantined", "reason": "missing evidence in content_json.evidence", "auto_merge_allowed": False, "candidate_type": candidate_type}
        return {
            "status": "approved",
            "reason": "evidence-backed staged knowledge candidate",
            "auto_merge_allowed": True,
            "candidate_type": candidate_type,
        }

    def _review_web_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
        title = sanitize_wiki_text(str(source.get("title") or ""))
        url = str(source.get("url") or "").strip()
        source_candidate_uri = str(source.get("source_candidate_uri") or "").strip()
        confidence = str(source.get("confidence") or "low")
        if not title or not url:
            return {
                "status": "quarantined",
                "reason": "web source candidate missing title or url",
                "auto_merge_allowed": False,
                "candidate_type": "web_source_candidate",
            }
        if confidence == "low":
            return {
                "status": "reviewed",
                "reason": "low-confidence web source remains staged",
                "auto_merge_allowed": False,
                "candidate_type": "web_source_candidate",
            }
        if not source_candidate_uri:
            return {
                "status": "quarantined",
                "reason": "missing source_candidate_uri",
                "auto_merge_allowed": False,
                "candidate_type": "web_source_candidate",
            }
        return {
            "status": "approved",
            "reason": "web source candidate can enter source ingestion queue",
            "auto_merge_allowed": True,
            "candidate_type": "web_source_candidate",
        }

    def _merge_card_update(
        self,
        candidate_id: str,
        payload: dict[str, Any],
        merge_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        target_card_id = str(payload.get("target_card_id") or "")
        card = self.wiki_store.get_card(target_card_id)
        if not card:
            return {"ok": False, "reason": "target card disappeared before merge"}
        changes = payload.get("changes") if isinstance(payload.get("changes"), dict) else {}
        if merge_plan:
            planned_changes: dict[str, Any] = {}
            if merge_plan.get("summary"):
                planned_changes["summary"] = merge_plan.get("summary")
            if merge_plan.get("content_json"):
                planned_changes["content_json"] = merge_plan.get("content_json")
            if merge_plan.get("related_topics"):
                planned_changes["related_topics"] = merge_plan.get("related_topics")
            changes = _merge_change_dicts(changes, planned_changes)
        existing_content = card.get("content_json") if isinstance(card.get("content_json"), dict) else {}
        incoming_content = changes.get("content_json") if isinstance(changes.get("content_json"), dict) else {}
        next_content = dict(existing_content)
        for field, value in incoming_content.items():
            if field not in ALLOWED_UPDATE_FIELDS:
                continue
            next_content[field] = _merge_value(next_content.get(field), value)
        evidence = payload.get("evidence_basis") if isinstance(payload.get("evidence_basis"), list) else []
        if evidence:
            updates = _as_list(next_content.get("evidence_updates"))
            updates.append({
                "source": "maintenance_candidate",
                "candidate_id": candidate_id,
                "items": evidence[:8],
                "reason": payload.get("reason", ""),
            })
            next_content["evidence_updates"] = updates[-20:]
        notes = _as_list(next_content.get("maintenance_notes"))
        note = sanitize_wiki_text(str(payload.get("review_notes") or payload.get("reason") or ""))
        if note:
            notes.append(note)
            next_content["maintenance_notes"] = _unique(notes)[-20:]

        next_summary = sanitize_wiki_text(str((changes.get("summary") or card.get("summary") or "")))
        if not next_summary:
            next_summary = card.get("summary", "")
        next_related = _unique(_as_list(card.get("related_topics")) + _as_list(changes.get("related_topics")))
        self.wiki_store.update_card(
            target_card_id,
            summary=next_summary[:500],
            content_json=next_content,
            related_topics_json=next_related,
        )
        audit_id = self.pipeline_store.add_merge_audit(
            source_packet_id=str(payload.get("source_repair_task_id") or payload.get("source_query_id") or "maintenance"),
            action="maintenance_card_update",
            candidate_id=candidate_id,
            candidate_title=str(payload.get("title") or card.get("title") or ""),
            candidate_type=str(payload.get("candidate_type") or "card_update"),
            target_card_id=target_card_id,
            result_card_id=target_card_id,
            status="applied",
            plan={"source": "maintenance_candidate_processor"},
            report={"status": "approved"},
            evidence_text=sanitize_wiki_text(str(evidence[0].get("fact") if isinstance(evidence[0], dict) else evidence[0])) if evidence else "",
        )
        return {
            "ok": True,
            "action": "maintenance_card_update",
            "target_card_id": target_card_id,
            "audit_id": audit_id,
        }

    def _merge_create_card(
        self,
        candidate_id: str,
        payload: dict[str, Any],
        merge_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        candidate_type = str(payload.get("candidate_type") or "")
        page_type = str((merge_plan or {}).get("page_type") or CREATE_CARD_TYPES.get(candidate_type) or "SourceNote")
        title = sanitize_wiki_text(str((merge_plan or {}).get("title") or payload.get("title") or ""))[:180]
        summary = sanitize_wiki_text(str((merge_plan or {}).get("summary") or payload.get("summary") or ""))[:500]
        content = payload.get("content_json") if isinstance(payload.get("content_json"), dict) else {}
        planned_content = (merge_plan or {}).get("content_json") if isinstance((merge_plan or {}).get("content_json"), dict) else {}
        content_json = {
            **content,
            **planned_content,
            "schema_version": content.get("schema_version") or "maintenance-candidate-v1",
            "compile_status": "maintenance_merged",
            "maintenance_candidate_id": candidate_id,
            "maintenance_candidate_type": candidate_type,
        }
        related_topics = _unique(_as_list(payload.get("related_topics")) + _as_list((merge_plan or {}).get("related_topics")))
        source_urls = []
        artifact_uri = str(content_json.get("artifact_uri") or "")
        if artifact_uri:
            source_urls.append(artifact_uri)
        card_id = self.wiki_store.create_card(
            title=title,
            page_type=page_type,
            content_json=content_json,
            summary=summary,
            source_level="secondary" if candidate_type in {"interview_note", "open_question"} else "source_reported",
            source_urls=source_urls,
            related_topics=related_topics,
        )
        aliases = _as_list(payload.get("aliases"))
        if title:
            aliases = _unique([title] + aliases)
        if aliases:
            self.pipeline_store.add_aliases(card_id, [str(item) for item in aliases])
        evidence = content_json.get("evidence") if isinstance(content_json.get("evidence"), list) else []
        source_query_id = str(content_json.get("source_query_id") or payload.get("source_id") or "maintenance")
        evidence_text = sanitize_wiki_text(str(evidence[0]))[:800] if evidence else ""
        if artifact_uri or evidence_text:
            self.pipeline_store.add_card_source(
                card_id=card_id,
                source_packet_id="",
                source_url=artifact_uri,
                evidence_text=evidence_text,
                claim_text=summary or sanitize_wiki_text(str(content_json.get("key_idea") or ""))[:500],
                confidence=0.85,
            )
        linked_card_ids = self._link_related_cards(
            card_id=card_id,
            related_topics=related_topics,
            source_id=source_query_id,
            evidence_text=evidence_text,
        )
        audit_id = self.pipeline_store.add_merge_audit(
            source_packet_id=source_query_id,
            action="maintenance_create_card",
            candidate_id=candidate_id,
            candidate_title=title,
            candidate_type=candidate_type,
            result_card_id=card_id,
            status="applied",
            plan={
                "source": "maintenance_candidate_processor",
                "page_type": page_type,
                "linked_card_ids": linked_card_ids,
            },
            report={"status": "approved"},
            evidence_text=evidence_text,
        )
        return {
            "ok": True,
            "action": "maintenance_create_card",
            "result_card_id": card_id,
            "audit_id": audit_id,
            "linked_card_ids": linked_card_ids,
        }

    def _link_related_cards(
        self,
        *,
        card_id: str,
        related_topics: list[Any],
        source_id: str,
        evidence_text: str,
    ) -> list[str]:
        linked: list[str] = []
        for topic in related_topics:
            topic_text = sanitize_wiki_text(str(topic or ""))
            if not topic_text:
                continue
            target_id = self._find_related_card_id(topic_text)
            if not target_id or target_id == card_id or target_id in linked:
                continue
            self.pipeline_store.add_card_link(
                from_card_id=card_id,
                to_card_id=target_id,
                relation_type="synthesizes",
                source_packet_id=source_id,
                evidence_text=evidence_text,
            )
            linked.append(target_id)
        return linked

    def _find_related_card_id(self, topic: str) -> str:
        alias_match = self.pipeline_store.find_card_by_alias([topic])
        if alias_match and alias_match.get("card_id"):
            return str(alias_match.get("card_id") or "")
        for card in self.wiki_store.search_cards(topic, limit=5):
            if str(card.get("title") or "").strip().lower() == topic.strip().lower():
                return str(card.get("id") or "")
        cards = self.wiki_store.search_cards(topic, limit=1)
        if cards:
            return str(cards[0].get("id") or "")
        return ""

    def _queue_web_source(self, candidate_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
        source_uri = str(source.get("source_candidate_uri") or source.get("url") or "").strip()
        if not source_uri:
            return {"ok": False, "reason": "missing source_uri for web source candidate"}
        job = IngestionJobStore(db_path=self.db_path).create_job(
            source_type="web_source",
            source_uri=source_uri,
            stage="queued",
            metadata={
                "candidate_id": candidate_id,
                "pipeline": "maintenance_web_update",
                "source_url": source.get("url", ""),
                "source_title": source.get("title", ""),
                "source_type": source.get("source_type", ""),
                "expected_wiki_impact": source.get("expected_wiki_impact", ""),
                "confidence": source.get("confidence", ""),
                "topic": payload.get("topic", ""),
            },
        )
        return {
            "ok": True,
            "action": "queue_web_source_ingestion",
            "candidate_status": "source_queued",
            "job_id": job.get("id", ""),
            "source_uri": source_uri,
        }


def _merge_value(existing: Any, incoming: Any) -> Any:
    if incoming in (None, "", [], {}):
        return existing
    if isinstance(existing, list) or isinstance(incoming, list):
        return _unique(_as_list(existing) + _as_list(incoming))
    existing_text = sanitize_wiki_text(str(existing or ""))
    incoming_text = sanitize_wiki_text(str(incoming or ""))
    if not existing_text:
        return incoming_text
    if not incoming_text or incoming_text in existing_text:
        return existing
    return f"{existing_text}\n\n{incoming_text}"


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _unique(items: list[Any]) -> list[Any]:
    seen = set()
    result = []
    for item in items:
        key = str(item).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _merge_change_dicts(base: dict[str, Any], planned: dict[str, Any]) -> dict[str, Any]:
    result = dict(base or {})
    for key, value in (planned or {}).items():
        if key == "content_json" and isinstance(value, dict):
            current = result.get("content_json") if isinstance(result.get("content_json"), dict) else {}
            result["content_json"] = {**current, **value}
        elif value not in (None, "", [], {}):
            result[key] = value
    return result


def _json(data: Any) -> str:
    import json

    return json.dumps(data, ensure_ascii=False, indent=2)

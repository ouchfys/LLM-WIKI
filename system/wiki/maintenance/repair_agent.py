from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from system.wiki.maintenance.candidates import MaintenanceCandidateStore
from system.wiki.maintenance.store import WikiMaintenanceStore
from system.wiki.paper_pipeline.distiller import parse_json_object
from system.wiki.paper_pipeline.store import PaperWikiPipelineStore
from system.wiki.wiki_builder import sanitize_wiki_text
from system.wiki.wiki_store import WikiStore


LLM_REPAIR_TARGETS = {
    "repair_agent",
    "distill_agent",
    "merge_agent",
    "reviewer_agent",
}


REPAIR_AGENT_PROMPT = """\
You are the Repair Agent for a self-maintained LLM research wiki.
Return strict JSON only. Do not write markdown.

Your job:
- Convert a repair task into a reviewable candidate.
- Do not directly patch official wiki pages.
- Do not invent facts. Use only supplied task payload, existing card content,
  existing evidence links, and source snippets.
- If evidence is insufficient, quarantine the task.

Allowed status values:
- candidate_ready
- quarantined

Allowed candidate_type values:
- card_update
- card_link_update
- card_deduplication
- source_reextract
- source_redistill

Return this shape:
{
  "status": "candidate_ready",
  "candidate_type": "card_update",
  "target_card_id": "",
  "title": "",
  "risk": "low|medium|high",
  "changes": {
    "summary": "",
    "content_json": {},
    "related_topics": [],
    "source_ids": []
  },
  "evidence_basis": [
    {
      "source_id": "",
      "fact": "",
      "supports_change": ""
    }
  ],
  "review_notes": "",
  "reason": ""
}

If evidence is insufficient, return:
{
  "status": "quarantined",
  "reason": "insufficient evidence",
  "missing_evidence": []
}

Input:
{payload}
"""


class WikiRepairAgent:
    """LLM semantic repair for non-deterministic maintenance tasks.

    This agent stages candidates only. It never mutates wiki_pages or link
    tables directly; candidates must later pass review and merge.
    """

    def __init__(self, db_path: str | Path | None = None, llm: Any = None):
        repo_root = Path(__file__).resolve().parents[3]
        self.db_path = str(Path(db_path) if db_path else repo_root / "sessions.db")
        self.llm = llm
        self.store = WikiMaintenanceStore(db_path=self.db_path)
        self.candidates = MaintenanceCandidateStore(db_path=self.db_path)
        self.wiki_store = WikiStore(db_path=self.db_path)
        self.pipeline_store = PaperWikiPipelineStore(db_path=self.db_path)

    def process_pending(self, *, limit: int = 10, upload: bool = True) -> dict[str, Any]:
        tasks = self.store.list_repair_tasks(status="pending", limit=limit)
        results = []
        for task in tasks:
            if str(task.get("repair_target") or "") not in LLM_REPAIR_TARGETS:
                continue
            results.append(self.process_task(task, upload=upload))
        return {
            "ok": True,
            "processed": len(results),
            "items": results,
        }

    def process_task(self, task: dict[str, Any], *, upload: bool = True) -> dict[str, Any]:
        task_id = str(task.get("id") or "")
        attempts = int(task.get("attempts") or 0) + 1
        if not self.llm:
            result = {"ok": False, "reason": "repair llm unavailable"}
            status = "quarantined" if attempts >= 2 else "pending"
            self.store.update_repair_task(task_id, status=status, attempts=attempts, result=result)
            return {"id": task_id, "status": status, "result": result}

        context = self._task_context(task)
        try:
            payload = self._repair_with_llm(context)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
            status = "quarantined" if attempts >= 2 else "pending"
            self.store.update_repair_task(task_id, status=status, attempts=attempts, result=result)
            return {"id": task_id, "status": status, "result": result}

        normalized = self._normalize_payload(payload, task)
        if normalized.get("status") != "candidate_ready":
            result = {"ok": False, "candidate": normalized}
            self.store.update_repair_task(task_id, status="quarantined", attempts=attempts, result=result)
            return {"id": task_id, "status": "quarantined", "result": result}

        candidate = self.candidates.add(
            source_type="repair_agent",
            source_id=task_id,
            candidate_type=normalized.get("candidate_type") or "card_update",
            title=normalized.get("title") or task.get("target_entity_id") or "Repair candidate",
            payload=normalized,
            status="candidate_ready",
            upload=upload,
        )
        result = {
            "ok": True,
            "candidate_id": candidate["id"],
            "artifact_uri": candidate["artifact_uri"],
            "candidate_type": candidate["candidate_type"],
            "title": candidate["title"],
        }
        self.store.update_repair_task(task_id, status="candidate_ready", attempts=attempts, result=result)
        return {"id": task_id, "status": "candidate_ready", "result": result}

    def _repair_with_llm(self, context: dict[str, Any]) -> dict[str, Any]:
        prompt = REPAIR_AGENT_PROMPT.format(
            payload=json.dumps(context, ensure_ascii=False, indent=2)
        )
        raw = self.llm.invoke(prompt, temperature=0.0, max_tokens=2600)
        parsed = parse_json_object(raw)
        if not parsed:
            raise ValueError("repair agent returned invalid JSON")
        return parsed

    def _task_context(self, task: dict[str, Any]) -> dict[str, Any]:
        target_id = str(task.get("target_entity_id") or "")
        target_card = self.wiki_store.get_card(target_id) if target_id else None
        links = self.pipeline_store.list_card_links(target_id) if target_id else {"sources": [], "outgoing": [], "incoming": []}
        source_snippets = []
        for item in links.get("sources", [])[:6]:
            source_snippets.append({
                "source_packet_id": item.get("source_packet_id", ""),
                "source_card_title": item.get("source_card_title", ""),
                "section_id": item.get("section_id", ""),
                "claim_text": sanitize_wiki_text(item.get("claim_text", ""))[:500],
                "evidence_text": sanitize_wiki_text(item.get("evidence_text", ""))[:900],
            })
        similar_cards = []
        query = target_card.get("title", "") if target_card else target_id
        if query:
            similar_cards = [
                {
                    "id": card.get("id", ""),
                    "title": card.get("title", ""),
                    "page_type": card.get("page_type", ""),
                    "summary": sanitize_wiki_text(card.get("summary", ""))[:360],
                }
                for card in self.wiki_store.search_cards(query, limit=8)
            ]
        return {
            "repair_task": {
                "id": task.get("id", ""),
                "task_type": task.get("task_type", ""),
                "repair_target": task.get("repair_target", ""),
                "target_entity_type": task.get("target_entity_type", ""),
                "target_entity_id": target_id,
                "payload": task.get("payload", {}),
            },
            "target_card": self._compact_card(target_card),
            "source_evidence": source_snippets,
            "existing_links": {
                "outgoing": links.get("outgoing", [])[:8],
                "incoming": links.get("incoming", [])[:8],
            },
            "similar_cards": similar_cards,
        }

    @staticmethod
    def _compact_card(card: dict[str, Any] | None) -> dict[str, Any]:
        if not card:
            return {}
        content = card.get("content_json") if isinstance(card.get("content_json"), dict) else {}
        return {
            "id": card.get("id", ""),
            "title": card.get("title", ""),
            "page_type": card.get("page_type", ""),
            "summary": sanitize_wiki_text(card.get("summary", ""))[:600],
            "content_json": content,
            "source_urls": card.get("source_urls", []),
            "related_topics": card.get("related_topics", []),
        }

    @staticmethod
    def _normalize_payload(payload: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        status = str(payload.get("status") or "").strip()
        if status not in {"candidate_ready", "quarantined"}:
            status = "quarantined"
        candidate_type = str(payload.get("candidate_type") or "card_update").strip()
        if candidate_type not in {
            "card_update",
            "card_link_update",
            "card_deduplication",
            "source_reextract",
            "source_redistill",
        }:
            candidate_type = "card_update"
        changes = payload.get("changes") if isinstance(payload.get("changes"), dict) else {}
        evidence_basis = payload.get("evidence_basis") if isinstance(payload.get("evidence_basis"), list) else []
        return {
            "status": status,
            "candidate_type": candidate_type,
            "target_card_id": str(payload.get("target_card_id") or task.get("target_entity_id") or ""),
            "title": sanitize_wiki_text(str(payload.get("title") or task.get("target_entity_id") or ""))[:180],
            "risk": str(payload.get("risk") or "medium") if str(payload.get("risk") or "medium") in {"low", "medium", "high"} else "medium",
            "changes": changes,
            "evidence_basis": [item for item in evidence_basis if isinstance(item, dict)][:12],
            "review_notes": sanitize_wiki_text(str(payload.get("review_notes") or ""))[:1000],
            "reason": sanitize_wiki_text(str(payload.get("reason") or ""))[:1000],
            "source_repair_task_id": task.get("id", ""),
            "repair_task_type": task.get("task_type", ""),
        }

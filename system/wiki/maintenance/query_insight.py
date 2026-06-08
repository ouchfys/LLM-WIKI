from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from system.wiki.maintenance.candidates import MaintenanceCandidateStore
from system.wiki.maintenance.store import WikiMaintenanceStore


QUERY_INSIGHT_PROMPT = """\
You are the Query Insight Distiller for an LLM-maintained research wiki.
Your job is to decide whether an archived user Q&A contains reusable knowledge
that should later be reviewed and merged into the official wiki.

Return strict JSON only. Do not write markdown.

Allowed statuses:
- candidate_ready: reusable knowledge exists
- skip: no reusable knowledge

Allowed candidate_type values:
- concept_card
- method_card
- interview_note
- claim
- comparison
- open_question

Return this shape:
{
  "status": "candidate_ready",
  "candidate_type": "concept_card",
  "title": "",
  "aliases": [],
  "summary": "",
  "content_json": {
    "key_idea": "",
    "method": "",
    "evidence": [],
    "source_query_id": ""
  },
  "related_topics": [],
  "reason": ""
}

Rules:
- Do not invent paper results or citations.
- Only use the archived question, answer, citations, resources, and tool trace.
- Prefer skip for one-off navigation, short factual answers, or unsupported claims.
- If the answer compares multiple cards/papers or clarifies an interview concept,
  produce candidate_ready.

Archived query artifact:
__PAYLOAD__
"""


class QueryInsightDistiller:
    """Distill archived answered queries into reviewable candidate payloads.

    This class does not write official wiki pages. It updates
    wiki_query_insights with a candidate payload for future review/merge.
    """

    def __init__(self, db_path: str | Path | None = None, llm: Any = None):
        self.store = WikiMaintenanceStore(db_path=db_path)
        self.candidates = MaintenanceCandidateStore(db_path=db_path)
        self.llm = llm

    def distill_pending(self, *, limit: int = 10) -> dict[str, Any]:
        items = self.store.list_query_insights(status="archived", limit=limit)
        results = []
        for item in items:
            results.append(self.distill_one(item))
        return {
            "ok": True,
            "processed": len(results),
            "items": results,
        }

    def distill_one(self, item: dict[str, Any]) -> dict[str, Any]:
        insight_id = item.get("id", "")
        artifact = item.get("insight") or {}
        candidate = self._llm_candidate(artifact) if self.llm else self._deterministic_candidate(artifact)
        if candidate.get("status") != "candidate_ready" and artifact.get("source_type") == "user_selection":
            candidate = self._user_selection_candidate(artifact)
        status = "candidate_ready" if candidate.get("status") == "candidate_ready" else "skipped"
        next_insight = dict(artifact)
        next_insight["distilled_candidate"] = candidate
        next_insight["distiller"] = "llm" if self.llm else "deterministic"
        candidate_id = ""
        if status == "candidate_ready":
            staged = self.candidates.add(
                source_type="query_insight",
                source_id=insight_id,
                candidate_type=str(candidate.get("candidate_type") or "query_insight"),
                title=str(candidate.get("title") or "Archived query insight"),
                payload=candidate,
                status="candidate_ready",
            )
            candidate_id = staged["id"]
            candidate["candidate_id"] = candidate_id
            next_insight["distilled_candidate"] = candidate
            next_insight["candidate_artifact_uri"] = staged.get("artifact_uri", "")
        self.store.update_query_insight(
            insight_id,
            status=status,
            insight=next_insight,
            candidate_id=candidate_id or str(candidate.get("candidate_id") or ""),
        )
        return {
            "id": insight_id,
            "status": status,
            "candidate_type": candidate.get("candidate_type", ""),
            "title": candidate.get("title", ""),
        }

    def _llm_candidate(self, artifact: dict[str, Any]) -> dict[str, Any]:
        prompt = QUERY_INSIGHT_PROMPT.replace(
            "__PAYLOAD__",
            json.dumps(self._compact_artifact(artifact), ensure_ascii=False, indent=2),
        )
        try:
            raw = self.llm.invoke(prompt, temperature=0.0, max_tokens=1800)
            parsed = json.loads(self._extract_json(raw))
            if isinstance(parsed, dict):
                return self._normalize_candidate(parsed, artifact)
        except Exception as exc:
            print(f"[QueryInsightDistiller] LLM distill failed: {exc}")
        return self._deterministic_candidate(artifact)

    def _deterministic_candidate(self, artifact: dict[str, Any]) -> dict[str, Any]:
        if artifact.get("source_type") == "user_selection":
            return self._user_selection_candidate(artifact)

        question = str(artifact.get("question") or "").strip()
        answer = str(artifact.get("answer_excerpt") or "").strip()
        citations = artifact.get("citations") if isinstance(artifact.get("citations"), list) else []
        if len(answer) < 160 or not citations:
            return {
                "status": "skip",
                "candidate_type": "",
                "title": "",
                "aliases": [],
                "summary": "",
                "content_json": {},
                "related_topics": [],
                "reason": "answer is too short or has no wiki citations",
            }
        title = question[:80].strip(" ?？。")
        return {
            "status": "candidate_ready",
            "candidate_type": "interview_note" if self._looks_interview(question) else "concept_card",
            "title": title or "Archived query insight",
            "aliases": [title] if title else [],
            "summary": answer[:280],
            "content_json": {
                "schema_version": "query-insight-v1",
                "key_idea": answer,
                "evidence": citations,
                "source_query_id": artifact.get("query_id", ""),
                "artifact_uri": artifact.get("artifact_uri", ""),
            },
            "related_topics": [item.get("title", "") for item in citations if item.get("title")][:6],
            "reason": "archived answer cites wiki cards and contains reusable explanation",
        }

    def _user_selection_candidate(self, artifact: dict[str, Any]) -> dict[str, Any]:
        question = str(artifact.get("question") or "").strip()
        selected_text = str(artifact.get("selected_text") or artifact.get("answer_excerpt") or "").strip()
        title = self._selection_title(question=question, selected_text=selected_text)
        related_topics = self._related_topics_from_text(" ".join([question, selected_text]))
        evidence = [
            {
                "source": "user_selection",
                "query_id": artifact.get("query_id", ""),
                "artifact_uri": artifact.get("artifact_uri", ""),
                "text": selected_text[:1200],
            }
        ]
        return {
            "status": "candidate_ready",
            "candidate_type": "source_note" if len(selected_text) < 180 else "concept_card",
            "title": title,
            "aliases": [title] if title else [],
            "summary": selected_text[:320] or question[:320],
            "content_json": {
                "schema_version": "query-insight-v1",
                "source_type": "user_selection",
                "question": question,
                "notes": selected_text,
                "evidence": evidence,
                "source_query_id": artifact.get("query_id", ""),
                "artifact_uri": artifact.get("artifact_uri", ""),
            },
            "related_topics": related_topics,
            "reason": "user explicitly selected this answer fragment for wiki feedback",
        }

    @staticmethod
    def _normalize_candidate(candidate: dict[str, Any], artifact: dict[str, Any]) -> dict[str, Any]:
        status = str(candidate.get("status") or "skip")
        if status != "candidate_ready":
            status = "skip"
        content = candidate.get("content_json") if isinstance(candidate.get("content_json"), dict) else {}
        content.setdefault("schema_version", "query-insight-v1")
        content.setdefault("source_query_id", artifact.get("query_id", ""))
        content.setdefault("artifact_uri", artifact.get("artifact_uri", ""))
        return {
            "status": status,
            "candidate_type": str(candidate.get("candidate_type") or ""),
            "title": str(candidate.get("title") or "")[:160],
            "aliases": [str(item) for item in candidate.get("aliases") or [] if str(item).strip()][:12],
            "summary": str(candidate.get("summary") or "")[:800],
            "content_json": content,
            "related_topics": [str(item) for item in candidate.get("related_topics") or [] if str(item).strip()][:12],
            "reason": str(candidate.get("reason") or ""),
        }

    @staticmethod
    def _compact_artifact(artifact: dict[str, Any]) -> dict[str, Any]:
        return {
            "query_id": artifact.get("query_id", ""),
            "question": artifact.get("question", ""),
            "answer_excerpt": artifact.get("answer_excerpt", ""),
            "citations": artifact.get("citations", []),
            "resources": artifact.get("resources", []),
            "trace_summary": artifact.get("trace_summary", {}),
        }

    @staticmethod
    def _extract_json(text: str) -> str:
        text = (text or "").strip()
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return text[start:end + 1]
        return text

    @staticmethod
    def _selection_title(question: str, selected_text: str) -> str:
        basis = (question or selected_text or "User selected insight").strip()
        basis = " ".join(basis.replace("\n", " ").split())
        basis = basis.strip(" ?？。；;，,")
        if len(basis) <= 64:
            return basis or "User selected insight"
        return basis[:64].rstrip(" ?？。；;，,") or "User selected insight"

    @staticmethod
    def _related_topics_from_text(text: str) -> list[str]:
        import re

        terms = re.findall(r"[A-Za-z][A-Za-z0-9+\-_/]{2,}|[\u4e00-\u9fff]{2,}", text or "")
        stop = {"帮我", "总结", "一下", "传统", "算法", "结构化解释", "核心原理", "典型应用"}
        result: list[str] = []
        for term in terms:
            if term in stop or term in result:
                continue
            result.append(term)
            if len(result) >= 8:
                break
        return result

    @staticmethod
    def _looks_interview(question: str) -> bool:
        lowered = question.lower()
        return any(token in lowered for token in ["面试", "面经", "interview", "为什么", "怎么回答"])

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from system.wiki.maintenance.candidates import MaintenanceCandidateStore
from system.wiki.maintenance.store import WikiMaintenanceStore
from system.wiki.paper_pipeline.store import PaperWikiPipelineStore
from system.wiki.wiki_store import WikiStore


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


CONVERSATION_INSIGHT_PROMPT = """\
You compile a user-directed discussion into one durable personal Wiki insight.
The user has explicitly said what should be preserved. Find only the messages
that support that instruction and discard detours, repetitions, rejected ideas,
and command acknowledgements.

Return strict JSON only. Do not write markdown.

Return this shape:
{
  "status": "candidate_ready",
  "candidate_type": "source_note",
  "title": "",
  "aliases": [],
  "summary": "",
  "knowledge_kind": "user_idea|discussion_conclusion|source_backed_conclusion|open_question",
  "selected_message_ids": [],
  "content_json": {
    "main_points": [],
    "open_questions": []
  },
  "related_topics": [],
  "reason": ""
}

Rules:
- Follow the user's preservation instruction exactly, including exclusions.
- Never turn an AI suggestion into a paper-proven fact.
- Use source_backed_conclusion only when the transcript contains explicit Wiki
  citations; otherwise use user_idea, discussion_conclusion, or open_question.
- selected_message_ids MUST come from the supplied transcript.
- Keep the result concise and reusable. Write the knowledge content in Chinese.
- Do not include the /wiki command itself as knowledge.

Input:
__PAYLOAD__
"""


INSIGHT_ROUTING_PROMPT = """\
You route a user-directed discussion insight into a personal research wiki.
Decide whether it should UPDATE an existing wiki page or CREATE a new page.

Return strict JSON only. Do not write markdown.

You are given the user's instruction, the compiled insight, and a list of candidate
existing wiki pages (each with id, title, page_type, summary). Choose update
only when the insight genuinely belongs to one of the candidate pages; prefer
create when it is a distinct concept or no candidate fits.

Return this shape:
{
  "action": "update" | "create",
  "target_card_id": "",   // required when action is update; MUST be one of the candidate ids
  "reason": ""
}

Rules:
- target_card_id MUST be copied verbatim from a candidate id. Never invent an id.
- If unsure, choose create.

Input:
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
        self.wiki_store = WikiStore(db_path=str(db_path) if db_path else None)
        self.pipeline_store = PaperWikiPipelineStore(db_path=db_path)
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
        if artifact.get("source_type") == "conversation_command":
            candidate = self._conversation_candidate(artifact)
        else:
            candidate = self._llm_candidate(artifact) if self.llm else self._deterministic_candidate(artifact)
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

    def _conversation_candidate(self, artifact: dict[str, Any]) -> dict[str, Any]:
        candidate: dict[str, Any] = {}
        if self.llm:
            prompt = CONVERSATION_INSIGHT_PROMPT.replace(
                "__PAYLOAD__",
                json.dumps(self._compact_artifact(artifact), ensure_ascii=False, indent=2),
            )
            try:
                raw = self.llm.invoke(prompt, temperature=0.0, max_tokens=1800)
                parsed = json.loads(self._extract_json(raw))
                if isinstance(parsed, dict):
                    candidate = parsed
            except Exception as exc:
                print(f"[QueryInsightDistiller] conversation distill failed: {exc}")

        if str(candidate.get("status") or "") != "candidate_ready":
            candidate = self._deterministic_conversation_candidate(artifact)

        candidate["candidate_type"] = "source_note"
        normalized = self._normalize_candidate(candidate, artifact)
        content = normalized.get("content_json") if isinstance(normalized.get("content_json"), dict) else {}
        ordered_message_ids = [
            str(item.get("id"))
            for item in artifact.get("messages") or []
            if isinstance(item, dict) and item.get("id") is not None
        ]
        allowed_message_ids = set(ordered_message_ids)
        selected_message_ids = [
            str(value) for value in candidate.get("selected_message_ids") or []
            if str(value) in allowed_message_ids
        ]
        if not selected_message_ids:
            selected_message_ids = ordered_message_ids

        knowledge_kind = str(candidate.get("knowledge_kind") or "discussion_conclusion")
        if knowledge_kind not in {
            "user_idea", "discussion_conclusion", "source_backed_conclusion", "open_question",
        }:
            knowledge_kind = "discussion_conclusion"
        citations = artifact.get("citations") if isinstance(artifact.get("citations"), list) else []
        if knowledge_kind == "source_backed_conclusion" and not citations:
            knowledge_kind = "discussion_conclusion"
        content.update({
            "source_type": "conversation_insight",
            "knowledge_kind": knowledge_kind,
            "conversation_instruction": str(artifact.get("instruction") or "")[:800],
            "source_session_id": str(artifact.get("session_id") or ""),
            "source_message_ids": selected_message_ids,
            "related_sources": citations[:12],
        })
        normalized["content_json"] = content
        normalized["candidate_type"] = "source_note"

        main_points = content.get("main_points") or content.get("key_idea") or normalized.get("summary") or ""
        routing_text = "\n".join([
            str(normalized.get("summary") or ""),
            json.dumps(main_points, ensure_ascii=False) if not isinstance(main_points, str) else main_points,
        ]).strip()
        related_topics = [str(item) for item in normalized.get("related_topics") or [] if str(item).strip()]
        if self.llm:
            target = self._resolve_target_card_llm(
                title=str(normalized.get("title") or ""),
                question=str(artifact.get("instruction") or ""),
                insight_text=routing_text,
                related_topics=related_topics,
            )
        else:
            target = self._resolve_target_card(
                title=str(normalized.get("title") or ""),
                question=str(artifact.get("instruction") or ""),
                insight_text=routing_text,
                related_topics=related_topics,
            )
        if not target:
            return normalized
        return self._conversation_update_candidate(
            target=target,
            artifact=artifact,
            summary=str(normalized.get("summary") or ""),
            content=content,
            related_topics=related_topics,
        )

    def _deterministic_conversation_candidate(self, artifact: dict[str, Any]) -> dict[str, Any]:
        instruction = str(artifact.get("instruction") or artifact.get("question") or "").strip()
        messages = [item for item in artifact.get("messages") or [] if isinstance(item, dict)]
        assistant_texts = [
            str(item.get("content") or "").strip()
            for item in messages if item.get("role") == "assistant" and str(item.get("content") or "").strip()
        ]
        body = assistant_texts[-1] if assistant_texts else instruction
        title = self._insight_title(instruction, body)
        return {
            "status": "candidate_ready",
            "candidate_type": "source_note",
            "title": title,
            "aliases": [title] if title else [],
            "summary": body[:320] or instruction[:320],
            "knowledge_kind": "discussion_conclusion",
            "selected_message_ids": [str(item.get("id")) for item in messages if item.get("id") is not None],
            "content_json": {"main_points": [body[:1600]] if body else []},
            "related_topics": self._related_topics_from_text(f"{instruction} {body}"),
            "reason": "explicit /wiki command with deterministic fallback",
        }

    def _conversation_update_candidate(
        self,
        *,
        target: dict[str, Any],
        artifact: dict[str, Any],
        summary: str,
        content: dict[str, Any],
        related_topics: list[str],
    ) -> dict[str, Any]:
        points = content.get("main_points")
        if not isinstance(points, list):
            points = [str(points)] if str(points or "").strip() else []
        insight_text = summary.strip() or "\n".join(str(item) for item in points if str(item).strip())
        source_ref = f"conversation://{artifact.get('session_id', '')}"
        changes: dict[str, Any] = {
            "content_json": {"conversation_insights": [insight_text] if insight_text else []},
            "related_topics": related_topics,
        }
        open_questions = content.get("open_questions")
        if isinstance(open_questions, list) and open_questions:
            changes["content_json"]["open_questions"] = open_questions
        return {
            "status": "candidate_ready",
            "candidate_type": "card_update",
            "target_card_id": target["id"],
            "title": target.get("title", ""),
            "summary": summary,
            "risk": "low",
            "review_notes": "Appended from an explicit /wiki conversation command.",
            "evidence_basis": [{
                "fact": insight_text[:600],
                "source": "conversation_command",
                "source_url": source_ref,
                "message_ids": content.get("source_message_ids") or [],
            }],
            "changes": changes,
            "content_json": {
                **content,
                "source_type": "conversation_insight_update",
                "target_card_id": target["id"],
                "match_reason": target.get("match_reason", ""),
                "artifact_uri": artifact.get("artifact_uri", ""),
            },
            "related_topics": related_topics,
            "reason": f"explicit /wiki insight maps to existing page via {target.get('match_reason', 'match')}",
        }

    def _recall_candidate_cards(
        self,
        *,
        title: str,
        question: str,
        insight_text: str,
        related_topics: list[str],
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Recall existing Wiki pages that might host a distilled insight."""
        terms = [t for t in [title, question, insight_text[:120], *related_topics[:5]] if t]
        cards: list[dict[str, Any]] = []
        seen: set[str] = set()
        for term in terms:
            for card in self.wiki_store.search_cards(term, limit=5):
                cid = str(card.get("id") or "")
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                cards.append({
                    "id": cid,
                    "title": str(card.get("title") or ""),
                    "page_type": str(card.get("page_type") or ""),
                    "summary": str(card.get("summary") or "")[:300],
                })
                if len(cards) >= limit:
                    return cards
        return cards

    def _resolve_target_card_llm(
        self,
        *,
        title: str,
        question: str,
        insight_text: str,
        related_topics: list[str],
    ) -> dict[str, Any] | None:
        """LLM decides update-vs-create against recalled candidate pages.

        Returns a target dict when the LLM picks a valid candidate to update;
        returns None to mean "create a new page". Any LLM failure or invalid id
        falls back to the deterministic resolver so behavior degrades safely.
        """
        candidates = self._recall_candidate_cards(
            title=title,
            question=question,
            insight_text=insight_text,
            related_topics=related_topics,
        )
        if not candidates:
            return None

        payload = {
            "question": question,
            "insight_text": insight_text[:1200],
            "candidate_pages": candidates,
        }
        prompt = INSIGHT_ROUTING_PROMPT.replace(
            "__PAYLOAD__", json.dumps(payload, ensure_ascii=False, indent=2)
        )
        try:
            raw = self.llm.invoke(prompt, temperature=0.0, max_tokens=400)
            decision = json.loads(self._extract_json(raw))
        except Exception as exc:
            print(f"[QueryInsightDistiller] insight routing LLM failed: {exc}")
            return self._resolve_target_card(
                title=title,
                question=question,
                insight_text=insight_text,
                related_topics=related_topics,
            )

        if not isinstance(decision, dict) or str(decision.get("action") or "") != "update":
            return None
        target_id = str(decision.get("target_card_id") or "")
        # Whitelist: the LLM may only target a card it was actually shown.
        allowed = {card["id"]: card for card in candidates}
        if target_id not in allowed:
            return None
        chosen = allowed[target_id]
        return {
            "id": chosen["id"],
            "title": chosen.get("title", ""),
            "page_type": chosen.get("page_type", ""),
            "match_reason": f"llm routing: {str(decision.get('reason') or 'update existing page')}"[:200],
        }

    def _resolve_target_card(
        self,
        *,
        title: str,
        question: str,
        insight_text: str,
        related_topics: list[str],
    ) -> dict[str, Any] | None:
        """Find the existing Wiki page this insight should update, or None.

        Conservative on purpose: only returns a target on a strong signal. We
        search the wiki for candidate pages, then accept one only when its title
        (or a known alias) appears as a whole phrase inside the user's question
        or distilled insight. Fuzzy/semantic ranking is deferred to a later
        LLM-assisted pass so we never silently merge into a loosely-related page.
        """
        haystack = f"{question}\n{insight_text}".lower()
        if not haystack.strip():
            return None

        # Direct alias hit on the derived title / related terms is the strongest
        # signal when it lands, so try it first.
        alias_queries = [q for q in [title, *related_topics[:4]] if q]
        alias_hit = self.pipeline_store.find_card_by_alias(alias_queries) if alias_queries else None
        if alias_hit and alias_hit.get("card_id"):
            card = self.wiki_store.get_card(str(alias_hit["card_id"]))
            if card:
                return {
                    "id": card["id"],
                    "title": card.get("title", ""),
                    "page_type": card.get("page_type", ""),
                    "match_reason": "normalized alias match",
                }

        # Otherwise, search the wiki and accept a candidate whose title appears
        # verbatim in the explicit instruction or distilled insight.
        search_terms = [t for t in [title, question, insight_text[:120], *related_topics[:4]] if t]
        seen_cards: set[str] = set()
        for term in search_terms:
            for card in self.wiki_store.search_cards(term, limit=5):
                cid = str(card.get("id") or "")
                if not cid or cid in seen_cards:
                    continue
                seen_cards.add(cid)
                card_title = str(card.get("title") or "").strip()
                if len(card_title) >= 3 and card_title.lower() in haystack:
                    return {
                        "id": cid,
                        "title": card_title,
                        "page_type": card.get("page_type", ""),
                        "match_reason": "title appears in the requested insight",
                    }
        return None

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
        compact = {
            "query_id": artifact.get("query_id", ""),
            "question": artifact.get("question", ""),
            "answer_excerpt": artifact.get("answer_excerpt", ""),
            "citations": artifact.get("citations", []),
            "resources": artifact.get("resources", []),
            "trace_summary": artifact.get("trace_summary", {}),
        }
        if artifact.get("source_type") == "conversation_command":
            compact.update({
                "source_type": "conversation_command",
                "instruction": artifact.get("instruction", ""),
                "messages": artifact.get("messages", []),
            })
        return compact

    @staticmethod
    def _extract_json(text: str) -> str:
        text = (text or "").strip()
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return text[start:end + 1]
        return text

    @staticmethod
    def _insight_title(question: str, insight_text: str) -> str:
        basis = (question or insight_text or "Conversation insight").strip()
        basis = " ".join(basis.replace("\n", " ").split())
        basis = basis.strip(" ?？。；;，,")
        if len(basis) <= 64:
            return basis or "Conversation insight"
        return basis[:64].rstrip(" ?？。；;，,") or "Conversation insight"

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

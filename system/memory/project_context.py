"""Model-assisted conversation resolution and project-memory distillation."""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, Iterable, List, Optional


Invoke = Callable[[str, str, int], str]


class ProjectContextManager:
    """Let the model propose semantic updates; let the store validate and commit them."""

    SESSION_FIELDS = {
        "current_goal", "active_entities", "constraints", "decisions",
        "open_questions", "last_turn_summary", "selected_wiki_pages",
    }
    USER_PREFERENCE_KEYS = {
        "language_preference", "answer_length", "answer_style",
        "citation_preference", "explanation_style",
    }

    def __init__(self, store, invoke: Optional[Invoke] = None):
        self.store = store
        self.invoke = invoke

    def resolve_turn(
        self,
        session_id: str,
        message: str,
        history_text: str,
        project_context: str,
        fallback: str,
    ) -> str:
        if not self.invoke or not session_id or not history_text.strip():
            return fallback
        prompt = f"""Resolve a Chinese multi-turn research conversation into a standalone retrieval query.
Return strict JSON only: {{"is_followup": boolean, "standalone_query": string, "referenced_context": [string]}}.
Use project state and conversation history to resolve pronouns, omitted paper names, comparison sets, constraints and prior decisions.
If the message starts a separate topic, set is_followup=false and copy the current message as standalone_query.
Do not answer the question and do not invent papers or constraints.

Project context:
{project_context}

Conversation history:
{history_text}

Current message:
{message}
"""
        try:
            parsed = _json_object(self.invoke(prompt, "memory.resolve_turn", 450))
            query = str(parsed.get("standalone_query") or "").strip()
            if query and len(query) <= 4000:
                return query
        except Exception as exc:
            print(f"[ProjectContextManager] turn resolution failed: {exc}")
        return fallback

    def maintain_turn(
        self,
        session_id: str,
        message: str,
        answer: str,
        *,
        evidence_message_ids: Iterable[int],
        cards: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, str]]:
        if not session_id or not self.store.get_session(session_id):
            return []
        project_id = self.store.get_session_project_id(session_id)
        card_refs = [
            {"card_id": str(card.get("id") or ""), "title": str(card.get("title") or "")}
            for card in (cards or [])[:8]
            if str(card.get("id") or "")
        ]
        if not self.invoke:
            if card_refs:
                self.store.update_session_state(
                    session_id,
                    {"selected_wiki_pages": card_refs},
                    through_message_id=max([int(i) for i in evidence_message_ids] or [0]),
                )
            return []

        project_context = self.store.render_project_context(session_id, message, memory_limit=6)
        prompt = f"""Extract working state and durable project memory from one completed research-chat turn.
Return strict JSON only with this shape:
{{
  "session_state": {{
    "current_goal": string,
    "active_entities": [string],
    "constraints": [string],
    "decisions": [string],
    "open_questions": [string],
    "last_turn_summary": string
  }},
  "user_preferences": [
    {{"key": "language_preference|answer_length|answer_style|citation_preference|explanation_style",
      "value": string, "evidence_quote": string, "confidence": number}}
  ],
  "memories": [
    {{"type": "goal|constraint|decision|open_question|milestone|topic", "content": string,
      "evidence_quote": string, "confidence": number, "importance": number,
      "durability": "durable|temporary", "supersedes_memory_id": integer|null}}
  ]
}}

Rules:
- Session state may describe the immediate task. Project state and memories require evidence that the item matters across conversations in this research project.
- Store explicit project goals, durable constraints, confirmed/rejected decisions, unresolved research questions and completed milestones.
- Every project memory requires evidence_quote copied exactly from the user message. Assistant suggestions must wait for later user confirmation.
- When the user explicitly replaces or reverses an existing [memory:id] of the same type, set supersedes_memory_id to that id. Otherwise return null.
- Do not turn paper claims, retrieved Wiki content, assistant suggestions or one-turn formatting requests into project memory.
- A user preference such as language or answer length is user memory, not project memory.
- Only extract a user preference when the user explicitly makes it persistent, for example with '以后', '默认', '始终' or '记住'. One-turn requests are not preferences. evidence_quote must be an exact substring of the user message.
- Return empty arrays/objects when nothing deserves persistence. Do not invent facts.

Existing project context:
{project_context}

Opened Wiki pages:
{json.dumps(card_refs, ensure_ascii=False)}

User message:
{message}

Assistant answer:
{answer[:8000]}
"""
        try:
            parsed = _json_object(self.invoke(prompt, "memory.distill_project", 1100))
        except Exception as exc:
            print(f"[ProjectContextManager] project distillation failed: {exc}")
            return []

        ids = [int(item) for item in evidence_message_ids if int(item) > 0]
        through = max(ids or [0])
        session_patch = self._state_patch(parsed.get("session_state"), self.SESSION_FIELDS)
        if card_refs:
            session_patch["selected_wiki_pages"] = card_refs
        self.store.replace_session_state(session_id, session_patch, through_message_id=through)

        updates: List[Dict[str, str]] = []
        for preference in parsed.get("user_preferences") or []:
            if not isinstance(preference, dict):
                continue
            key = str(preference.get("key") or "").strip()
            value = " ".join(str(preference.get("value") or "").split())[:500]
            evidence_quote = str(preference.get("evidence_quote") or "").strip()
            confidence = _number(preference.get("confidence"), 0.0)
            if (key not in self.USER_PREFERENCE_KEYS or not value or confidence < 0.85
                    or not self._preference_supported(key, evidence_quote, message)):
                continue
            self.store.upsert_preference(
                key,
                value,
                evidence=message,
                source_session_id=session_id,
                evidence_message_id=ids[0] if ids else 0,
                confidence=confidence,
            )
            updates.append({"signal_type": "preference", "value": f"{key}={value}"})

        state_keys = {
            "goal": "goals",
            "constraint": "constraints",
            "decision": "decisions",
            "open_question": "open_questions",
            "milestone": "milestones",
            "topic": "active_topics",
        }
        durable_memory_changed = False
        for candidate in parsed.get("memories") or []:
            if not isinstance(candidate, dict):
                continue
            memory_type = str(candidate.get("type") or "").strip().lower()
            content = " ".join(str(candidate.get("content") or "").split())
            evidence_quote = str(candidate.get("evidence_quote") or "").strip()
            confidence = _number(candidate.get("confidence"), 0.0)
            importance = _number(candidate.get("importance"), 0.5)
            if (memory_type not in self.store._PROJECT_MEMORY_TYPES or len(content) < 4 or confidence < 0.72
                    or not evidence_quote or evidence_quote.lower() not in message.lower()):
                continue
            durability = str(candidate.get("durability") or "temporary").lower()
            supersedes_id = self._positive_int(candidate.get("supersedes_memory_id"))
            if supersedes_id is not None and not self._supersession_supported(evidence_quote):
                continue
            saved = self.store.add_project_memory(
                project_id,
                memory_type,
                content,
                source_session_id=session_id,
                evidence_message_ids=ids,
                confidence=confidence,
                importance=importance,
                ttl_days=None if durability == "durable" else 30,
                supersedes_id=supersedes_id,
            )
            if saved:
                updates.append({"signal_type": f"project_{memory_type}", "value": content})
                durable_memory_changed = durable_memory_changed or durability == "durable" or supersedes_id is not None
        if durable_memory_changed:
            rebuilt_state: Dict[str, List[str]] = {}
            active_memories = self.store.search_project_memories(project_id, "", limit=200)
            for memory in reversed(active_memories):
                if memory.get("expires_at") is not None:
                    continue
                state_key = state_keys.get(str(memory.get("memory_type") or ""))
                value = str(memory.get("content") or "").strip()
                if state_key and value:
                    bucket = rebuilt_state.setdefault(state_key, [])
                    if value not in bucket:
                        bucket.append(value)
            self.store.replace_project_state(
                project_id,
                rebuilt_state,
                through_message_id=through,
                source_session_id=session_id,
            )
        return updates

    @staticmethod
    def _preference_supported(key: str, quote: str, message: str) -> bool:
        quote_l = quote.lower().strip()
        message_l = str(message or "").lower()
        if not quote_l or quote_l not in message_l:
            return False
        if not any(marker in quote_l for marker in ("以后", "今后", "默认", "始终", "记住", "always", "from now on")):
            return False
        hints = {
            "language_preference": ("中文", "英文", "chinese", "english"),
            "answer_length": ("简短", "精炼", "详细", "展开", "短一点", "长一点", "short", "long"),
            "answer_style": ("结论", "结构", "格式", "conclusion", "format"),
            "citation_preference": ("引用", "出处", "来源", "citation", "source"),
            "explanation_style": ("技术", "专业", "通俗", "白话", "简单", "technical", "simple"),
        }
        return any(hint in quote_l for hint in hints.get(key, ()))

    @staticmethod
    def _state_patch(value: Any, allowed: set[str]) -> Dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        result: Dict[str, Any] = {}
        for key in allowed:
            item = value.get(key)
            if isinstance(item, str):
                item = " ".join(item.split())[:1000]
            elif isinstance(item, list):
                item = [entry for entry in item[:20] if isinstance(entry, (str, dict)) and entry]
            else:
                continue
            if item:
                result[key] = item
        return result

    @staticmethod
    def _positive_int(value: Any) -> Optional[int]:
        try:
            parsed = int(value)
            return parsed if parsed > 0 else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _supersession_supported(quote: str) -> bool:
        lowered = str(quote or "").lower()
        return any(marker in lowered for marker in (
            "改为", "改成", "替换", "更新为", "不再", "取消", "删除", "放弃",
            "instead", "replace", "no longer", "change to", "switch to",
        ))


def _json_object(raw: str) -> Dict[str, Any]:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(text[start:end + 1])
    return value if isinstance(value, dict) else {}


def _number(value: Any, default: float) -> float:
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return default

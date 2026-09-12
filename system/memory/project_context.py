"""Model-assisted multi-turn resolution for project conversations.

Durable project state is maintained directly in ``MEMORY.md`` through the
Agent's memory tool. This module only resolves follow-up turns; it deliberately
does not run a second post-answer memory-extraction model call.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Dict, Optional


Invoke = Callable[[str, str, int], str]


class ProjectContextManager:
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
Use purpose.md, MEMORY.md and conversation history to resolve pronouns, omitted paper names, comparison sets, constraints and prior decisions.
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


def _json_object(raw: str) -> Dict[str, object]:
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

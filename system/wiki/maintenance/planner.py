from __future__ import annotations

import json
from typing import Any

from system.core.config import SILICONFLOW_FAST_MODEL


class MaintenancePlanner:
    """LLM-assisted repair-task planner with deterministic fallback.

    The planner never writes official wiki pages. It only groups validation
    errors into repair task payloads for later repair/review/merge processing.
    """

    def __init__(self, llm: Any = None):
        self.llm = llm
        self.model = SILICONFLOW_FAST_MODEL

    def plan(self, report: dict[str, Any]) -> dict[str, Any]:
        if not report.get("errors") and not report.get("warnings"):
            return self.deterministic_plan(report, reason="no_validation_findings")
        if not self.llm:
            return self.deterministic_plan(report, reason="llm_unavailable")
        try:
            planned = self._plan_with_llm(report)
            tasks = planned.get("tasks") if isinstance(planned, dict) else None
            if isinstance(tasks, list):
                normalized = [self._normalize_task(item) for item in tasks if isinstance(item, dict)]
                normalized = [item for item in normalized if item]
                return {
                    "ok": True,
                    "planner": "llm",
                    "model": self.model,
                    "tasks": normalized,
                }
        except Exception as exc:
            return self.deterministic_plan(report, reason=f"llm_failed: {exc}")
        return self.deterministic_plan(report, reason="llm_invalid_output")

    def deterministic_plan(self, report: dict[str, Any], reason: str = "deterministic") -> dict[str, Any]:
        tasks = []
        for item in report.get("errors", []) or []:
            if not isinstance(item, dict):
                continue
            repair_target = str(item.get("repair_target") or "")
            if not repair_target or repair_target == "none":
                continue
            tasks.append(self._normalize_task({
                "task_type": item.get("error_type") or "validation_error",
                "priority": "high",
                "target_entity_type": item.get("entity_type", ""),
                "target_entity_id": item.get("entity_id", ""),
                "repair_target": repair_target,
                "reason": item.get("message", ""),
                "evidence": [f"validator:error:{item.get('error_type', 'unknown')}"],
                "payload": item,
            }))
        return {
            "ok": True,
            "planner": "deterministic",
            "model": "",
            "fallback_reason": reason,
            "tasks": [task for task in tasks if task],
        }

    def _plan_with_llm(self, report: dict[str, Any]) -> dict[str, Any]:
        errors = report.get("errors", [])[:40]
        warnings = report.get("warnings", [])[:20]
        prompt = (
            "You are the Maintenance Planner for an LLM-maintained research wiki.\n"
            "Convert validation errors into repair tasks. Do not repair content.\n"
            "Return strict JSON only with this schema:\n"
            "{\n"
            '  "tasks": [\n'
            "    {\n"
            '      "task_type": "repair_broken_link",\n'
            '      "priority": "high|medium|low",\n'
            '      "target_entity_type": "wiki_page|wiki_card_link|source_packet|database",\n'
            '      "target_entity_id": "...",\n'
            '      "repair_target": "deterministic_fixer|extract_agent|distill_agent|repair_agent|merge_agent|index_generator",\n'
            '      "reason": "...",\n'
            '      "evidence": ["validator:error:..."]\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            "Rules:\n"
            "- Prefer deterministic_fixer for pure schema/path/index issues.\n"
            "- Use merge_agent for broken links, duplicate cards, alias conflicts.\n"
            "- Use distill_agent for weak evidence and hallucinated claims.\n"
            "- Use extract_agent for missing source packets or storage source issues.\n"
            "- Keep tasks concise and deduplicate similar tasks.\n\n"
            f"Validation errors:\n{json.dumps(errors, ensure_ascii=False, indent=2)}\n\n"
            f"Validation warnings:\n{json.dumps(warnings, ensure_ascii=False, indent=2)}"
        )
        messages = [
            {"role": "system", "content": "Return valid JSON only. No markdown."},
            {"role": "user", "content": prompt},
        ]
        if hasattr(self.llm, "complete_json"):
            data = self.llm.complete_json(messages)
        elif hasattr(self.llm, "invoke"):
            data = json.loads(self._extract_json(self.llm.invoke(prompt, temperature=0.0, max_tokens=1800)))
        else:
            text = self.llm.complete(messages)
            data = json.loads(self._extract_json(text))
        if not isinstance(data, dict):
            raise ValueError("planner returned non-object JSON")
        return data

    @staticmethod
    def _normalize_task(item: dict[str, Any]) -> dict[str, Any]:
        task_type = str(item.get("task_type") or item.get("error_type") or "").strip()
        repair_target = str(item.get("repair_target") or "").strip()
        if not task_type or not repair_target or repair_target == "none":
            return {}
        priority = str(item.get("priority") or "medium").strip().lower()
        if priority not in {"high", "medium", "low"}:
            priority = "medium"
        return {
            "task_type": task_type,
            "priority": priority,
            "target_entity_type": str(item.get("target_entity_type") or item.get("entity_type") or ""),
            "target_entity_id": str(item.get("target_entity_id") or item.get("entity_id") or ""),
            "repair_target": repair_target,
            "reason": str(item.get("reason") or item.get("message") or ""),
            "evidence": item.get("evidence") if isinstance(item.get("evidence"), list) else [],
            "payload": item.get("payload") if isinstance(item.get("payload"), dict) else item,
        }

    @staticmethod
    def _extract_json(text: str) -> str:
        text = (text or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return text[start:end + 1]
        return text

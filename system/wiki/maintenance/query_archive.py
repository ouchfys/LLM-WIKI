from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.storage import get_object_storage, get_storage_layout
from system.wiki.maintenance.store import WikiMaintenanceStore


class QueryArchive:
    """Archive reusable wiki-chat turns under queries/answered/.

    This is a safe first step toward knowledge回流: archived turns are not
    merged into the official wiki until a later insight distiller/reviewer/merge
    path accepts them.
    """

    def __init__(self, db_path: str | Path | None = None):
        self.store = WikiMaintenanceStore(db_path=db_path)
        self.layout = get_storage_layout()
        self.storage = get_object_storage()

    def should_archive(
        self,
        *,
        question: str,
        answer: str,
        citations: list[Any],
        resources: list[dict[str, Any]],
        trace: dict[str, Any],
    ) -> bool:
        if not question.strip() or len(answer.strip()) < 120:
            return False
        if re.search(r"(保存|记住|总结|归档|沉淀|remember|save|archive|summari[sz]e)", question, re.I):
            return True
        if citations:
            return True
        if resources:
            return True
        observations = trace.get("tool_observations", []) if isinstance(trace, dict) else []
        return any(obs.get("tool") in {"wiki_card", "web_fetch", "web_search"} for obs in observations if isinstance(obs, dict))

    def archive_turn(
        self,
        *,
        session_id: str,
        question: str,
        answer: str,
        citations: list[Any],
        resources: list[dict[str, Any]],
        tool_plan: dict[str, Any],
        trace: dict[str, Any],
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        query_id = self._query_id(session_id, question, answer)
        rel_path = Path("answered") / now.date().isoformat() / f"{query_id}.md"
        path = self.layout.queries_dir / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)

        citation_items = [self._citation_dict(item) for item in citations]
        artifact = {
            "query_id": query_id,
            "session_id": session_id,
            "question": question,
            "answer_excerpt": self._compact(answer, 600),
            "citations": citation_items,
            "resources": resources,
            "tool_plan": tool_plan,
            "trace_summary": self._trace_summary(trace),
        }
        markdown = self._render_markdown(artifact, answer)
        path.write_text(markdown, encoding="utf-8")
        uri = self.storage.upload_text(
            self.storage.key_for_local_path(path),
            markdown,
            content_type="text/markdown; charset=utf-8",
        )
        artifact["artifact_uri"] = uri
        artifact["artifact_path"] = path.relative_to(self.layout.repo_root).as_posix()
        insight_id = self.store.add_query_insight(
            session_id=session_id,
            question=question,
            answer_excerpt=self._compact(answer, 600),
            insight=artifact,
            status="archived",
        )
        artifact["insight_id"] = insight_id
        return artifact

    @staticmethod
    def _query_id(session_id: str, question: str, answer: str) -> str:
        digest = hashlib.sha1(f"{session_id}\n{question}\n{answer[:500]}".encode("utf-8")).hexdigest()[:12]
        slug = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "-", question.strip().lower()).strip("-")[:48]
        return f"{slug or 'query'}-{digest}"

    @staticmethod
    def _citation_dict(item: Any) -> dict[str, Any]:
        if isinstance(item, dict):
            return item
        return {
            "card_id": getattr(item, "card_id", ""),
            "title": getattr(item, "title", ""),
            "page_type": getattr(item, "page_type", ""),
            "summary": getattr(item, "summary", ""),
            "markdown_path": getattr(item, "markdown_path", ""),
        }

    @staticmethod
    def _trace_summary(trace: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(trace, dict):
            return {}
        observations = trace.get("tool_observations", [])
        return {
            "tool_observations": [
                {
                    "tool": obs.get("tool", ""),
                    "query": obs.get("query", ""),
                    "status": obs.get("status", ""),
                    "summary": obs.get("summary", ""),
                    "item_count": len(obs.get("items") or []),
                }
                for obs in observations
                if isinstance(obs, dict)
            ],
        }

    @staticmethod
    def _render_markdown(artifact: dict[str, Any], answer: str) -> str:
        lines = [
            "---",
            f"id: {artifact['query_id']}",
            "type: answered_query",
            f"session_id: {artifact.get('session_id', '')}",
            f"created: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            "status: archived",
            "---",
            "",
            "# Answered Query",
            "",
            "## Question",
            "",
            artifact.get("question", "").strip(),
            "",
            "## Answer",
            "",
            answer.strip(),
            "",
            "## Citations",
            "",
        ]
        citations = artifact.get("citations") or []
        if citations:
            for item in citations:
                lines.append(f"- {item.get('title', '')} (`{item.get('card_id', '')}`)")
        else:
            lines.append("- none")
        lines.extend([
            "",
            "## Tool Trace Summary",
            "",
            "```json",
            json.dumps(artifact.get("trace_summary", {}), ensure_ascii=False, indent=2),
            "```",
            "",
        ])
        return "\n".join(lines)

    @staticmethod
    def _compact(value: Any, limit: int) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        return text[:limit].rsplit(" ", 1)[0] + "..."

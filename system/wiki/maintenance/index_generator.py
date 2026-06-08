from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.storage import get_object_storage, get_storage_layout


class WikiIndexGenerator:
    """Generate deterministic query index markdown from wiki tables."""

    def __init__(self, db_path: str | Path | None = None):
        repo_root = Path(__file__).resolve().parents[3]
        self.repo_root = repo_root
        self.db_path = str(Path(db_path) if db_path else repo_root / "sessions.db")
        self.layout = get_storage_layout()
        self.storage = get_object_storage()

    def generate_all(self, *, upload: bool = True) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            pages = self._fetch(conn, "SELECT * FROM wiki_pages ORDER BY title COLLATE NOCASE")
            links = self._fetch(conn, "SELECT * FROM wiki_card_links")
            sources = self._fetch(conn, "SELECT * FROM wiki_card_sources")
            aliases = self._fetch(conn, "SELECT * FROM wiki_aliases")

        page_by_id = {page["id"]: page for page in pages}
        source_count = defaultdict(int)
        for item in sources:
            source_count[item.get("card_id", "")] += 1

        link_count = defaultdict(int)
        for link in links:
            link_count[link.get("from_card_id", "")] += 1
            link_count[link.get("to_card_id", "")] += 1

        aliases_by_card: dict[str, list[str]] = defaultdict(list)
        for alias in aliases:
            value = str(alias.get("alias") or "").strip()
            if value:
                aliases_by_card[str(alias.get("card_id") or "")].append(value)

        files = {
            "by-paper.md": self._by_type(pages, "PaperPage", "Paper", source_count, aliases_by_card),
            "by-concept.md": self._by_type(pages, "ConceptPage", "Concept", source_count, aliases_by_card),
            "by-method.md": self._by_type(pages, "MethodPage", "Method", source_count, aliases_by_card),
            "by-claim.md": self._by_future_type(pages, "claim", "Claim", source_count, aliases_by_card),
            "by-benchmark.md": self._by_future_type(pages, "benchmark", "Benchmark", source_count, aliases_by_card),
            "by-source.md": self._by_source(sources, page_by_id),
            "by-interview-topic.md": self._by_type(pages, "InterviewQA", "Interview Topic", source_count, aliases_by_card),
            "orphan-cards.md": self._orphan_cards(pages, link_count, source_count),
            "weak-evidence.md": self._weak_evidence(pages, source_count),
        }

        artifacts: list[dict[str, Any]] = []
        output_dir = self.layout.queries_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        for name, markdown in files.items():
            path = output_dir / name
            path.write_text(markdown, encoding="utf-8")
            uri = ""
            if upload:
                uri = self.storage.upload_text(
                    self.storage.key_for_local_path(path),
                    markdown,
                    content_type="text/markdown; charset=utf-8",
                )
            artifacts.append({
                "name": name,
                "path": path.relative_to(self.repo_root).as_posix(),
                "uri": uri,
                "bytes": len(markdown.encode("utf-8")),
            })

        return {
            "ok": True,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "card_count": len(pages),
            "artifact_count": len(artifacts),
            "artifacts": artifacts,
        }

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _fetch(conn: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
        try:
            return [dict(row) for row in conn.execute(sql).fetchall()]
        except sqlite3.OperationalError:
            return []

    def _by_type(
        self,
        pages: list[dict[str, Any]],
        page_type: str,
        label: str,
        source_count: dict[str, int],
        aliases_by_card: dict[str, list[str]],
    ) -> str:
        selected = [page for page in pages if page.get("page_type") == page_type]
        return self._table_index(f"Query: By {label}", selected, source_count, aliases_by_card)

    def _by_future_type(
        self,
        pages: list[dict[str, Any]],
        semantic_type: str,
        label: str,
        source_count: dict[str, int],
        aliases_by_card: dict[str, list[str]],
    ) -> str:
        selected = []
        for page in pages:
            content = self._json(page.get("content_json"), {})
            kind = str(content.get("type") or content.get("candidate_type") or page.get("page_type") or "").lower()
            if semantic_type in kind:
                selected.append(page)
        return self._table_index(f"Query: By {label}", selected, source_count, aliases_by_card)

    def _table_index(
        self,
        title: str,
        pages: list[dict[str, Any]],
        source_count: dict[str, int],
        aliases_by_card: dict[str, list[str]],
    ) -> str:
        lines = self._header(title)
        lines.extend([
            "| Title | Type | Summary | Aliases | Sources | Markdown |",
            "| --- | --- | --- | --- | ---: | --- |",
        ])
        for page in pages:
            card_id = page.get("id", "")
            aliases = ", ".join(aliases_by_card.get(card_id, [])[:5])
            lines.append(
                "| {title} | `{ptype}` | {summary} | {aliases} | {src} | `{path}` |".format(
                    title=self._escape(page.get("title", "")),
                    ptype=self._escape(page.get("page_type", "")),
                    summary=self._escape(self._compact(page.get("summary", ""), 160)),
                    aliases=self._escape(aliases),
                    src=source_count.get(card_id, 0),
                    path=self._escape(page.get("markdown_path", "")),
                )
            )
        if not pages:
            lines.append("| _No pages yet_ |  |  |  | 0 |  |")
        return "\n".join(lines) + "\n"

    def _by_source(self, sources: list[dict[str, Any]], page_by_id: dict[str, dict[str, Any]]) -> str:
        lines = self._header("Query: By Source")
        lines.extend([
            "| Source Packet | Card | Claim | Evidence | Raw Source |",
            "| --- | --- | --- | --- | --- |",
        ])
        for item in sources:
            page = page_by_id.get(item.get("card_id", ""), {})
            lines.append(
                "| `{packet}` | {card} | {claim} | {evidence} | `{raw}` |".format(
                    packet=self._escape(item.get("source_packet_id", "")),
                    card=self._escape(page.get("title") or item.get("card_id", "")),
                    claim=self._escape(self._compact(item.get("claim_text", ""), 120)),
                    evidence=self._escape(self._compact(item.get("evidence_text", ""), 160)),
                    raw=self._escape(item.get("raw_source_path", "")),
                )
            )
        if not sources:
            lines.append("| _No source links yet_ |  |  |  |  |")
        return "\n".join(lines) + "\n"

    def _orphan_cards(
        self,
        pages: list[dict[str, Any]],
        link_count: dict[str, int],
        source_count: dict[str, int],
    ) -> str:
        lines = self._header("Query: Orphan Cards")
        lines.extend([
            "| Title | Type | Summary | Markdown |",
            "| --- | --- | --- | --- |",
        ])
        count = 0
        for page in pages:
            card_id = page.get("id", "")
            if page.get("page_type") == "PaperPage":
                continue
            if link_count.get(card_id, 0) or source_count.get(card_id, 0):
                continue
            count += 1
            lines.append(
                f"| {self._escape(page.get('title', ''))} | `{self._escape(page.get('page_type', ''))}` "
                f"| {self._escape(self._compact(page.get('summary', ''), 160))} "
                f"| `{self._escape(page.get('markdown_path', ''))}` |"
            )
        if count == 0:
            lines.append("| _No orphan cards_ |  |  |  |")
        return "\n".join(lines) + "\n"

    def _weak_evidence(self, pages: list[dict[str, Any]], source_count: dict[str, int]) -> str:
        lines = self._header("Query: Weak Evidence")
        lines.extend([
            "| Title | Type | Reason | Summary |",
            "| --- | --- | --- | --- |",
        ])
        count = 0
        for page in pages:
            card_id = page.get("id", "")
            reasons = []
            if source_count.get(card_id, 0) == 0:
                reasons.append("no source links")
            if len(str(page.get("summary") or "").strip()) < 30:
                reasons.append("short summary")
            content = self._json(page.get("content_json"), {})
            if isinstance(content, dict) and str(content.get("compile_status", "")).endswith("fallback"):
                reasons.append("fallback compile")
            if reasons:
                count += 1
                lines.append(
                    f"| {self._escape(page.get('title', ''))} | `{self._escape(page.get('page_type', ''))}` "
                    f"| {self._escape(', '.join(reasons))} "
                    f"| {self._escape(self._compact(page.get('summary', ''), 160))} |"
                )
        if count == 0:
            lines.append("| _No weak-evidence cards detected_ |  |  |  |")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _header(title: str) -> list[str]:
        return [
            f"# {title}",
            "",
            "> Auto-generated by `system.wiki.maintenance.index_generator`. Do not edit manually.",
            "",
        ]

    @staticmethod
    def _json(value: Any, default: Any) -> Any:
        if value in (None, ""):
            return default
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return default

    @staticmethod
    def _compact(value: Any, limit: int) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        return text[:limit].rsplit(" ", 1)[0] + "..."

    @staticmethod
    def _escape(value: Any) -> str:
        return str(value or "").replace("|", "\\|").replace("\n", " ")

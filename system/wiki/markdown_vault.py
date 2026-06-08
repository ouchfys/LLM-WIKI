"""
Markdown-first Wiki vault.

The SQLite store indexes cards for UI and search, but Markdown files are the
human-readable source of the personal Wiki.
"""

import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from system.storage import get_object_storage, get_storage_layout


PAGE_TYPE_DIRS = {
    "ConceptPage": "concepts",
    "PaperPage": "papers",
    "MethodPage": "methods",
    "ComparePage": "comparisons",
    "InterviewQA": "interview",
    "MistakeNote": "mistakes",
    "StudyPlan": "plans",
    "SourceNote": "sources",
}


class MarkdownVault:
    def __init__(self, vault_dir: Optional[str] = None):
        self.repo_root = Path(__file__).resolve().parents[2]
        self.vault_dir = Path(vault_dir) if vault_dir else get_storage_layout().wiki_dir

    @property
    def vault_name(self) -> str:
        return self.vault_dir.name

    def vault_info(self) -> Dict[str, str]:
        storage = get_object_storage()
        return {
            "vault_name": self.vault_name,
            "vault_path": storage.uri_for_key(storage.key_for_local_path(self.vault_dir)) if storage.enabled else str(self.vault_dir.resolve()),
            "obsidian_uri": "" if storage.enabled else self.vault_uri(),
        }

    def vault_uri(self) -> str:
        return f"obsidian://open?vault={urllib.parse.quote(self.vault_name)}"

    def card_uri(self, markdown_path: str) -> str:
        if (markdown_path or "").startswith(("oss://", "local://")):
            return ""
        path = self.resolve_markdown_path(markdown_path)
        try:
            rel = path.relative_to(self.vault_dir).as_posix()
        except ValueError:
            rel = path.name
        return (
            "obsidian://open?"
            f"vault={urllib.parse.quote(self.vault_name)}"
            f"&file={urllib.parse.quote(rel)}"
        )

    def resolve_markdown_path(self, markdown_path: str) -> Path:
        path = Path(markdown_path)
        if path.is_absolute():
            return path
        return self.repo_root / path

    def write_card(
        self,
        card_id: str,
        title: str,
        page_type: str,
        summary: str = "",
        content_json: Optional[Dict[str, Any]] = None,
        source_level: str = "",
        source_urls: Optional[List[str]] = None,
        related_topics: Optional[List[str]] = None,
        existing_path: str = "",
    ) -> str:
        path = self._resolve_path(card_id, title, page_type, existing_path)
        markdown = self.render_card(
            card_id=card_id,
            title=title,
            page_type=page_type,
            summary=summary,
            content_json=content_json or {},
            source_level=source_level,
            source_urls=source_urls or [],
            related_topics=related_topics or [],
        )
        storage = get_object_storage()
        if storage.enabled:
            return storage.upload_text(storage.key_for_local_path(path), markdown)

        self._ensure_local_vault()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        storage.upload_text(storage.key_for_local_path(path), markdown)
        try:
            return path.relative_to(self.repo_root).as_posix()
        except ValueError:
            return str(path)

    def delete_card(self, markdown_path: str) -> None:
        if not markdown_path:
            return
        if markdown_path.startswith(("oss://", "local://")):
            try:
                get_object_storage().delete(markdown_path)
            except Exception:
                pass
            return
        path = Path(markdown_path)
        if not path.is_absolute():
            path = self.repo_root / path
        try:
            if path.exists() and self.vault_dir in path.resolve().parents:
                path.unlink()
        except OSError:
            pass

    def render_card(
        self,
        card_id: str,
        title: str,
        page_type: str,
        summary: str,
        content_json: Dict[str, Any],
        source_level: str,
        source_urls: List[str],
        related_topics: List[str],
    ) -> str:
        """Render the canonical Markdown schema used by the wiki reindexer."""
        now = datetime.now(timezone.utc).date().isoformat()
        content_json = content_json or {}
        aliases = self._as_string_list(content_json.get("aliases"))
        source_packet_id = str(content_json.get("source_packet_id") or "")
        if not source_packet_id:
            for source in content_json.get("sources") or []:
                if isinstance(source, dict) and source.get("source_packet_id"):
                    source_packet_id = str(source.get("source_packet_id") or "")
                    break
        if not source_packet_id:
            source_packet_ids = self._as_string_list(content_json.get("source_packet_ids"))
            source_packet_id = source_packet_ids[0] if source_packet_ids else ""
        review_status = str(content_json.get("review_status") or "")
        status = self._status_for(review_status, source_level)
        tags = self._tags_for(page_type, related_topics)

        frontmatter = [
            "---",
            f"id: {self._yaml_scalar(card_id)}",
            f"title: {self._yaml_scalar(title)}",
            f"type: {self._yaml_scalar(page_type)}",
            f"status: {self._yaml_scalar(status)}",
            f"created: {self._yaml_scalar(now)}",
            f"updated: {self._yaml_scalar(now)}",
            f"source_level: {self._yaml_scalar(source_level)}",
            "aliases:",
        ]
        frontmatter.extend([f"  - {self._yaml_scalar(alias)}" for alias in aliases] or ["  []"])
        frontmatter.append("tags:")
        frontmatter.extend([f"  - {self._yaml_scalar(tag)}" for tag in tags] or ["  []"])
        frontmatter.append("sources:")
        if source_urls:
            for url in source_urls:
                frontmatter.append(f"  - url: {self._yaml_scalar(url)}")
                frontmatter.append(f"    level: {self._yaml_scalar(source_level)}")
                if source_packet_id:
                    frontmatter.append(f"    source_packet_id: {self._yaml_scalar(source_packet_id)}")
        else:
            frontmatter.append("  []")
        frontmatter.append("related:")
        frontmatter.extend([f"  - {self._yaml_scalar(topic)}" for topic in related_topics] or ["  []"])
        frontmatter.extend(["---", ""])

        body = [f"# {title}", ""]
        if summary:
            body.extend(["## Summary", "", summary.strip(), ""])
        body.extend(self._render_content_sections(page_type, content_json))
        if source_urls:
            body.extend(["## Evidence", ""])
            for url in source_urls:
                label = source_level or "source"
                body.append(f"- [{label}] {url}")
            body.append("")
        if related_topics:
            body.extend(["## Links", ""])
            for topic in related_topics:
                body.append(f"- [[{topic}]]")
            body.append("")
        body.extend([
            "## Notes",
            "",
            "- ",
            "",
            "## Review Status",
            "",
            f"- status: {status}",
            f"- reviewer: {review_status or 'unreviewed'}",
            "",
        ])
        return "\n".join(frontmatter + body).rstrip() + "\n"

    def _ensure_local_vault(self) -> None:
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        for dirname in set(PAGE_TYPE_DIRS.values()):
            (self.vault_dir / dirname).mkdir(parents=True, exist_ok=True)
        self._init_obsidian_vault()

    def _init_obsidian_vault(self) -> None:
        obsidian_dir = self.vault_dir / ".obsidian"
        obsidian_dir.mkdir(parents=True, exist_ok=True)

        app_json = obsidian_dir / "app.json"
        if not app_json.exists():
            app_json.write_text(
                '{\n'
                '  "alwaysUpdateLinks": true,\n'
                '  "newFileLocation": "folder",\n'
                '  "newFileFolderPath": "inbox",\n'
                '  "attachmentFolderPath": "attachments"\n'
                '}\n',
                encoding="utf-8",
            )

        appearance_json = obsidian_dir / "appearance.json"
        if not appearance_json.exists():
            appearance_json.write_text(
                '{\n'
                '  "baseFontSize": 16,\n'
                '  "cssTheme": ""\n'
                '}\n',
                encoding="utf-8",
            )

        readme = self.vault_dir / "README.md"
        if not readme.exists():
            readme.write_text(
                "# 私有化贾维斯 Vault\n\n"
                "这是私有化贾维斯生成和维护的 Obsidian 知识库。\n\n"
                "建议工作流：\n\n"
                "1. 在 Web 端发现论文、面经、博客和灵感。\n"
                "2. 存入 Wiki 后自动生成 Markdown 笔记。\n"
                "3. 在 Obsidian 中继续改写、双链、复习和整理。\n"
                "4. Web 端负责检索、推荐、精读入口和画像更新。\n",
                encoding="utf-8",
            )

        (self.vault_dir / "inbox").mkdir(exist_ok=True)
        (self.vault_dir / "attachments").mkdir(exist_ok=True)

    def _resolve_path(
        self,
        card_id: str,
        title: str,
        page_type: str,
        existing_path: str = "",
    ) -> Path:
        if existing_path and not existing_path.startswith(("oss://", "local://")):
            path = Path(existing_path)
            if not path.is_absolute():
                path = self.repo_root / path
            return path

        dirname = PAGE_TYPE_DIRS.get(page_type, "sources")
        slug = self._slugify(title) or card_id
        return self.vault_dir / dirname / f"{slug}.md"

    @staticmethod
    def _render_content_sections(page_type: str, content: Dict[str, Any]) -> List[str]:
        """Render stable, parser-friendly Markdown body sections."""
        lines: List[str] = []
        if not content:
            return lines

        preferred = {
            "PaperPage": [
                ("problem", "Problem"),
                ("key_idea", "Key Ideas"),
                ("method", "Method"),
                ("methods", "Method"),
                ("results", "Results"),
                ("findings", "Findings"),
                ("limitations", "Limitations"),
                ("key_takeaways", "Key Takeaways"),
                ("interview_notes", "Interview Notes"),
                ("notes", "Notes"),
            ],
            "ConceptPage": [
                ("definition", "Definition"),
                ("mechanism", "Mechanism"),
                ("method", "Method"),
                ("findings", "Findings"),
                ("limitations", "Limitations"),
                ("key_takeaways", "Key Ideas"),
                ("explanation", "Explanation"),
                ("examples", "Examples"),
                ("related_concepts", "Related Concepts"),
            ],
            "MethodPage": [
                ("definition", "Definition"),
                ("mechanism", "Mechanism"),
                ("method", "Method"),
                ("findings", "Findings"),
                ("limitations", "Limitations"),
                ("key_takeaways", "Key Ideas"),
                ("category", "Category"),
                ("description", "Description"),
                ("when_to_use", "When To Use"),
                ("steps", "Steps"),
                ("comparison_to_alternatives", "Comparison To Alternatives"),
            ],
            "ComparePage": [
                ("item_a", "Item A"),
                ("item_b", "Item B"),
                ("dimensions", "Dimensions"),
            ],
            "InterviewQA": [
                ("question", "Question"),
                ("ideal_answer", "Ideal Answer"),
                ("key_points", "Key Points"),
                ("common_mistakes", "Common Mistakes"),
            ],
            "MistakeNote": [
                ("mistake", "Mistake"),
                ("correction", "Correction"),
                ("lesson", "Lesson"),
                ("context", "Context"),
            ],
        }

        seen = set()
        for key, label in preferred.get(page_type, []):
            if key in content:
                lines.extend(MarkdownVault._render_value(label, content.get(key)))
                seen.add(key)
        for key, value in content.items():
            if key not in seen and not key.startswith("_") and key not in {"aliases", "sources", "source_packet_id", "source_packet_ids", "raw_source_path", "pdf_storage_uri", "review_status"}:
                lines.extend(MarkdownVault._render_value(key.replace("_", " ").title(), value))
        return lines

    @staticmethod
    def _render_value(label: str, value: Any) -> List[str]:
        if value in (None, "", [], {}):
            return []
        lines = [f"## {label}", ""]
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    parts = [f"{k}: {v}" for k, v in item.items() if v not in (None, "")]
                    lines.append(f"- {'; '.join(parts)}")
                else:
                    lines.append(f"- {item}")
        elif isinstance(value, dict):
            for key, nested in value.items():
                lines.append(f"- **{key}**: {nested}")
        else:
            lines.append(str(value).strip())
        lines.append("")
        return lines

    @staticmethod
    def _slugify(title: str) -> str:
        title = (title or "").strip().lower()
        title = re.sub(r"[^\w\u4e00-\u9fff]+", "-", title)
        title = re.sub(r"-+", "-", title).strip("-")
        return title[:80]

    @staticmethod
    def _yaml_scalar(value: Any) -> str:
        text = str(value or "").replace('"', '\\"')
        return f'"{text}"'

    @staticmethod
    def _as_string_list(value: Any) -> List[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    @staticmethod
    def _status_for(review_status: str, source_level: str) -> str:
        review = (review_status or "").strip().lower()
        if review in {"approved", "verified"}:
            return "verified"
        if review in {"reviewed"}:
            return "reviewed"
        level = (source_level or "").strip().lower()
        if level in {"primary", "verified"}:
            return "reviewed"
        return "draft"

    @staticmethod
    def _tags_for(page_type: str, related_topics: List[str]) -> List[str]:
        base = {
            "PaperPage": ["paper"],
            "ConceptPage": ["concept"],
            "MethodPage": ["method"],
            "ComparePage": ["compare"],
            "InterviewQA": ["interview"],
            "MistakeNote": ["mistake"],
            "SourceNote": ["source"],
        }.get(page_type, ["wiki"])
        cleaned = []
        seen = set()
        for tag in [*base, *(related_topics or [])]:
            value = re.sub(r"\s+", "-", str(tag).strip())
            if value and value.lower() not in seen:
                seen.add(value.lower())
                cleaned.append(value)
        return cleaned

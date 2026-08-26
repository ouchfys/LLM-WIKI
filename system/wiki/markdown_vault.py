"""
Markdown-first Wiki vault.

The SQLite store indexes cards for UI and search, but Markdown files are the
human-readable source of the personal Wiki.
"""

import json
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from system.storage import get_object_storage, get_storage_layout


PAGE_TYPE_DIRS = {
    "ConceptPage": "concepts",
    "TopicPage": "topics",
    "PaperPage": "papers",
    "MethodPage": "methods",
    "ComparePage": "comparisons",
    "InterviewQA": "interview",
    "MistakeNote": "mistakes",
    "StudyPlan": "plans",
    "SourceNote": "sources",
}

# These fields are part of the compiler/runtime audit trail, not the knowledge
# article a reader should scan. They remain in canonical Markdown as one hidden
# JSON comment so reindex/replay stays lossless without exposing implementation
# bookkeeping as user-facing sections.
SYSTEM_CONTENT_KEYS = {
    "schema_version", "compile_status", "pipeline", "compiler_model",
    "source_packet_id", "source_packet_ids", "raw_source_path",
    "pdf_storage_uri", "parser_used", "review_status", "aliases", "sources",
    "evidence_updates", "merge_history", "affected_claims", "compiler",
    "import_impact", "claims", "markdown_status", "review_status_text",
    "evidence", "links",
}

INTERNAL_SECTION_HEADINGS = {
    "claims", "knowledge claims", "evidence", "evidence updates",
    "merge history", "affected claims", "compiler", "import impact",
    "review status", "schema version", "compile status", "markdown status",
    "compiler model", "pipeline", "parser used", "review status text",
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
        created: str = "",
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
            created=created,
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

    def read_reference(self, markdown_path: str) -> str:
        if not markdown_path:
            return ""
        storage = get_object_storage()
        text = storage.read_text(markdown_path)
        if text:
            return text
        path = self.resolve_markdown_path(markdown_path)
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def write_raw_reference(self, markdown_path: str, markdown: str) -> str:
        """Write an already-rendered revision without reinterpreting it."""
        storage = get_object_storage()
        if markdown_path.startswith(("oss://", "local://")):
            key = storage.key_from_uri(markdown_path) if hasattr(storage, "key_from_uri") else markdown_path.split("://", 1)[-1]
            return storage.upload_text(key, markdown)
        path = self.resolve_markdown_path(markdown_path)
        self._ensure_local_vault()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        storage.upload_text(storage.key_for_local_path(path), markdown)
        try:
            return path.relative_to(self.repo_root).as_posix()
        except ValueError:
            return str(path)

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
        created: str = "",
    ) -> str:
        """Render the canonical Markdown schema used by the wiki reindexer."""
        now = datetime.now(timezone.utc).date().isoformat()
        created_date = self._normalize_date(created) or now
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
            f"created: {self._yaml_scalar(created_date)}",
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
            # Summaries are prose fields, not nested Markdown documents. A
            # Docling abstract can begin with ``##`` and would otherwise close
            # the Summary section during deterministic reindexing.
            inline_summary = re.sub(r"\s+", " ", summary).strip()
            inline_summary = re.sub(r"^#{1,6}\s*", "", inline_summary)
            body.extend(["## Summary", "", inline_summary, ""])
        body.extend(self._render_content_sections(page_type, content_json))
        if related_topics:
            body.extend(["## Links", ""])
            for topic in related_topics:
                body.append(f"- [[{topic}]]")
            body.append("")
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
                "# LLM-WIKI Vault\n\n"
                "这是 LLM-WIKI 生成和维护的 Markdown 知识库。\n\n"
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
        directory = self.vault_dir / dirname
        candidate = directory / f"{slug}.md"
        # Distinct cards may slugify to the same filename (e.g. "Self Attention"
        # vs "Self-Attention"). Their dedupe_keys differ, so this is a genuine
        # collision: disambiguate with a short card_id suffix rather than letting
        # the later write clobber the earlier card's Markdown.
        if self._slug_collides(candidate, card_id):
            candidate = directory / f"{slug}-{card_id[:8]}.md"
        return candidate

    def _slug_collides(self, candidate: Path, card_id: str) -> bool:
        storage = get_object_storage()
        if storage.enabled:
            try:
                existing = storage.read_text(storage.key_for_local_path(candidate))
            except Exception:
                return False
            if not existing:
                return False
            return f'id: "{card_id}"' not in existing
        if not candidate.exists():
            return False
        try:
            existing = candidate.read_text(encoding="utf-8")
        except OSError:
            return True
        return f'id: "{card_id}"' not in existing

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
                ("key_takeaways", "Key Takeaways"),
                ("explanation", "Explanation"),
                ("examples", "Examples"),
                ("related_concepts", "Related Concepts"),
            ],
            "TopicPage": [
                ("definition", "Definition"),
                ("mechanism", "Mechanism"),
                ("method", "Method"),
                ("findings", "Findings"),
                ("limitations", "Limitations"),
                ("key_takeaways", "Key Takeaways"),
                ("examples", "Examples"),
                ("related_concepts", "Related Topics"),
            ],
            "MethodPage": [
                ("definition", "Definition"),
                ("mechanism", "Mechanism"),
                ("method", "Method"),
                ("findings", "Findings"),
                ("limitations", "Limitations"),
                ("key_takeaways", "Key Takeaways"),
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
            if (
                key not in seen
                and not key.startswith("_")
                and key not in SYSTEM_CONTENT_KEYS
                and len(key) <= 64
            ):
                lines.extend(MarkdownVault._render_value(key.replace("_", " ").title(), value))
        system_metadata = {
            key: value for key, value in content.items()
            if key in SYSTEM_CONTENT_KEYS and value not in (None, "", [], {})
        }
        if system_metadata:
            payload = json.dumps(system_metadata, ensure_ascii=False, separators=(",", ":")).replace("-->", "--\\u003e")
            lines.extend([f"<!-- wiki-system {payload} -->", ""])
        return lines

    @staticmethod
    def _render_value(label: str, value: Any) -> List[str]:
        if value in (None, "", [], {}):
            return []
        if isinstance(value, str) and value.strip() in {"-", "- ", "[]"}:
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
    def _normalize_date(value: Any) -> str:
        """Coerce an ISO datetime/date string to a bare YYYY-MM-DD date."""
        text = str(value or "").strip()
        if not text:
            return ""
        match = re.match(r"(\d{4}-\d{2}-\d{2})", text)
        return match.group(1) if match else ""

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
            "TopicPage": ["topic"],
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


def readable_markdown(markdown: str) -> str:
    """Return only reader-facing Wiki prose.

    The sanitizer also understands legacy pages, so old compiler audit sections
    never leak into prompts while the corpus is being migrated lazily.
    """
    from system.wiki.markdown_parser import split_frontmatter

    _, body = split_frontmatter(markdown or "")
    output: list[str] = []
    skipping = False
    for line in body.replace("\r\n", "\n").splitlines():
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            skipping = heading.group(1).strip().lower() in INTERNAL_SECTION_HEADINGS
            if skipping:
                continue
        if not skipping:
            output.append(line)
    text = "\n".join(output)
    text = re.sub(r"<!--\s*wiki-(?:system|claim)\s+.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text

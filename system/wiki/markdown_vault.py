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
    "conversation_instruction", "source_session_id", "source_message_ids",
    "related_sources", "selected_table_ids",
    "repository_research", "repository_review",
}

INTERNAL_SECTION_HEADINGS = {
    "claims", "knowledge claims", "evidence", "evidence updates",
    "merge history", "affected claims", "compiler", "import impact",
    "review status", "schema version", "compile status", "markdown status",
    "compiler model", "pipeline", "parser used", "review status text",
    "sources", "source packet id", "source packet ids",
}


def normalize_markdown_table(markdown: str) -> str:
    """Repair common PDF table layout noise without rewriting cell values.

    MinerU may emit blank pipe-only rows and omit repeated leading row-group
    labels. The reader needs a rectangular Markdown table, while the source
    packet remains the authoritative copy of the original extraction.
    """
    rows: list[list[str]] = []
    for raw_line in str(markdown or "").replace("\r\n", "\n").splitlines():
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        inner = line[1:-1] if line.endswith("|") else line[1:]
        cells = [cell.strip() for cell in inner.split("|")]
        if not any(cells):
            continue
        if all(re.fullmatch(r":?-{3,}:?", cell or "") for cell in cells):
            continue
        rows.append(cells)

    if not rows:
        return ""
    # Extractors sometimes synthesize headers above the actual header row.
    if len(rows) > 1 and all(re.fullmatch(r"column_\d+", cell, re.I) for cell in rows[0]) and len(rows[1]) == len(rows[0]) and all(rows[1]):
        rows.pop(0)
    symbols = {r"$\bm{\checkmark}$": "✓", r"$\bm{\circ}$": "○", r"$\bm{\times}$": "×"}
    rows = [[symbols.get(cell, cell) for cell in row] for row in rows]
    width = max(len(row) for row in rows)
    if width < 2:
        return ""

    normalized: list[list[str]] = []
    for row in rows:
        if len(row) < width:
            # PDF rowspan extraction normally drops repeated grouping cells at
            # the left. Padding there preserves the alignment of numeric data.
            row = [""] * (width - len(row)) + row
        normalized.append(row[:width])

    def render(row: list[str]) -> str:
        return "| " + " | ".join(row) + " |"

    separator = ["---"] * width
    return "\n".join([render(normalized[0]), render(separator), *(render(row) for row in normalized[1:])])


def _compact_artifact_label(caption: str, kind: str, fallback_index: int) -> str:
    text = re.sub(r"\s+", " ", str(caption or "")).strip()
    prefix = r"(?:table|表)" if kind == "表" else r"(?:figure|fig\.?|图)"
    match = re.match(rf"^{prefix}\s*([\w.-]+)", text, flags=re.IGNORECASE)
    if match:
        number = match.group(1).rstrip(".:：")
        return f"{kind} {number}"
    return text if text else f"{kind} {fallback_index}"


def _clean_reader_markdown(text: str) -> str:
    value = str(text or "")
    value = re.sub(r"^\s*-\s*\*\*Table Id\*\*[:：].*$", "", value, flags=re.MULTILINE | re.IGNORECASE)
    replacements = {
        "Purpose": "用途",
        "Mechanism": "机制",
        "Details": "说明",
        "Evidence": "证据",
        "Conditions": "条件",
        "Implication": "含义",
    }
    for source, target in replacements.items():
        value = re.sub(rf"\*\*{source}\*\*", f"**{target}**", value, flags=re.IGNORECASE)
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def _nested_reader_label(key: str) -> str:
    return {
        "purpose": "用途",
        "mechanism": "机制",
        "details": "说明",
        "evidence": "证据",
        "conditions": "条件",
        "implication": "含义",
        "trend": "趋势",
        "key_values": "关键数值",
    }.get(key, key.replace("_", " ").title())


class MarkdownVault:
    def __init__(self, vault_dir: Optional[str] = None):
        self.repo_root = get_storage_layout().repo_root
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
        is_repository_card = bool(content_json.get("repository_research"))
        if is_repository_card:
            # A repository card records source identity/version without embedding
            # code-file URLs or requiring a separate persistent evidence bundle.
            repository = str(content_json["repository_research"].get("repository") or "")
            source_urls = [f"https://github.com/{repository}"] if repository else []
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
        if is_repository_card:
            from system.wiki.repository_verification import repository_card_status
            status = repository_card_status(content_json.get("repository_review") or {})
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
                if source_packet_id and not is_repository_card:
                    frontmatter.append(f"    source_packet_id: {self._yaml_scalar(source_packet_id)}")
        else:
            frontmatter.append("  []")
        frontmatter.append("related:")
        frontmatter.extend([f"  - {self._yaml_scalar(topic)}" for topic in related_topics] or ["  []"])
        frontmatter.extend(["---", ""])

        body = [f"# {title}", ""]
        if summary and not is_repository_card:
            # Summaries are prose fields, not nested Markdown documents. A
            # Parser abstracts can begin with ``##`` and would otherwise close
            # the Summary section during deterministic reindexing.
            inline_summary = re.sub(r"\s+", " ", summary).strip()
            inline_summary = re.sub(r"^#{1,6}\s*", "", inline_summary)
            body.extend(["## 摘要", "", inline_summary, ""])
        body.extend(self._render_content_sections(page_type, content_json))
        if is_repository_card:
            snapshot = content_json.get("repository_research") or {}
            repository = str(snapshot.get("repository") or "")
            commit = str(snapshot.get("commit") or "")
            read_date = str(snapshot.get("read_date") or "")
            if repository:
                body.extend([
                    f'<small class="repository-snapshot">代码仓库：https://github.com/{repository} · '
                    f'阅读日期（UTC）：{read_date or "未记录"} · 版本：{commit[:12] or "未记录"}</small>',
                    "",
                ])
        if related_topics:
            body.extend(["## 相关主题", ""])
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
                ("paper_type", "论文类型"),
                ("research_problem", "研究问题"),
                ("motivation", "研究动机"),
                ("contributions", "主要贡献"),
                ("method_overview", "方法概览"),
                ("method_components", "方法组成"),
                ("execution_flow", "执行流程"),
                ("experiment_setup", "实验设置"),
                ("key_results", "关键结果"),
                ("key_tables", "关键表格"),
                ("figure_notes", "图表解读"),
                ("ablations", "消融实验"),
                ("comparison_to_prior_work", "与既有工作的比较"),
                ("problem", "问题"),
                ("key_idea", "核心观点"),
                ("method", "方法"),
                ("methods", "方法"),
                ("results", "结果"),
                ("findings", "发现"),
                ("limitations", "局限"),
                ("key_takeaways", "要点"),
                ("interview_notes", "面试提示"),
                ("notes", "补充说明"),
            ],
            "ConceptPage": [
                ("definition", "定义"),
                ("mechanism", "机制"),
                ("method", "方法"),
                ("findings", "发现"),
                ("limitations", "局限"),
                ("key_takeaways", "要点"),
                ("explanation", "解释"),
                ("examples", "示例"),
                ("related_concepts", "相关主题"),
                ("conversation_insights", "对话洞见"),
                ("open_questions", "待验证问题"),
            ],
            "TopicPage": [
                ("definition", "定义"),
                ("mechanism", "机制"),
                ("method", "方法"),
                ("findings", "发现"),
                ("limitations", "局限"),
                ("key_takeaways", "要点"),
                ("examples", "示例"),
                ("related_concepts", "相关主题"),
                ("conversation_insights", "对话洞见"),
                ("open_questions", "待验证问题"),
            ],
            "MethodPage": [
                ("definition", "定义"),
                ("mechanism", "机制"),
                ("method", "方法"),
                ("findings", "发现"),
                ("limitations", "局限"),
                ("key_takeaways", "要点"),
                ("category", "类别"),
                ("description", "说明"),
                ("when_to_use", "适用场景"),
                ("steps", "步骤"),
                ("comparison_to_alternatives", "替代方案比较"),
                ("conversation_insights", "对话洞见"),
                ("open_questions", "待验证问题"),
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
            "SourceNote": [
                ("knowledge_kind", "Knowledge Type"),
                ("main_points", "Main Points"),
                ("open_questions", "Open Questions"),
                ("notes", "Notes"),
            ],
            "MistakeNote": [
                ("mistake", "Mistake"),
                ("correction", "Correction"),
                ("lesson", "Lesson"),
                ("context", "Context"),
            ],
        }

        seen = {"reading_guide"}
        if content.get("reading_guide"):
            lines.extend(MarkdownVault._render_value("核心解读", content["reading_guide"]))
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
        if label in {"Key Tables", "关键表格"}:
            if isinstance(value, list):
                return MarkdownVault._render_key_tables(value)
            if isinstance(value, str):
                return MarkdownVault._render_legacy_key_tables(value)
        if label in {"Figure Notes", "图表解读"}:
            if isinstance(value, list):
                return MarkdownVault._render_figure_notes(value)
            if isinstance(value, str):
                return MarkdownVault._render_legacy_figure_notes(value)
        lines = [f"## {label}", ""]
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    title = str(item.get("name") or item.get("finding") or item.get("factor") or "").strip()
                    if title:
                        lines.extend([f"### {title}", ""])
                    for key, nested in item.items():
                        if key in {"name", "finding", "factor", "table_id", "figure_id"} or nested in (None, "", [], {}):
                            continue
                        nested_label = _nested_reader_label(key)
                        if isinstance(nested, list):
                            lines.append(f"- **{nested_label}**: " + "；".join(str(entry) for entry in nested))
                        else:
                            lines.append(f"- **{nested_label}**: {nested}")
                    if not title and not any(item.values()):
                        continue
                    lines.append("")
                else:
                    lines.append(f"- {item}")
        elif isinstance(value, dict):
            for key, nested in value.items():
                if key in {"table_id", "figure_id"}:
                    continue
                lines.append(f"- **{key}**: {nested}")
        else:
            lines.append(_clean_reader_markdown(str(value)))
        lines.append("")
        return lines

    @staticmethod
    def _render_key_tables(tables: List[Dict[str, Any]]) -> List[str]:
        lines = ["## 关键表格", ""]
        for index, table in enumerate(tables, start=1):
            if not isinstance(table, dict):
                continue
            caption = str(table.get("caption") or f"Table {index}").strip()
            lines.extend([f"### {_compact_artifact_label(caption, '表', index)}", ""])
            location = " / ".join(
                value for value in (
                    str(table.get("section") or "").strip(),
                    f"page {table.get('page')}" if table.get("page") else "",
                ) if value
            )
            if location:
                lines.extend([f"*来源位置：{location}*", ""])
            markdown = str(table.get("markdown") or "").strip()
            if markdown:
                normalized = normalize_markdown_table(markdown)
                if normalized:
                    lines.extend([normalized, ""])
        return lines if len(lines) > 2 else []

    @staticmethod
    def _render_legacy_key_tables(markdown: str) -> List[str]:
        lines = ["## 关键表格", ""]
        blocks = [
            block.strip()
            for block in re.split(r"(?=^###\s+)", str(markdown or ""), flags=re.MULTILINE)
            if block.strip()
        ]
        for index, block in enumerate(blocks, start=1):
            block_lines = block.splitlines()
            heading = re.match(r"^###\s+(.+)$", block_lines[0].strip()) if block_lines else None
            caption = heading.group(1) if heading else f"Table {index}"
            lines.extend([f"### {_compact_artifact_label(caption, '表', index)}", ""])
            location = next((line.strip() for line in block_lines if "来源位置" in line), "")
            if location:
                lines.extend([location, ""])
            table_text = "\n".join(line for line in block_lines if line.strip().startswith("|"))
            normalized = normalize_markdown_table(table_text)
            if normalized:
                lines.extend([normalized, ""])
        return lines if len(lines) > 2 else []

    @staticmethod
    def _render_figure_notes(figures: List[Dict[str, Any]]) -> List[str]:
        lines = ["## 图表解读", ""]
        for index, figure in enumerate(figures, start=1):
            if not isinstance(figure, dict):
                continue
            caption = str(figure.get("caption") or f"Figure {index}").strip()
            lines.extend([f"### {_compact_artifact_label(caption, '图', index)}", ""])
            description = str(figure.get("description") or "").strip()
            if description:
                lines.extend([description, ""])
            for key, label in (("trend", "趋势"), ("conditions", "适用条件")):
                value = str(figure.get(key) or "").strip()
                if value:
                    lines.append(f"- **{label}**：{value}")
            values = figure.get("key_values") or []
            if values:
                lines.append(f"- **关键数值**：{'；'.join(str(value) for value in values)}")
            lines.append("")
        return lines if len(lines) > 2 else []

    @staticmethod
    def _render_legacy_figure_notes(markdown: str) -> List[str]:
        lines = ["## 图表解读", ""]
        figure_index = 0
        for raw_line in str(markdown or "").replace("\r\n", "\n").splitlines():
            line = raw_line.strip()
            if not line or "论文正文说明" in line:
                continue
            heading = re.match(r"^###\s+(.+)$", line)
            if heading:
                figure_index += 1
                lines.extend([
                    f"### {_compact_artifact_label(heading.group(1), '图', figure_index)}",
                    "",
                ])
                continue
            lines.extend([line, ""])
        return lines if len(lines) > 2 else []

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
            name = re.sub(r"[_\s-]+", " ", heading.group(1).strip().lower())
            skipping = name in INTERNAL_SECTION_HEADINGS
            if skipping:
                continue
        if not skipping:
            output.append(line)
    text = "\n".join(output)
    text = re.sub(r"<!--\s*wiki-(?:system|claim)\s+.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text

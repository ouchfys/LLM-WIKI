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

from system.storage import get_object_storage


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
        repo_root = Path(__file__).resolve().parents[2]
        self.vault_dir = Path(vault_dir) if vault_dir else repo_root / "data" / "generated" / "wiki"

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
        repo_root = self.vault_dir.parent.parent
        return repo_root / path

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
        return str(path.relative_to(self.vault_dir.parent.parent))

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
            path = self.vault_dir.parent.parent / path
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
        now = datetime.now(timezone.utc).date().isoformat()
        tags = self._tags_for(page_type, related_topics)
        frontmatter = [
            "---",
            f"id: {self._yaml_scalar(card_id)}",
            f"title: {self._yaml_scalar(title)}",
            f"type: {self._yaml_scalar(page_type)}",
            "status: draft",
            f"created: {self._yaml_scalar(now)}",
            f"updated: {self._yaml_scalar(now)}",
            f"source_level: {self._yaml_scalar(source_level)}",
            "tags:",
        ]
        for tag in tags:
            frontmatter.append(f"  - {self._yaml_scalar(tag)}")
        frontmatter.extend([
            "sources:",
        ])
        if source_urls:
            for url in source_urls:
                frontmatter.append(f"  - url: {self._yaml_scalar(url)}")
                frontmatter.append(f"    level: {self._yaml_scalar(source_level)}")
        else:
            frontmatter.append("  []")

        frontmatter.append("related:")
        if related_topics:
            for topic in related_topics:
                frontmatter.append(f"  - {self._yaml_scalar(topic)}")
        else:
            frontmatter.append("  []")
        frontmatter.extend(["---", ""])

        body = [f"# {title}", ""]
        if summary:
            body.extend(["> [!summary] 一句话摘要", ">", f"> {summary.strip()}", ""])

        body.extend([
            "## 为什么值得记录",
            "",
            "- [ ] 这条内容和我的目标有什么关系？",
            "- [ ] 面试或项目展示时可以怎么讲？",
            "- [ ] 后续需要补哪篇论文或哪个概念？",
            "",
        ])

        body.extend(self._render_content_sections(page_type, content_json))

        if source_urls:
            body.extend(["## 来源", ""])
            for url in source_urls:
                label = source_level or "source"
                body.append(f"- [{label}] {url}")
            body.append("")

        if related_topics:
            body.extend(["## 关联页面", ""])
            for topic in related_topics:
                body.append(f"- [[{topic}]]")
            body.append("")

        body.extend([
            "## 我的笔记",
            "",
            "- ",
            "",
            "## 下次复习",
            "",
            "- [ ] 需要复习",
            "- [ ] 可以转成面试问答",
            "- [ ] 值得进入精读",
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
                path = self.vault_dir.parent.parent / path
            return path

        dirname = PAGE_TYPE_DIRS.get(page_type, "sources")
        slug = self._slugify(title) or card_id
        return self.vault_dir / dirname / f"{slug}.md"

    @staticmethod
    def _render_content_sections(page_type: str, content: Dict[str, Any]) -> List[str]:
        lines: List[str] = []
        if not content:
            return lines

        preferred = {
            "ConceptPage": [
                ("explanation", "核心概念"),
                ("examples", "例子"),
                ("related_concepts", "相关概念"),
            ],
            "PaperPage": [
                ("problem", "问题 / 背景"),
                ("key_idea", "核心思路"),
                ("authors", "作者"),
                ("year", "年份"),
                ("venue", "会议 / 来源"),
                ("key_contributions", "核心贡献"),
                ("methods", "方法"),
                ("method", "方法"),
                ("results", "结果 / 摘要"),
                ("key_takeaways", "关键要点"),
                ("interview_notes", "面试 / 项目表达"),
                ("notes", "原始记录"),
            ],
            "MethodPage": [
                ("category", "方法类别"),
                ("description", "方法说明"),
                ("when_to_use", "适用场景"),
                ("steps", "步骤"),
                ("comparison_to_alternatives", "和替代方案对比"),
            ],
            "ComparePage": [
                ("item_a", "对象 A"),
                ("item_b", "对象 B"),
                ("dimensions", "对比维度"),
            ],
            "InterviewQA": [
                ("question", "问题"),
                ("ideal_answer", "面试回答版本"),
                ("key_points", "关键点"),
                ("common_mistakes", "常见错误"),
            ],
            "MistakeNote": [
                ("mistake", "错误"),
                ("correction", "修正"),
                ("lesson", "教训"),
                ("context", "上下文"),
            ],
        }

        keys = preferred.get(page_type, [])
        seen = set()
        for key, label in keys:
            if key in content:
                lines.extend(MarkdownVault._render_value(label, content.get(key)))
                seen.add(key)
        for key, value in content.items():
            if key not in seen and not key.startswith("_"):
                lines.extend(MarkdownVault._render_value(key.replace("_", " "), value))
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

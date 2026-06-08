"""Markdown storage for raw source artifacts before Wiki compilation."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from system.storage import get_object_storage, get_storage_layout


class RawSourceVault:
    def __init__(self, base_dir: Optional[str] = None):
        repo_root = Path(__file__).resolve().parents[2]
        self.repo_root = repo_root
        self.base_dir = Path(base_dir) if base_dir else get_storage_layout().sources_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def write_source(
        self,
        source_kind: str,
        title: str,
        body_markdown: str,
        metadata: Optional[Dict[str, Any]] = None,
        source_urls: Optional[Iterable[str]] = None,
        local_files: Optional[Iterable[str]] = None,
        slug_hint: str = "",
    ) -> str:
        directory = self.base_dir / self._slugify(source_kind or "misc")
        directory.mkdir(parents=True, exist_ok=True)
        slug = self._slugify(slug_hint or title) or "untitled"
        path = directory / f"{slug}.md"

        frontmatter = [
            "---",
            f'title: "{self._escape(title)}"',
            f'type: "{self._escape(source_kind)}"',
            "source_urls:",
        ]
        urls = [url for url in (source_urls or []) if url]
        if urls:
            frontmatter.extend(f'  - "{self._escape(url)}"' for url in urls)
        else:
            frontmatter.append("  []")

        frontmatter.append("local_files:")
        files = [file for file in (local_files or []) if file]
        if files:
            frontmatter.extend(f'  - "{self._escape(file)}"' for file in files)
        else:
            frontmatter.append("  []")

        frontmatter.append("metadata:")
        meta = metadata or {}
        if meta:
            for key, value in meta.items():
                rendered = self._render_scalar(value)
                frontmatter.append(f"  {self._slugify_key(key)}: {rendered}")
        else:
            frontmatter.append("  {}")
        frontmatter.extend(["---", ""])

        markdown = "\n".join(frontmatter) + body_markdown.rstrip() + "\n"
        path.write_text(markdown, encoding="utf-8")
        storage = get_object_storage()
        storage_uri = storage.upload_text(storage.key_for_local_path(path), markdown)
        if storage.enabled:
            return storage_uri
        return path.relative_to(self.repo_root).as_posix()

    @staticmethod
    def _escape(value: Any) -> str:
        return str(value or "").replace('"', '\\"')

    @staticmethod
    def _slugify(value: str) -> str:
        value = (value or "").strip().lower()
        value = re.sub(r"[^\w\u4e00-\u9fff]+", "-", value)
        value = re.sub(r"-+", "-", value).strip("-")
        return value[:96]

    @staticmethod
    def _slugify_key(value: str) -> str:
        value = str(value or "").strip().lower()
        value = re.sub(r"[^a-z0-9_]+", "_", value)
        value = re.sub(r"_+", "_", value).strip("_")
        return value or "field"

    @classmethod
    def _render_scalar(cls, value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if value in (None, ""):
            return '""'
        return f'"{cls._escape(value)}"'

"""Canonical storage layout for the single-user LLM Wiki.

All durable user artifacts live under STORAGE_ROOT_PREFIX in object storage:

    users/admin/sources/   immutable or near-raw source artifacts
    users/admin/wiki/      LLM/agent compiled Markdown knowledge pages
    users/admin/queries/   generated query artifacts, indexes, and lint runs

Local paths with the same first-level names are developer caches only.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


class StorageLayout:
    def __init__(self, repo_root: str | Path | None = None):
        self.repo_root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[2]

    @property
    def sources_dir(self) -> Path:
        return self.repo_root / "sources"

    @property
    def wiki_dir(self) -> Path:
        return self.repo_root / "wiki"

    @property
    def queries_dir(self) -> Path:
        return self.repo_root / "queries"

    @property
    def maintenance_dir(self) -> Path:
        return self.repo_root / "maintenance"

    def source_dir(self, source_kind: str, source_id: str = "") -> Path:
        base = self.sources_dir / self.slug(source_kind or "misc")
        return base / self.slug(source_id) if source_id else base

    def source_markdown_path(self, source_kind: str, title: str, slug_hint: str = "") -> Path:
        slug = self.slug(slug_hint or title) or "untitled"
        return self.source_dir(source_kind) / f"{slug}.md"

    def source_asset_dir(self, source_kind: str, source_id: str) -> Path:
        return self.source_dir(source_kind, source_id) / "assets"

    def paper_upload_path(self, filename: str) -> Path:
        return self.sources_dir / "papers" / "uploads" / self.safe_filename(filename or "paper.pdf")

    def paper_original_key(self, filename: str) -> str:
        return f"sources/papers/originals/{self.safe_filename(filename or 'paper.pdf')}"

    def query_artifact_path(self, category: str, name: str, suffix: str = ".md") -> Path:
        safe_suffix = suffix if suffix.startswith(".") else f".{suffix}"
        return self.queries_dir / self.slug(category or "artifacts") / f"{self.slug(name) or 'artifact'}{safe_suffix}"

    def maintenance_artifact_path(self, category: str, name: str, suffix: str = ".json") -> Path:
        safe_suffix = suffix if suffix.startswith(".") else f".{suffix}"
        return self.maintenance_dir / self.slug(category or "artifacts") / f"{self.slug(name) or 'artifact'}{safe_suffix}"

    @staticmethod
    def slug(value: Any, limit: int = 96) -> str:
        text = str(value or "").strip().lower()
        text = re.sub(r"[^\w\u4e00-\u9fff]+", "-", text)
        text = re.sub(r"-+", "-", text).strip("-")
        return text[:limit]

    @staticmethod
    def safe_filename(filename: str, limit: int = 120) -> str:
        name = Path(filename or "file").name
        return "".join(ch if ch.isalnum() or ch in " ._-()" else "_" for ch in name)[:limit]


_LAYOUT: StorageLayout | None = None


def get_storage_layout() -> StorageLayout:
    global _LAYOUT
    if _LAYOUT is None:
        _LAYOUT = StorageLayout()
    return _LAYOUT

"""Application data paths, independent of the code checkout and current cwd.

PAPERWIKI_HOME defaults to ~/.paperwiki. An explicit test directory is itself
the data home; no extra .paperwiki directory is added inside it. Merely asking
for a layout path does not create directories.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from system.core import config


class StorageLayout:
    def __init__(self, repo_root: str | Path | None = None):
        configured_root = self._configured("PAPERWIKI_HOME") or self._configured("PAPERWIKI_WORKSPACE_ROOT")
        self.data_root = Path(repo_root or configured_root or Path.home() / ".paperwiki").expanduser().resolve()

    @staticmethod
    def _configured(name: str) -> str:
        return str(os.environ.get(name, getattr(config, name, "")) or "").strip()

    @property
    def repo_root(self) -> Path:
        """Compatibility alias: this is the data root, not the source repo."""
        return self.data_root

    @property
    def sessions_dir(self) -> Path:
        return self.data_root / "sessions"

    @property
    def database_path(self) -> Path:
        path = self.sessions_dir / "sessions.db"
        legacy = self.data_root / "sessions.db"
        # Keep an existing pre-home-layout database visible until migration.
        return legacy if not path.exists() and legacy.is_file() else path

    @property
    def memory_dir(self) -> Path:
        path = self.data_root / "memory"
        for legacy in (self.database_path.parent / ".paperwiki" / "memory",
                       self.data_root / ".paperwiki" / "memory"):
            if not path.exists() and legacy.is_dir():
                return legacy
        return path

    @property
    def repo_cache_dir(self) -> Path:
        return self.data_root / "cache" / "repos"

    @property
    def tmp_dir(self) -> Path:
        return self.data_root / "tmp"

    @property
    def archives_dir(self) -> Path:
        return self.data_root / "archives"

    @property
    def logs_dir(self) -> Path:
        return self.data_root / "logs"

    def scratch_dir(self, session_id: str = "") -> Path:
        name = self.slug(session_id) or "default"
        if re.fullmatch(r"con|prn|aux|nul|com[1-9]|lpt[1-9]", name, re.I):
            name = "_" + name
        return self.tmp_dir / name

    @property
    def sources_dir(self) -> Path:
        return self.repo_root / "sources"

    @property
    def projects_dir(self) -> Path:
        return self.repo_root / "projects"

    @property
    def wiki_dir(self) -> Path:
        return self.repo_root / "wiki"

    @property
    def queries_dir(self) -> Path:
        return self.archives_dir / "queries"

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

    def source_document_key(self, source_id: str, content_hash: str = "") -> str:
        """Stable object key for an optional parser artifact."""
        identity = self.slug(content_hash or source_id) or "unknown"
        return f"sources/papers/parser-artifacts/{identity}/document.json"

    def docling_json_key(self, source_id: str, content_hash: str = "") -> str:
        """Compatibility alias for migrations created before parser routing."""
        return self.source_document_key(source_id, content_hash)

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


def resolve_database_path(db_path: str | Path | None = None, *, create_parent: bool = True) -> Path:
    """Use one database location for HTTP handlers, workers and direct stores."""
    configured = os.getenv("PAPERWIKI_DB_PATH", getattr(config, "PAPERWIKI_DB_PATH", "")).strip()
    path = Path(db_path) if db_path else Path(configured) if configured else get_storage_layout().database_path
    path = path.expanduser().resolve()
    if create_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    return path

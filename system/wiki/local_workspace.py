"""Read-only, workspace-confined access to the local Markdown Wiki."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


class LocalWikiWorkspace:
    """Expose Codex-like listing/search/read without an unrestricted shell."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def list_markdown(self, *, cursor: int = 0, limit: int = 50) -> dict[str, Any]:
        files = self._files()
        start = max(0, int(cursor or 0))
        page_size = max(1, min(int(limit or 50), 100))
        page = files[start:start + page_size]
        next_cursor = start + len(page) if start + len(page) < len(files) else None
        return {
            "total": len(files),
            "cursor": start,
            "next_cursor": next_cursor,
            "items": [
                {"path": path.relative_to(self.root).as_posix(), "size_bytes": path.stat().st_size}
                for path in page
            ],
        }

    def search_markdown(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        terms = [term.lower() for term in re.findall(r"[\w\u4e00-\u9fff.-]+", str(query or "")) if term]
        if not terms:
            return []
        results: list[dict[str, Any]] = []
        for path in self._files():
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            lowered = text.lower()
            positions = [lowered.find(term) for term in terms]
            hits = sum(position >= 0 for position in positions)
            if not hits:
                continue
            first = min(position for position in positions if position >= 0)
            start = max(0, first - 180)
            end = min(len(text), first + 420)
            results.append({
                "path": path.relative_to(self.root).as_posix(),
                "score": hits,
                "snippet": " ".join(text[start:end].split()),
            })
        results.sort(key=lambda item: (-int(item["score"]), str(item["path"])))
        return results[: max(1, min(int(limit or 20), 50))]

    def read_markdown(
        self,
        relative_path: str,
        *,
        offset: int = 0,
        max_chars: int = 12_000,
    ) -> dict[str, Any]:
        path = self._resolve(relative_path)
        text = path.read_text(encoding="utf-8")
        start = max(0, int(offset or 0))
        size = max(500, min(int(max_chars or 12_000), 20_000))
        content = text[start:start + size]
        next_offset = start + len(content) if start + len(content) < len(text) else None
        return {
            "path": path.relative_to(self.root).as_posix(),
            "offset": start,
            "next_offset": next_offset,
            "total_chars": len(text),
            "content": content,
        }

    def _files(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted(
            (path for path in self.root.rglob("*.md") if path.is_file() and self._inside(path)),
            key=lambda path: path.relative_to(self.root).as_posix().lower(),
        )

    def _resolve(self, relative_path: str) -> Path:
        raw = Path(str(relative_path or ""))
        if raw.is_absolute() or raw.suffix.lower() != ".md":
            raise ValueError("Only relative Markdown paths inside the Wiki workspace are readable.")
        path = (self.root / raw).resolve()
        if not self._inside(path) or not path.is_file():
            raise ValueError("Markdown path is outside the Wiki workspace or does not exist.")
        return path

    def _inside(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root)
            return True
        except ValueError:
            return False

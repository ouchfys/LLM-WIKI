"""Parser-neutral document model used by the paper ingestion workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ParsedDocument:
    title: str = ""
    text: str = ""
    markdown: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    blocks: list[dict[str, Any]] = field(default_factory=list)
    elements: list[dict[str, Any]] = field(default_factory=list)
    figures: list[dict[str, Any]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    parser: str = ""
    # Optional lossless provider artifact. The legacy database column that
    # stores this payload is retained so old installations can be upgraded
    # without a destructive schema migration.
    source_document: dict[str, Any] = field(default_factory=dict)

    def as_pipeline_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "summary": _summary(self.text or self.markdown),
            "metadata": self.metadata,
            "blocks": self.blocks,
            "markdown": self.markdown,
            "elements": self.elements,
            "figures": self.figures,
            "tables": self.tables,
            "source_document": self.source_document,
            "parser_used": self.parser or self.metadata.get("parser", "unknown"),
        }


def _summary(text: str, limit: int = 600) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[:limit].rsplit(" ", 1)[0] + "..."

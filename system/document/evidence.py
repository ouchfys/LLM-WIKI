"""Build stable evidence records without depending on a parsing vendor."""

from __future__ import annotations

import hashlib
from typing import Any


def stable_evidence_id(source_key: str, kind: str, ref: str, index: int = 0) -> str:
    seed = f"{source_key}|{kind}|{ref}|{index}".encode("utf-8")
    return f"ev-{hashlib.sha256(seed).hexdigest()[:24]}"


def elements_from_blocks(blocks: list[dict[str, Any]], source_key: str) -> list[dict[str, Any]]:
    """Project parser blocks into the stable evidence shape.

    Page and bbox are optional locators. They are deliberately not fabricated
    for HTML sources and are never required by retrieval or Wiki compilation.
    """
    elements: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        text = " ".join(str(block.get("text") or "").split())
        if not text:
            continue
        metadata = dict(block.get("metadata") or {})
        ref = str(metadata.get("source_ref") or metadata.get("docling_ref") or f"block/{index}")
        element_type = str(block.get("block_type") or "text")
        heading = str(block.get("section") or "Body")
        elements.append({
            "element_id": stable_evidence_id(source_key, element_type, ref, index),
            "element_type": element_type,
            "text": text,
            "caption": str(block.get("caption") or ""),
            "page": int(block.get("page") or 0),
            "bbox": metadata.get("bbox") or {},
            "heading_path": list(metadata.get("heading_path") or ([heading] if heading else [])),
            "parent_id": str(metadata.get("parent_id") or ""),
            "reading_order": index,
            # Compatibility field used by the existing SQLite schema. Values
            # may now be HTML anchors or MinerU block references.
            "docling_ref": ref,
            "metadata": metadata,
        })
    return elements


def matrix_to_markdown(headers: list[list[str]], rows: list[list[str]]) -> str:
    if not headers and not rows:
        return ""
    width = max((len(row) for row in headers + rows), default=0)
    if not width:
        return ""
    header = (headers[-1] if headers else [f"column_{i + 1}" for i in range(width)])[:width]
    header += [""] * (width - len(header))
    lines = ["| " + " | ".join(_escape(value) for value in header) + " |"]
    lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
    for row in rows:
        padded = list(row[:width]) + [""] * max(0, width - len(row))
        lines.append("| " + " | ".join(_escape(value) for value in padded) + " |")
    return "\n".join(lines)


def _escape(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()

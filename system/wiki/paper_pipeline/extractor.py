from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path
from typing import Any

from system.document.evidence import elements_from_blocks
from system.document.parser_router import PaperParserRouter
from system.storage import get_object_storage, get_storage_layout
from system.wiki.paper_pipeline.models import SourceElement, SourcePacket, SourceSection, SourceTable
from system.wiki.paper_pipeline.store import PaperWikiPipelineStore
from system.wiki.raw_source_vault import RawSourceVault
from system.wiki.wiki_builder import sanitize_wiki_text


def extract_paper_source(
    pdf_path: Path,
    source_url: str = "",
    store: PaperWikiPipelineStore | None = None,
) -> SourcePacket:
    source_hash = _file_sha256(pdf_path)
    if store:
        cached = store.find_source_packet_by_hash(source_hash)
        if (
            cached
            and cached.elements
            and cached.blocks
            and cached.parser_used in {"arxiv-html", "mineru-vlm", "pymupdf-fallback"}
        ):
            cached.metadata = {
                **cached.metadata,
                "extraction_cache_hit": True,
                "extraction_cache_source": cached.source_id,
            }
            return cached
    parsed = _parse_pdf(pdf_path, source_url=source_url, source_key=source_hash)
    title = sanitize_wiki_text(parsed.get("title") or pdf_path.stem).strip() or pdf_path.stem
    summary = sanitize_wiki_text(parsed.get("summary") or "")
    if not summary:
        summary = _quick_pdf_abstract(pdf_path)
    metadata = parsed.get("metadata") or {}
    blocks = parsed.get("blocks") or []
    source_document = parsed.get("source_document") if isinstance(parsed.get("source_document"), dict) else {}
    normalized_elements = parsed.get("elements") if isinstance(parsed.get("elements"), list) else []
    normalized_tables = parsed.get("tables") if isinstance(parsed.get("tables"), list) else []
    if not normalized_elements:
        normalized_elements = elements_from_blocks(blocks, source_hash)
    else:
        blocks = _blocks_from_elements(normalized_elements, str(pdf_path))
    raw_markdown = parsed.get("markdown") or _paper_markdown_from_blocks(title, summary, blocks)
    raw_markdown = sanitize_wiki_text(raw_markdown)
    if not raw_markdown:
        raw_markdown = sanitize_wiki_text(_paper_markdown_from_blocks(title, summary, blocks))

    pdf_storage_uri = get_object_storage().upload_file(
        pdf_path,
        key=get_storage_layout().paper_original_key(pdf_path.name),
        content_type="application/pdf",
    )
    source_urls = [source_url] if source_url else [f"file://{pdf_path.resolve().as_posix()}"]
    raw_source_path = RawSourceVault().write_source(
        source_kind="paper_pdf",
        title=title,
        body_markdown=raw_markdown,
        metadata={
            "parser": parsed.get("parser_used", "unknown"),
            "pdf_path": str(pdf_path),
            "pdf_storage_uri": pdf_storage_uri,
            "page_count": metadata.get("page_count", 0),
            "pipeline": "wiki_compile",
        },
        source_urls=source_urls,
        local_files=[str(pdf_path)],
        slug_hint=pdf_path.stem,
    )

    packet = SourcePacket(
        source_id=str(uuid.uuid4()),
        source_type="paper_pdf",
        title=title,
        abstract=summary,
        metadata=metadata,
        source_urls=source_urls,
        raw_source_path=raw_source_path,
        pdf_storage_uri=pdf_storage_uri,
        parser_used=parsed.get("parser_used", "unknown"),
        source_hash=source_hash,
        sections=_sections_from_blocks(normalized_elements or blocks, title, summary, raw_markdown),
        blocks=blocks,
        # Compatibility name in the persisted model; the payload is now
        # parser-neutral and intentionally small.
        docling_json=source_document,
        elements=[SourceElement(**item) for item in normalized_elements],
        tables=[SourceTable(**item) for item in normalized_tables],
    )
    if store:
        packet.source_id = store.upsert_source_packet(packet)
    return packet


def _parse_pdf(pdf_path: Path, *, source_url: str = "", source_key: str = "") -> dict[str, Any]:
    parsed = PaperParserRouter().parse(pdf_path, source_url=source_url, source_key=source_key)
    result = parsed.as_pipeline_dict()
    result["summary"] = _make_summary(parsed.text or parsed.markdown)
    return result


def _sections_from_blocks(
    blocks: list[dict[str, Any]],
    title: str,
    summary: str,
    raw_markdown: str,
) -> list[SourceSection]:
    sections: list[SourceSection] = []
    if summary:
        sections.append(SourceSection(section_id="abstract", heading="Abstract", text=summary, page_start=1, page_end=1))

    grouped: dict[str, dict[str, Any]] = {}
    for block in blocks:
        text = sanitize_wiki_text(block.get("text", ""))
        if not text:
            continue
        heading_path = block.get("heading_path") if isinstance(block.get("heading_path"), list) else []
        heading = sanitize_wiki_text(block.get("section", "") or (heading_path[-1] if heading_path else "")) or "Body"
        section_id = _section_id(heading)
        item = grouped.setdefault(
            section_id,
            {"heading": heading, "texts": [], "page_start": int(block.get("page") or 0), "page_end": int(block.get("page") or 0), "evidence_ids": []},
        )
        item["texts"].append(text)
        page = int(block.get("page") or 0)
        if page:
            item["page_start"] = min(item["page_start"] or page, page)
            item["page_end"] = max(item["page_end"] or page, page)
        evidence_id = str(block.get("element_id") or "")
        if evidence_id and evidence_id not in item["evidence_ids"]:
            item["evidence_ids"].append(evidence_id)

    for section_id, item in grouped.items():
        text = sanitize_wiki_text("\n\n".join(item["texts"]))
        if not text:
            continue
        sections.append(
            SourceSection(
                section_id=section_id,
                heading=item["heading"],
                text=text[:6000],
                page_start=item["page_start"],
                page_end=item["page_end"],
                evidence_ids=item["evidence_ids"],
            )
        )
        if len(sections) >= 18:
            break

    if len(sections) <= 1 and raw_markdown:
        sections.extend(_sections_from_markdown(raw_markdown, title))
    return sections


def _sections_from_markdown(markdown: str, title: str) -> list[SourceSection]:
    text = sanitize_wiki_text(markdown)
    headings = list(re.finditer(r"^#{1,6}\s+(.+?)\s*$", text, re.MULTILINE))
    sections: list[SourceSection] = []
    for index, match in enumerate(headings[:18]):
        heading = match.group(1).strip()
        if heading.lower() == title.lower():
            continue
        start = match.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        body = text[start:end].strip()
        if body:
            sections.append(SourceSection(section_id=_section_id(heading), heading=heading, text=body[:6000]))
    return sections


def _chunk_markdown_to_blocks(markdown: str, source_path: str) -> list[dict[str, Any]]:
    blocks = []
    paragraphs = re.split(r"\n\s*\n", markdown or "")
    current_section = ""
    for idx, para in enumerate(paragraphs):
        para = para.strip()
        if not para:
            continue
        heading_match = re.match(r"^#{1,3}\s*(.+)", para)
        if heading_match:
            current_section = heading_match.group(1).strip()
        blocks.append({
            "page": idx + 1,
            "section": current_section,
            "block_type": "text",
            "text": para[:3000],
            "metadata": {"source": source_path},
        })
    return blocks


def _paper_markdown_from_blocks(title: str, summary: str, blocks: list[dict[str, Any]]) -> str:
    lines = [f"# {title}", ""]
    if summary:
        lines.extend(["## Summary", "", summary, ""])
    current_section = None
    for block in blocks[:240]:
        section = (block.get("section") or "").strip()
        text = (block.get("text") or "").strip()
        if not text:
            continue
        if section and section != current_section:
            current_section = section
            lines.extend([f"## {section}", ""])
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


def _quick_pdf_abstract(pdf_path: Path) -> str:
    try:
        import fitz
    except Exception:
        return ""
    try:
        doc = fitz.open(str(pdf_path))
        text = "\n".join(doc[index].get_text() or "" for index in range(min(doc.page_count, 2)))
        doc.close()
    except Exception:
        return ""
    match = re.search(
        r"\bAbstract\b\s*(.*?)(?:\n\s*(?:1\.?\s*)?Introduction\b|\n\s*Keywords?\b)",
        text.replace("\r\n", "\n").replace("\r", "\n"),
        flags=re.IGNORECASE | re.DOTALL,
    )
    return _make_summary(match.group(1), max_len=900) if match else ""


def _make_summary(text: str, max_len: int = 600) -> str:
    text = " ".join(sanitize_wiki_text(text or "").split())
    if len(text) <= max_len:
        return text
    return text[:max_len].rsplit(" ", 1)[0] + "..."


def _section_id(heading: str) -> str:
    normalized = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "-", (heading or "section").strip().lower())
    return normalized.strip("-")[:80] or "section"


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _blocks_from_elements(elements: list[dict[str, Any]], source_path: str) -> list[dict[str, Any]]:
    blocks = []
    for element in elements:
        text = sanitize_wiki_text(str(element.get("text") or element.get("caption") or ""))
        if not text:
            continue
        heading_path = element.get("heading_path") if isinstance(element.get("heading_path"), list) else []
        blocks.append({
            "page": int(element.get("page") or 0),
            "section": str(heading_path[-1] if heading_path else ""),
            "block_type": str(element.get("element_type") or "text"),
            "text": text,
            "metadata": {
                "source": source_path,
                "evidence_id": str(element.get("element_id") or ""),
                "bbox": element.get("bbox") or {},
                "heading_path": heading_path,
                "source_ref": str(element.get("docling_ref") or ""),
            },
        })
    return blocks

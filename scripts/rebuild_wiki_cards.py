from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.deps import get_chunk_index, get_fast_llm, get_wiki_store
from system.wiki.wiki_builder import WikiBuilder, heuristic_compile_raw_markdown, sanitize_wiki_text


PRESERVE_FIELDS = {
    "raw_source_path",
    "pdf_storage_uri",
    "source_type",
    "attachments",
    "image_urls",
    "downloaded_images",
    "_ocr_text",
    "_ocr_notes",
    "_ocr_status",
    "tags",
}


def read_raw_markdown(raw_source_path: str) -> str:
    if not raw_source_path:
        return ""
    path = Path(raw_source_path)
    if not path.is_absolute():
        path = REPO_ROOT / raw_source_path
    if not path.exists():
        return ""
    return sanitize_wiki_text(path.read_text(encoding="utf-8"))


def source_kind_for(card: dict) -> str:
    content = card.get("content_json") or {}
    if content.get("source_kind"):
        return str(content["source_kind"])
    if content.get("source_type"):
        return str(content["source_type"])
    if card.get("page_type") == "PaperPage":
        return "paper_pdf"
    return card.get("page_type") or "wiki_card"


def main() -> None:
    store = get_wiki_store()
    chunk_index = get_chunk_index()
    llm = get_fast_llm()
    if not llm:
        raise RuntimeError("Wiki compiler LLM unavailable.")

    builder = WikiBuilder(llm)
    cards = store.get_recent_cards(limit=10000)
    rebuilt = 0
    skipped = 0
    failed = 0

    for card in cards:
        content = card.get("content_json") or {}
        raw_source_path = content.get("raw_source_path") or ""
        raw_markdown = read_raw_markdown(raw_source_path)
        if not raw_markdown:
            skipped += 1
            continue

        source_kind = source_kind_for(card)
        compiled = builder.build_from_raw_markdown(
            title=card.get("title", ""),
            raw_markdown=raw_markdown,
            page_type=card.get("page_type") or "SourceNote",
            source_kind=source_kind,
        )
        if not compiled:
            compiled = heuristic_compile_raw_markdown(
                title=card.get("title", ""),
                raw_markdown=raw_markdown,
                page_type=card.get("page_type") or "SourceNote",
                source_kind=source_kind,
            )

        preserved = {
            key: value
            for key, value in content.items()
            if key in PRESERVE_FIELDS and value not in (None, "", [], {})
        }
        next_content = dict(compiled.get("content_json") or {})
        next_content.update(preserved)
        next_summary = (
            sanitize_wiki_text(compiled.get("summary", ""))
            or sanitize_wiki_text(str(next_content.get("problem") or next_content.get("key_idea") or next_content.get("notes") or ""))
            or "原始资料已入库，已生成本地 Wiki 草稿。"
        )[:240]
        store.update_card(
            card["id"],
            title=compiled.get("title") or card.get("title", ""),
            page_type=compiled.get("page_type") or card.get("page_type", ""),
            summary=next_summary,
            content_json=next_content,
            source_level=compiled.get("source_level") or card.get("source_level", ""),
            related_topics_json=compiled.get("related_topics") or card.get("related_topics") or [],
        )
        rebuilt += 1

        chunk_index.reindex_card(
            card_id=card["id"],
            raw_source_path=raw_source_path,
            markdown_path=card.get("markdown_path", ""),
            source_kind=source_kind,
        )

    print(json.dumps({"rebuilt": rebuilt, "failed": failed, "skipped": skipped}, ensure_ascii=False))


if __name__ == "__main__":
    main()

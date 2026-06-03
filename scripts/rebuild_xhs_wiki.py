from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.deps import get_chunk_index, get_wiki_store
from backend.api.wiki import (
    _sanitize_ocr_text,
    _structure_ocr_text,
    _visible_ocr_excerpt,
    _write_xhs_raw_markdown,
)


def main() -> None:
    store = get_wiki_store()
    chunk_index = get_chunk_index()
    cards = store.get_cards_by_type("InterviewQA", limit=1000)

    rebuilt = 0
    skipped = 0

    for card in cards:
        content = card.get("content_json") or {}
        source_type = content.get("source_type", "")
        if source_type != "xiaohongshu":
            continue

        note_id = Path(card.get("content_json", {}).get("raw_source_path", "")).stem or card["id"]
        source_urls = card.get("source_urls") or []
        source_url = source_urls[0] if source_urls else ""
        raw_ocr = content.get("_ocr_text") or content.get("ocr_text") or content.get("notes") or ""
        cleaned_ocr = _sanitize_ocr_text(raw_ocr)
        structured_ocr = _structure_ocr_text(cleaned_ocr)

        if not cleaned_ocr and not source_url:
            skipped += 1
            continue

        content["ocr_excerpt"] = _visible_ocr_excerpt(cleaned_ocr)
        content["ocr_notes"] = structured_ocr
        content["_ocr_text"] = cleaned_ocr
        content["_ocr_notes"] = structured_ocr
        content["notes"] = content.get("notes") or structured_ocr or _visible_ocr_excerpt(cleaned_ocr)

        raw_path = _write_xhs_raw_markdown(
            title=card.get("title", ""),
            source_url=source_url,
            note_text=content.get("notes", ""),
            image_urls=content.get("image_urls") or [],
            downloaded_images=content.get("downloaded_images") or [],
            ocr_text=structured_ocr or cleaned_ocr,
            ocr_status=content.get("_ocr_status") or content.get("ocr_status") or "done",
            tags=card.get("related_topics") or content.get("tags") or [],
            slug_hint=note_id,
        )
        content["raw_source_path"] = raw_path

        store.update_card(card["id"], content_json=content)
        chunk_index.reindex_card(
            card_id=card["id"],
            raw_source_path=raw_path,
            markdown_path=card.get("markdown_path", ""),
            source_kind="xiaohongshu",
        )
        rebuilt += 1

    print(json.dumps({"rebuilt": rebuilt, "skipped": skipped}, ensure_ascii=False))


if __name__ == "__main__":
    main()

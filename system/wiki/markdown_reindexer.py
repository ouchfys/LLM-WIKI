"""Reindex canonical Markdown wiki cards into SQLite cache tables."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.storage import get_object_storage
from system.wiki.chunk_index import WikiChunkIndex
from system.wiki.markdown_parser import ParsedMarkdownCard, content_json_from_sections, parse_markdown_card
from system.wiki.wiki_store import WikiStore


class MarkdownWikiReindexer:
    """Treat Markdown as canonical input and SQLite as an index/cache."""

    def __init__(self, db_path: str | None = None):
        self.wiki_store = WikiStore(db_path=db_path)
        self.db_path = self.wiki_store.db_path
        self.chunk_index = WikiChunkIndex(db_path=self.db_path)

    def reindex_reference(self, reference: str) -> dict[str, Any]:
        markdown = get_object_storage().read_text(reference)
        if not markdown:
            path = Path(reference)
            markdown = path.read_text(encoding="utf-8") if path.exists() else ""
        if not markdown:
            raise FileNotFoundError(f"Markdown reference not found: {reference}")
        card = parse_markdown_card(markdown)
        return self.reindex_card(card=card, markdown_path=reference)

    def reindex_card(self, card: ParsedMarkdownCard, markdown_path: str) -> dict[str, Any]:
        source_urls = [source["url"] for source in card.sources if source.get("url")]
        related = _unique(card.related)
        content_json = content_json_from_sections(card)
        content_json.update({
            "markdown_status": card.status,
            "aliases": card.aliases,
            "sources": card.sources,
        })

        now = _now_iso()
        dedupe_key = self.wiki_store.make_dedupe_key(
            title=card.title,
            page_type=card.page_type,
            source_urls=source_urls,
        )
        action = "updated" if self.wiki_store.get_card(card.id) else "created"
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            existing = conn.execute("SELECT id, created_at FROM wiki_pages WHERE id = ?", (card.id,)).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE wiki_pages
                    SET title = ?, page_type = ?, markdown_path = ?, dedupe_key = ?,
                        summary = ?, content_json = ?, source_level = ?,
                        source_urls_json = ?, related_topics_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        card.title,
                        card.page_type,
                        markdown_path,
                        dedupe_key,
                        card.summary,
                        json.dumps(content_json, ensure_ascii=False),
                        card.source_level,
                        json.dumps(source_urls, ensure_ascii=False),
                        json.dumps(related, ensure_ascii=False),
                        now,
                        card.id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO wiki_pages
                    (id, title, page_type, markdown_path, dedupe_key, summary, content_json,
                     source_level, source_urls_json, related_topics_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        card.id,
                        card.title,
                        card.page_type,
                        markdown_path,
                        dedupe_key,
                        card.summary,
                        json.dumps(content_json, ensure_ascii=False),
                        card.source_level,
                        json.dumps(source_urls, ensure_ascii=False),
                        json.dumps(related, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
            self._replace_aliases(conn, card)
            self._replace_markdown_links(conn, card)
            self._upsert_markdown_sources(conn, card)
            conn.commit()

        chunks = self.chunk_index.reindex_card(
            card_id=card.id,
            markdown_path=markdown_path,
            source_kind="markdown_wiki",
        )
        return {
            "ok": True,
            "action": action,
            "card_id": card.id,
            "title": card.title,
            "page_type": card.page_type,
            "chunks": chunks,
            "aliases": len(card.aliases),
            "sources": len(source_urls),
            "related": len(related),
        }

    def _replace_aliases(self, conn: sqlite3.Connection, card: ParsedMarkdownCard) -> None:
        conn.execute("DELETE FROM wiki_aliases WHERE card_id = ?", (card.id,))
        for alias in _unique([card.title, *card.aliases]):
            normalized = normalize_alias(alias)
            if normalized:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO wiki_aliases(card_id, alias, normalized_alias)
                    VALUES (?, ?, ?)
                    """,
                    (card.id, alias, normalized),
                )

    def _replace_markdown_links(self, conn: sqlite3.Connection, card: ParsedMarkdownCard) -> None:
        conn.execute(
            "DELETE FROM wiki_card_links WHERE from_card_id = ? AND source_packet_id = 'markdown'",
            (card.id,),
        )
        for related in _unique(card.related):
            target = conn.execute(
                """
                SELECT id FROM wiki_pages
                WHERE id = ? OR lower(title) = lower(?)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (related, related),
            ).fetchone()
            if not target or target["id"] == card.id:
                continue
            conn.execute(
                """
                INSERT INTO wiki_card_links
                (id, from_card_id, to_card_id, relation_type, source_packet_id, evidence_text, created_at)
                VALUES (?, ?, ?, 'related', 'markdown', ?, ?)
                """,
                (str(uuid.uuid4()), card.id, target["id"], related, _now_iso()),
            )

    def _upsert_markdown_sources(self, conn: sqlite3.Connection, card: ParsedMarkdownCard) -> None:
        conn.execute(
            """
            DELETE FROM wiki_card_sources
            WHERE card_id = ?
              AND source_card_id = ''
              AND raw_source_path = ''
              AND section_id = 'frontmatter'
            """,
            (card.id,),
        )
        for source in card.sources:
            url = source.get("url", "")
            if not url:
                continue
            source_packet_id = source.get("source_packet_id") or "markdown"
            level = source.get("level") or card.source_level or "source"
            conn.execute(
                """
                INSERT INTO wiki_card_sources
                (id, card_id, source_packet_id, raw_source_path, source_url,
                 section_id, evidence_text, claim_text, confidence, created_at)
                VALUES (?, ?, ?, '', ?, 'frontmatter', ?, '', 1.0, ?)
                """,
                (str(uuid.uuid4()), card.id, source_packet_id, url, level, _now_iso()),
            )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            output.append(text)
    return output


def normalize_alias(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[\u2010-\u2015_+-]+", " ", value)
    value = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()

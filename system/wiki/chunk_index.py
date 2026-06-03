"""
Unified wiki chunk index — markdown-first FTS5 retrieval for all sources.

Replaces paper_blocks as the single chunk-level search layer.
Every source (paper, XHS, image OCR, text note) eventually lands here.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from system.storage import get_object_storage


class WikiChunkIndex:
    """SQLite FTS5 chunk index for all wiki content."""

    def __init__(self, db_path: str = None):
        repo_root = Path(__file__).resolve().parents[2]
        path = Path(db_path) if db_path else repo_root / "sessions.db"
        self.db_path = str(path)
        self._fts_enabled = False
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _init_db(self):
        with closing(self._connect()) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS wiki_chunks (
                    id TEXT PRIMARY KEY,
                    card_id TEXT NOT NULL,
                    source_kind TEXT DEFAULT '',
                    chunk_index INTEGER NOT NULL DEFAULT 0,
                    section TEXT DEFAULT '',
                    page INTEGER,
                    text TEXT NOT NULL,
                    raw_source_path TEXT DEFAULT '',
                    markdown_path TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_wiki_chunks_card "
                "ON wiki_chunks(card_id)"
            )
            self._init_fts(conn)
            conn.commit()

    def _init_fts(self, conn: sqlite3.Connection):
        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS wiki_chunks_fts
                USING fts5(text, content='wiki_chunks', content_rowid='rowid')
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS wiki_chunks_ai
                AFTER INSERT ON wiki_chunks BEGIN
                    INSERT INTO wiki_chunks_fts(rowid, text)
                    VALUES (new.rowid, new.text);
                END;
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS wiki_chunks_ad
                AFTER DELETE ON wiki_chunks BEGIN
                    INSERT INTO wiki_chunks_fts(wiki_chunks_fts, rowid, text)
                    VALUES ('delete', old.rowid, old.text);
                END;
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS wiki_chunks_au
                AFTER UPDATE ON wiki_chunks BEGIN
                    INSERT INTO wiki_chunks_fts(wiki_chunks_fts, rowid, text)
                    VALUES ('delete', old.rowid, old.text);
                    INSERT INTO wiki_chunks_fts(rowid, text)
                    VALUES (new.rowid, new.text);
                END;
            """)
            conn.execute(
                "INSERT INTO wiki_chunks_fts(wiki_chunks_fts) VALUES ('rebuild')"
            )
            self._fts_enabled = True
        except sqlite3.OperationalError:
            self._fts_enabled = False

    # ------------------------------------------------------------------
    #  Chunking
    # ------------------------------------------------------------------

    def reindex_card(
        self,
        card_id: str,
        raw_source_path: str = "",
        markdown_path: str = "",
        source_kind: str = "",
    ) -> int:
        """Delete old chunks for this card, then re-chunk from markdown files.

        Tries raw_source_path first (full original text), then markdown_path.
        Returns the number of chunks written.
        """
        self.delete_chunks(card_id)

        text = self._read_markdown(raw_source_path) or self._read_markdown(markdown_path)
        if not text:
            return 0

        chunks = self._chunk_markdown(text)
        if not chunks:
            return 0

        now = self._now_iso()
        with closing(self._connect()) as conn:
            for idx, chunk in enumerate(chunks):
                conn.execute(
                    """INSERT INTO wiki_chunks
                       (id, card_id, source_kind, chunk_index, section, page, text,
                        raw_source_path, markdown_path, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(uuid.uuid4()),
                        card_id,
                        source_kind,
                        idx,
                        chunk.get("section", ""),
                        chunk.get("page"),
                        chunk["text"],
                        raw_source_path,
                        markdown_path,
                        now,
                        now,
                    ),
                )
            conn.commit()
        return len(chunks)

    def delete_chunks(self, card_id: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "DELETE FROM wiki_chunks WHERE card_id = ?", (card_id,)
            )
            conn.commit()

    # ------------------------------------------------------------------
    #  Search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        limit: int = 10,
        card_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Full-text search chunks, returning matched chunks with card info.

        Returns list of dicts with: card_id, title, page_type, chunk_text,
        section, page, markdown_path, chunk_id.
        """
        if not query or not query.strip():
            return []

        with closing(self._connect()) as conn:
            if self._fts_enabled:
                return self._search_fts(conn, query, limit, card_ids)
            return self._search_like(conn, query, limit, card_ids)

    def _search_fts(
        self, conn: sqlite3.Connection, query: str, limit: int,
        card_ids: Optional[List[str]],
    ) -> List[Dict[str, Any]]:
        terms = self._fts_terms(query)
        if not terms:
            return []

        fts_query = " OR ".join(terms)
        card_filter = ""
        params: list = []
        if card_ids:
            placeholders = ",".join("?" for _ in card_ids)
            card_filter = f"AND c.card_id IN ({placeholders})"
            params = list(card_ids)

        try:
            rows = conn.execute(
                f"""SELECT c.id AS chunk_id, c.card_id, c.section, c.page,
                           c.text, c.markdown_path, c.raw_source_path, c.chunk_index,
                           p.title, p.page_type
                    FROM wiki_chunks_fts
                    JOIN wiki_chunks AS c ON c.rowid = wiki_chunks_fts.rowid
                    LEFT JOIN wiki_pages AS p ON p.id = c.card_id
                    WHERE wiki_chunks_fts MATCH ? {card_filter}
                    ORDER BY rank
                    LIMIT ?""",
                [fts_query] + params + [limit],
            ).fetchall()
        except sqlite3.OperationalError:
            return self._search_like(conn, query, limit, card_ids)

        return [self._row_to_result(row) for row in rows]

    def _search_like(
        self, conn: sqlite3.Connection, query: str, limit: int,
        card_ids: Optional[List[str]],
    ) -> List[Dict[str, Any]]:
        like = f"%{query}%"
        card_filter = ""
        params: list = [like, limit]
        if card_ids:
            placeholders = ",".join("?" for _ in card_ids)
            card_filter = f"AND c.card_id IN ({placeholders})"
            params = [like] + list(card_ids) + [limit]

        rows = conn.execute(
            f"""SELECT c.id AS chunk_id, c.card_id, c.section, c.page,
                       c.text, c.markdown_path, c.raw_source_path, c.chunk_index,
                       p.title, p.page_type
                FROM wiki_chunks AS c
                LEFT JOIN wiki_pages AS p ON p.id = c.card_id
                WHERE c.text LIKE ? {card_filter}
                ORDER BY c.chunk_index ASC
                LIMIT ?""",
            params,
        ).fetchall()
        return [self._row_to_result(row) for row in rows]

    # ------------------------------------------------------------------
    #  Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_result(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "chunk_id": row["chunk_id"],
            "card_id": row["card_id"],
            "title": row["title"] or "",
            "page_type": row["page_type"] or "",
            "section": row["section"] or "",
            "page": row["page"],
            "text": row["text"],
            "markdown_path": row["markdown_path"] or "",
            "raw_source_path": row["raw_source_path"] or "",
            "chunk_index": row["chunk_index"],
        }

    @staticmethod
    def _fts_terms(query: str) -> List[str]:
        terms = []
        stop = {
            "and", "or", "the", "for", "with", "from", "what", "how", "why",
            "are", "is", "was", "were", "this", "that", "into", "about",
            "什么", "怎么", "如何", "为什么", "区别", "不同", "对比", "比较",
            "主流", "技术", "里面", "这个", "那个", "一下", "哪些", "相关",
            "内容", "最近", "保存", "记录", "帮我", "总结",
        }
        # Multi-character CJK
        for block in re.findall(r"[一-鿿]{2,}", query):
            if block.lower() not in stop:
                terms.append(f'"{block}"')
        # English words
        for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_\-]{1,}", query):
            if token.lower() not in stop:
                terms.append(f'"{token}"')
        return terms[:8]

    def _read_markdown(self, path: str) -> Optional[str]:
        if not path:
            return None
        if path.startswith(("oss://", "local://")):
            try:
                text = get_object_storage().read_text(path)
                return self._sanitize_markdown_for_indexing(text) if text else None
            except Exception:
                return None
        repo_root = Path(__file__).resolve().parents[2]
        file_path = Path(path)
        if not file_path.is_absolute():
            file_path = repo_root / path
        if not file_path.exists():
            return None
        try:
            return self._sanitize_markdown_for_indexing(file_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    @staticmethod
    def _sanitize_markdown_for_indexing(text: str) -> str:
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        if not text:
            return ""

        text = re.sub(r"!\[[^\]]*\]\(data:image/[^)]+\)", "", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+", "", text, flags=re.IGNORECASE)
        text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)

        cleaned_lines: List[str] = []
        for raw_line in text.split("\n"):
            line = raw_line.strip()
            if not line:
                cleaned_lines.append("")
                continue
            compact = re.sub(r"\s+", "", line)
            if len(compact) >= 256 and re.fullmatch(r"[A-Za-z0-9+/=_-]+", compact):
                continue
            cleaned_lines.append(raw_line)

        text = "\n".join(cleaned_lines)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _chunk_markdown(text: str, target_chars: int = 800) -> List[Dict[str, Any]]:
        """Split markdown into chunks by heading sections.

        Each `## Heading` starts a new section. Sections longer than
        target_chars are further split by paragraphs.
        """
        # Strip YAML frontmatter
        text = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.DOTALL).strip()
        if not text:
            return []

        # Split on ## headings
        sections = re.split(r"\n(?=## )", text)
        chunks: List[Dict[str, Any]] = []
        current_section = ""
        current_page = None

        for section in sections:
            section = section.strip()
            if not section:
                continue

            heading_match = re.match(r"^## (.+)", section)
            if heading_match:
                current_section = heading_match.group(1).strip()

            # If section is short enough, keep as one chunk
            if len(section) <= target_chars * 2:
                chunks.append({
                    "section": current_section,
                    "page": current_page,
                    "text": section[:3000],
                })
                continue

            # Split long sections by paragraphs
            heading_line = ""
            body = section
            if section.startswith("## "):
                lines = section.split("\n", 1)
                heading_line = lines[0] + "\n"
                body = lines[1] if len(lines) > 1 else ""

            paragraphs = re.split(r"\n\s*\n", body)
            buffer = heading_line
            for para in paragraphs:
                para = para.strip()
                if not para:
                    continue
                if len(buffer) + len(para) > target_chars and buffer.strip():
                    chunks.append({
                        "section": current_section,
                        "page": current_page,
                        "text": buffer.strip()[:3000],
                    })
                    buffer = para
                else:
                    buffer += "\n\n" + para if buffer else para
            if buffer.strip():
                chunks.append({
                    "section": current_section,
                    "page": current_page,
                    "text": buffer.strip()[:3000],
                })

        return chunks

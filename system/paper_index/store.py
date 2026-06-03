from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class PaperIndexStore:
    def __init__(self, db_path: str = None):
        repo_root = Path(__file__).resolve().parents[2]
        path = Path(db_path) if db_path else repo_root / "sessions.db"
        self.db_path = str(path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _dump_json(data: Any) -> str:
        return json.dumps(data if data is not None else {}, ensure_ascii=False)

    @staticmethod
    def _load_json(data: str) -> Any:
        if not data:
            return {}
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return {}

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS papers (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    dedupe_key TEXT DEFAULT '',
                    source_url TEXT DEFAULT '',
                    pdf_path TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'indexed',
                    summary TEXT DEFAULT '',
                    metadata_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(conn, "papers", "dedupe_key", "TEXT DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_papers_dedupe_key ON papers(dedupe_key)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_blocks (
                    id TEXT PRIMARY KEY,
                    paper_id TEXT NOT NULL,
                    page INTEGER NOT NULL,
                    section TEXT DEFAULT '',
                    block_type TEXT DEFAULT 'text',
                    text TEXT NOT NULL,
                    metadata_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(paper_id) REFERENCES papers(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS paper_blocks_paper_page_idx ON paper_blocks(paper_id, page)")
            conn.execute("CREATE INDEX IF NOT EXISTS paper_blocks_section_idx ON paper_blocks(paper_id, section)")
            conn.commit()

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {row["name"] for row in rows}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def upsert_paper(
        self,
        title: str,
        source_url: str = "",
        pdf_path: str = "",
        summary: str = "",
        metadata: Dict[str, Any] = None,
    ) -> str:
        dedupe_key = self.make_dedupe_key(title=title, source_url=source_url, pdf_path=pdf_path)
        existing = self.find_existing(dedupe_key=dedupe_key, source_url=source_url, pdf_path=pdf_path)
        now = self._now_iso()
        if existing:
            paper_id = existing["id"]
            with closing(self._connect()) as conn:
                conn.execute(
                    """
                    UPDATE papers
                    SET title = ?, dedupe_key = ?, source_url = ?, pdf_path = ?, summary = ?,
                        metadata_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (title, dedupe_key, source_url, pdf_path, summary, self._dump_json(metadata or {}), now, paper_id),
                )
                conn.commit()
            return paper_id

        paper_id = str(uuid.uuid4())
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO papers
                (id, title, dedupe_key, source_url, pdf_path, status, summary, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'indexed', ?, ?, ?, ?)
                """,
                (paper_id, title, dedupe_key, source_url, pdf_path, summary, self._dump_json(metadata or {}), now, now),
            )
            conn.commit()
        return paper_id

    def replace_blocks(self, paper_id: str, blocks: List[Dict[str, Any]]) -> None:
        now = self._now_iso()
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM paper_blocks WHERE paper_id = ?", (paper_id,))
            for block in blocks:
                conn.execute(
                    """
                    INSERT INTO paper_blocks
                    (id, paper_id, page, section, block_type, text, metadata_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        paper_id,
                        int(block.get("page") or 0),
                        block.get("section", ""),
                        block.get("block_type", "text"),
                        block.get("text", ""),
                        self._dump_json(block.get("metadata", {})),
                        now,
                    ),
                )
            conn.execute("UPDATE papers SET updated_at = ? WHERE id = ?", (now, paper_id))
            conn.commit()

    def list_papers(self, limit: int = 100) -> List[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM papers ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._paper_row(row) for row in rows]

    def get_paper(self, paper_id: str) -> Optional[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
        return self._paper_row(row) if row else None

    def find_by_pdf_path(self, pdf_path: str) -> Optional[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM papers WHERE pdf_path = ? LIMIT 1", (pdf_path,)).fetchone()
        return self._paper_row(row) if row else None

    def find_existing(self, dedupe_key: str = "", source_url: str = "", pdf_path: str = "") -> Optional[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            if dedupe_key:
                row = conn.execute("SELECT * FROM papers WHERE dedupe_key = ? LIMIT 1", (dedupe_key,)).fetchone()
                if row:
                    return self._paper_row(row)
            if source_url:
                canonical = self._canonical_url(source_url)
                rows = conn.execute("SELECT * FROM papers WHERE source_url != ''").fetchall()
                for row in rows:
                    if self._canonical_url(row["source_url"]) == canonical:
                        return self._paper_row(row)
            if pdf_path:
                row = conn.execute("SELECT * FROM papers WHERE pdf_path = ? LIMIT 1", (pdf_path,)).fetchone()
                if row:
                    return self._paper_row(row)
        return None

    def delete_paper(self, paper_id: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM paper_blocks WHERE paper_id = ?", (paper_id,))
            conn.execute("DELETE FROM papers WHERE id = ?", (paper_id,))
            conn.commit()

    def list_sections(self, paper_id: str) -> List[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT section, MIN(page) AS start_page, COUNT(*) AS blocks
                FROM paper_blocks
                WHERE paper_id = ? AND section != ''
                GROUP BY section
                ORDER BY start_page ASC
                """,
                (paper_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_blocks(self, paper_id: str, section: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            if section:
                rows = conn.execute(
                    """
                    SELECT * FROM paper_blocks
                    WHERE paper_id = ? AND section = ?
                    ORDER BY page ASC
                    LIMIT ?
                    """,
                    (paper_id, section, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM paper_blocks
                    WHERE paper_id = ?
                    ORDER BY page ASC
                    LIMIT ?
                    """,
                    (paper_id, limit),
                ).fetchall()
        return [self._block_row(row) for row in rows]

    def search_blocks(self, paper_id: str, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        terms = [term for term in re.split(r"\s+", query.strip()) if term]
        if not terms:
            return []
        like_terms = [f"%{term}%" for term in terms[:5]]
        where = " AND ".join("text LIKE ?" for _ in like_terms)
        params = [paper_id] + like_terms + [limit]
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM paper_blocks
                WHERE paper_id = ? AND {where}
                ORDER BY page ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._block_row(row) for row in rows]

    def _paper_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "dedupe_key": row["dedupe_key"] if "dedupe_key" in row.keys() else "",
            "source_url": row["source_url"],
            "pdf_path": row["pdf_path"],
            "status": row["status"],
            "summary": row["summary"],
            "metadata": self._load_json(row["metadata_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @classmethod
    def make_dedupe_key(cls, title: str, source_url: str = "", pdf_path: str = "") -> str:
        canonical = cls._canonical_url(source_url)
        if canonical:
            return f"url:{canonical}"
        if pdf_path:
            return f"pdf:{str(Path(pdf_path).resolve()).lower()}"
        normalized = re.sub(r"\s+", " ", (title or "").strip().lower())
        return f"title:{normalized}"

    @staticmethod
    def _canonical_url(url: str) -> str:
        url = (url or "").strip()
        if not url:
            return ""
        url = url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
        url = url.replace("http://", "https://")
        arxiv_match = re.search(r"arxiv\.org/(?:abs|pdf)/([^/]+?)(?:\.pdf)?$", url, re.IGNORECASE)
        if arxiv_match:
            return f"https://arxiv.org/abs/{arxiv_match.group(1)}"
        return url.lower()

    def _block_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "paper_id": row["paper_id"],
            "page": row["page"],
            "section": row["section"],
            "block_type": row["block_type"],
            "text": row["text"],
            "metadata": self._load_json(row["metadata_json"]),
            "created_at": row["created_at"],
        }

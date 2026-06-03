"""
Wiki Store — SQLite-backed structured knowledge cards.

Card types:
- ConceptPage: concept explanation with examples
- PaperPage: paper metadata and notes
- MethodPage: method description and comparison
- ComparePage: side-by-side comparison
- InterviewQA: interview question with ideal answer
- MistakeNote: captured mistake with correction
"""

import json
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from system.wiki.markdown_vault import MarkdownVault

CARD_TYPES = [
    "ConceptPage",
    "PaperPage",
    "MethodPage",
    "ComparePage",
    "InterviewQA",
    "MistakeNote",
    "StudyPlan",
    "SourceNote",
]


class WikiStore:
    def __init__(self, db_path: str = None):
        base_dir = Path(__file__).resolve().parents[2]
        path = Path(db_path) if db_path else base_dir / "sessions.db"
        self.db_path = str(path)
        self.vault = MarkdownVault()
        self._fts_enabled = False
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
        return json.dumps(data, ensure_ascii=False)

    @staticmethod
    def _load_json(data: Optional[str]) -> Any:
        if not data:
            return {}
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return {}

    def _init_db(self):
        with closing(self._connect()) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS wiki_pages (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    page_type TEXT NOT NULL,
                    markdown_path TEXT DEFAULT '',
                    dedupe_key TEXT DEFAULT '',
                    summary TEXT DEFAULT '',
                    content_json TEXT NOT NULL,
                    source_level TEXT DEFAULT '',
                    source_urls_json TEXT DEFAULT '[]',
                    related_topics_json TEXT DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            self._ensure_column(conn, "wiki_pages", "markdown_path", "TEXT DEFAULT ''")
            self._ensure_column(conn, "wiki_pages", "dedupe_key", "TEXT DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wiki_pages_dedupe_key ON wiki_pages(dedupe_key)")
            self._init_fts(conn)
            conn.commit()

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str):
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {row["name"] for row in rows}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _init_fts(self, conn: sqlite3.Connection):
        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS wiki_pages_fts
                USING fts5(title, summary, content='wiki_pages', content_rowid='rowid')
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS wiki_pages_ai
                AFTER INSERT ON wiki_pages BEGIN
                    INSERT INTO wiki_pages_fts(rowid, title, summary)
                    VALUES (new.rowid, new.title, new.summary);
                END;
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS wiki_pages_ad
                AFTER DELETE ON wiki_pages BEGIN
                    INSERT INTO wiki_pages_fts(wiki_pages_fts, rowid, title, summary)
                    VALUES ('delete', old.rowid, old.title, old.summary);
                END;
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS wiki_pages_au
                AFTER UPDATE ON wiki_pages BEGIN
                    INSERT INTO wiki_pages_fts(wiki_pages_fts, rowid, title, summary)
                    VALUES ('delete', old.rowid, old.title, old.summary);
                    INSERT INTO wiki_pages_fts(rowid, title, summary)
                    VALUES (new.rowid, new.title, new.summary);
                END;
            """)
            conn.execute("INSERT INTO wiki_pages_fts(wiki_pages_fts) VALUES ('rebuild')")
            self._fts_enabled = True
        except sqlite3.OperationalError:
            self._fts_enabled = False

    # ---- CRUD ----

    def create_card(
        self,
        title: str,
        page_type: str,
        content_json: dict,
        summary: str = "",
        source_level: str = "",
        source_urls: list = None,
        related_topics: list = None,
    ) -> str:
        source_urls = source_urls or []
        related_topics = related_topics or []
        dedupe_key = self.make_dedupe_key(title=title, page_type=page_type, source_urls=source_urls)
        existing = self.find_duplicate(title=title, page_type=page_type, source_urls=source_urls)
        if existing:
            self.update_card(
                existing["id"],
                title=title,
                page_type=page_type,
                summary=summary,
                content_json=content_json,
                source_level=source_level,
                source_urls_json=source_urls,
                related_topics_json=related_topics,
            )
            return existing["id"]

        card_id = str(uuid.uuid4())
        now = self._now_iso()
        markdown_path = self.vault.write_card(
            card_id=card_id,
            title=title,
            page_type=page_type,
            summary=summary,
            content_json=content_json,
            source_level=source_level,
            source_urls=source_urls,
            related_topics=related_topics,
        )
        with closing(self._connect()) as conn:
            conn.execute(
                """INSERT INTO wiki_pages
                   (id, title, page_type, markdown_path, dedupe_key, summary, content_json,
                    source_level, source_urls_json, related_topics_json,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    card_id,
                    title,
                    page_type,
                    markdown_path,
                    dedupe_key,
                    summary,
                    self._dump_json(content_json),
                    source_level,
                    self._dump_json(source_urls),
                    self._dump_json(related_topics),
                    now,
                    now,
                ),
            )
            conn.commit()
        return card_id

    def get_card(self, card_id: str) -> Optional[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM wiki_pages WHERE id = ?", (card_id,)
            ).fetchone()
        if not row:
            return None
        return self._row_to_dict(row)

    def update_card(self, card_id: str, **kwargs) -> None:
        current = self.get_card(card_id)
        if not current:
            return

        allowed = {"title", "page_type", "summary", "content_json",
                    "source_level", "source_urls_json", "related_topics_json", "markdown_path"}
        updates = {}
        for key, value in kwargs.items():
            if key in allowed:
                if key.endswith("_json") and isinstance(value, (list, dict)):
                    value = self._dump_json(value)
                updates[key] = value

        next_title = updates.get("title", current["title"])
        next_page_type = updates.get("page_type", current["page_type"])
        next_summary = updates.get("summary", current["summary"])
        next_content = updates.get("content_json", current["content_json"])
        if isinstance(next_content, str):
            next_content = self._load_json(next_content)
        next_source_level = updates.get("source_level", current["source_level"])
        next_source_urls = updates.get("source_urls_json", current["source_urls"])
        if isinstance(next_source_urls, str):
            next_source_urls = self._load_json(next_source_urls)
        next_related = updates.get("related_topics_json", current["related_topics"])
        if isinstance(next_related, str):
            next_related = self._load_json(next_related)
        updates["dedupe_key"] = self.make_dedupe_key(
            title=next_title,
            page_type=next_page_type,
            source_urls=next_source_urls,
        )

        markdown_path = self.vault.write_card(
            card_id=card_id,
            title=next_title,
            page_type=next_page_type,
            summary=next_summary,
            content_json=next_content,
            source_level=next_source_level,
            source_urls=next_source_urls,
            related_topics=next_related,
            existing_path=current.get("markdown_path", ""),
        )
        updates["markdown_path"] = markdown_path

        if not updates:
            return

        updates["updated_at"] = self._now_iso()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [card_id]

        with closing(self._connect()) as conn:
            conn.execute(
                f"UPDATE wiki_pages SET {set_clause} WHERE id = ?",
                values,
            )
            conn.commit()

    def delete_card(self, card_id: str) -> None:
        card = self.get_card(card_id)
        with closing(self._connect()) as conn:
            try:
                conn.execute("DELETE FROM wiki_chunks WHERE card_id = ?", (card_id,))
            except sqlite3.OperationalError:
                pass
            conn.execute("DELETE FROM wiki_pages WHERE id = ?", (card_id,))
            conn.commit()
        if card:
            self.vault.delete_card(card.get("markdown_path", ""))

    def find_duplicate(self, title: str, page_type: str, source_urls: list = None) -> Optional[Dict[str, Any]]:
        dedupe_key = self.make_dedupe_key(title=title, page_type=page_type, source_urls=source_urls or [])
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT * FROM wiki_pages
                   WHERE dedupe_key = ?
                   ORDER BY created_at ASC
                   LIMIT 1""",
                (dedupe_key,),
            ).fetchone()
            if row:
                return self._row_to_dict(row)

            # Backward compatibility for rows created before dedupe_key existed.
            canonical_urls = [self._canonical_url(url) for url in (source_urls or []) if url]
            if canonical_urls:
                rows = conn.execute(
                    "SELECT * FROM wiki_pages WHERE page_type = ? ORDER BY created_at ASC",
                    (page_type,),
                ).fetchall()
                for row in rows:
                    row_urls = self._load_json(row["source_urls_json"])
                    if any(self._canonical_url(url) in canonical_urls for url in row_urls):
                        return self._row_to_dict(row)

            row = conn.execute(
                """SELECT * FROM wiki_pages
                   WHERE page_type = ? AND lower(title) = lower(?)
                   ORDER BY created_at ASC
                   LIMIT 1""",
                (page_type, title.strip()),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_cards(
        self,
        page_type: str = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            if page_type:
                rows = conn.execute(
                    """SELECT * FROM wiki_pages WHERE page_type = ?
                       ORDER BY updated_at DESC LIMIT ? OFFSET ?""",
                    (page_type, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM wiki_pages ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def search_cards(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        query = (query or "").strip()
        if not query:
            return self.list_cards(limit=limit)

        with closing(self._connect()) as conn:
            if self._fts_enabled:
                try:
                    fts_query = " OR ".join(
                        f'"{term}"' for term in query.split() if term
                    )
                    if fts_query:
                        rows = conn.execute(
                            """SELECT p.* FROM wiki_pages_fts
                               JOIN wiki_pages AS p ON p.rowid = wiki_pages_fts.rowid
                               WHERE wiki_pages_fts MATCH ?
                               ORDER BY rank
                               LIMIT ?""",
                            (fts_query, limit),
                        ).fetchall()
                        if rows:
                            return [self._row_to_dict(row) for row in rows]
                except sqlite3.OperationalError:
                    pass

            like = f"%{query}%"
            rows = conn.execute(
                """SELECT * FROM wiki_pages
                   WHERE title LIKE ? OR summary LIKE ?
                   ORDER BY updated_at DESC LIMIT ?""",
                (like, like, limit),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_cards_by_type(self, page_type: str, limit: int = 50) -> List[Dict[str, Any]]:
        return self.list_cards(page_type=page_type, limit=limit)

    def get_recent_cards(self, limit: int = 10) -> List[Dict[str, Any]]:
        return self.list_cards(limit=limit)

    def count_by_type(self) -> Dict[str, int]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT page_type, COUNT(*) as cnt FROM wiki_pages GROUP BY page_type"
            ).fetchall()
        return {row["page_type"]: row["cnt"] for row in rows}

    @classmethod
    def make_dedupe_key(cls, title: str, page_type: str, source_urls: list = None) -> str:
        urls = [cls._canonical_url(url) for url in (source_urls or []) if url]
        urls = [url for url in urls if url]
        if urls:
            return f"url:{urls[0]}"
        return f"title:{page_type}:{cls._normalize_title(title)}"

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

    @staticmethod
    def _normalize_title(title: str) -> str:
        return re.sub(r"\s+", " ", (title or "").strip().lower())

    # ---- Helpers ----

    def _row_to_dict(self, row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "page_type": row["page_type"],
            "markdown_path": row["markdown_path"] if "markdown_path" in row.keys() else "",
            "dedupe_key": row["dedupe_key"] if "dedupe_key" in row.keys() else "",
            "summary": row["summary"],
            "content_json": self._load_json(row["content_json"]),
            "source_level": row["source_level"],
            "source_urls": self._load_json(row["source_urls_json"]),
            "related_topics": self._load_json(row["related_topics_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

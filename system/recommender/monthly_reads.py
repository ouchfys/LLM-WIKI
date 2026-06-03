"""Monthly reading recommendation queue."""

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class MonthlyReadingStore:
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
    def current_month() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

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
                CREATE TABLE IF NOT EXISTS reading_items (
                    id TEXT PRIMARY KEY,
                    source_id TEXT DEFAULT '',
                    title TEXT NOT NULL,
                    url TEXT DEFAULT '',
                    summary TEXT DEFAULT '',
                    source_type TEXT DEFAULT '',
                    source_level TEXT DEFAULT '',
                    recommend_month TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'candidate',
                    note_summary TEXT DEFAULT '',
                    takeaways_json TEXT DEFAULT '[]',
                    open_questions_json TEXT DEFAULT '[]',
                    interview_points_json TEXT DEFAULT '[]',
                    deep_read_worthy INTEGER DEFAULT 0,
                    score REAL DEFAULT 0,
                    reasons_json TEXT DEFAULT '[]',
                    metadata_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(conn, "reading_items", "note_summary", "TEXT DEFAULT ''")
            self._ensure_column(conn, "reading_items", "takeaways_json", "TEXT DEFAULT '[]'")
            self._ensure_column(conn, "reading_items", "open_questions_json", "TEXT DEFAULT '[]'")
            self._ensure_column(conn, "reading_items", "interview_points_json", "TEXT DEFAULT '[]'")
            self._ensure_column(conn, "reading_items", "deep_read_worthy", "INTEGER DEFAULT 0")
            conn.commit()

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {row["name"] for row in rows}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def add_source_item(
        self,
        item: Any,
        month: str = "",
        score: float = 0,
        reasons: List[str] = None,
        status: str = "candidate",
    ) -> str:
        month = month or self.current_month()
        source_id = self._get(item, "id", "") or self._get(item, "url", "")
        title = self._get(item, "title", "Untitled")
        url = self._get(item, "url", "") or ""
        now = self._now_iso()
        existing = self.find_existing(source_id=source_id, url=url, month=month)

        payload = {
            "authors": self._get(item, "authors", []),
            "year": self._get(item, "year", None),
            "venue": self._get(item, "venue", ""),
            "citation_count": self._get(item, "citation_count", None),
            "raw_metadata": self._get(item, "raw_metadata", {}),
        }

        if existing:
            with closing(self._connect()) as conn:
                conn.execute(
                    """
                    UPDATE reading_items
                    SET title = ?, url = ?, summary = ?, source_type = ?,
                        source_level = ?, score = ?, reasons_json = ?,
                        metadata_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        title,
                        url,
                        self._get(item, "summary", "") or "",
                        self._get(item, "source_type", "") or "",
                        self._get(item, "source_level", "") or "",
                        score,
                        self._dump_json(reasons or []),
                        self._dump_json(payload),
                        now,
                        existing["id"],
                    ),
                )
                conn.commit()
            return existing["id"]

        item_id = str(uuid.uuid4())
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO reading_items
                (id, source_id, title, url, summary, source_type, source_level,
                 recommend_month, status, score, reasons_json, metadata_json,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    source_id,
                    title,
                    url,
                    self._get(item, "summary", "") or "",
                    self._get(item, "source_type", "") or "",
                    self._get(item, "source_level", "") or "",
                    month,
                    status,
                    score,
                    self._dump_json(reasons or []),
                    self._dump_json(payload),
                    now,
                    now,
                ),
            )
            conn.commit()
        return item_id

    def find_existing(self, source_id: str = "", url: str = "", month: str = "") -> Optional[Dict[str, Any]]:
        month = month or self.current_month()
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM reading_items
                WHERE recommend_month = ?
                  AND ((source_id != '' AND source_id = ?) OR (url != '' AND url = ?))
                LIMIT 1
                """,
                (month, source_id or "", url or ""),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def list_items(
        self,
        month: str = "",
        status: str = "",
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        month = month or self.current_month()
        with closing(self._connect()) as conn:
            if status and status != "all":
                rows = conn.execute(
                    """
                    SELECT * FROM reading_items
                    WHERE recommend_month = ? AND status = ?
                    ORDER BY score DESC, updated_at DESC
                    LIMIT ?
                    """,
                    (month, status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM reading_items
                    WHERE recommend_month = ?
                    ORDER BY score DESC, updated_at DESC
                    LIMIT ?
                    """,
                    (month, limit),
                ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def get_item(self, item_id: str) -> Optional[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM reading_items WHERE id = ?", (item_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def update_status(self, item_id: str, status: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE reading_items
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, self._now_iso(), item_id),
            )
            conn.commit()

    def update_notes(
        self,
        item_id: str,
        note_summary: str = "",
        takeaways: List[str] = None,
        open_questions: List[str] = None,
        interview_points: List[str] = None,
        deep_read_worthy: bool = False,
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE reading_items
                SET note_summary = ?,
                    takeaways_json = ?,
                    open_questions_json = ?,
                    interview_points_json = ?,
                    deep_read_worthy = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    note_summary,
                    self._dump_json(takeaways or []),
                    self._dump_json(open_questions or []),
                    self._dump_json(interview_points or []),
                    1 if deep_read_worthy else 0,
                    self._now_iso(),
                    item_id,
                ),
            )
            conn.commit()

    def count_by_status(self, month: str = "") -> Dict[str, int]:
        month = month or self.current_month()
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM reading_items
                WHERE recommend_month = ?
                GROUP BY status
                """,
                (month,),
            ).fetchall()
        return {row["status"]: row["count"] for row in rows}

    def _row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "source_id": row["source_id"],
            "title": row["title"],
            "url": row["url"],
            "summary": row["summary"],
            "source_type": row["source_type"],
            "source_level": row["source_level"],
            "recommend_month": row["recommend_month"],
            "status": row["status"],
            "note_summary": row["note_summary"],
            "takeaways": self._load_json(row["takeaways_json"]) or [],
            "open_questions": self._load_json(row["open_questions_json"]) or [],
            "interview_points": self._load_json(row["interview_points_json"]) or [],
            "deep_read_worthy": bool(row["deep_read_worthy"]),
            "score": row["score"],
            "reasons": self._load_json(row["reasons_json"]) or [],
            "metadata": self._load_json(row["metadata_json"]) or {},
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _get(item: Any, key: str, default: Any = None) -> Any:
        if isinstance(item, dict):
            return item.get(key, default)
        return getattr(item, key, default)

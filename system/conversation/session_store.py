import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import re


from system.memory.user_memory import UserMemoryStore


class SessionStore(UserMemoryStore):
    def __init__(self, db_path: str = None):
        base_dir = Path(__file__).resolve().parents[2]
        path = Path(db_path) if db_path else base_dir / "sessions.db"
        self.db_path = str(path)
        self._preference_fts_enabled = False
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
        if data is None:
            data = {}
        return json.dumps(data, ensure_ascii=False)

    @staticmethod
    def _load_json(data: Optional[str]) -> Dict[str, Any]:
        if not data:
            return {}
        try:
            loaded = json.loads(data)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    created_at TEXT,
                    settings_json TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    role TEXT,
                    content TEXT,
                    metadata_json TEXT,
                    created_at TEXT
                )
                """
            )
            # ---- 用户偏好表：稳定偏好，键值型，按 key upsert ----
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_profile (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    evidence TEXT DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(user_profile)")}
            if "source_session_id" not in columns:
                conn.execute("ALTER TABLE user_profile ADD COLUMN source_session_id TEXT DEFAULT ''")
            # ---- 情节记忆表：已讲过的论文/话题，带过期时间 ----
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS preference_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic TEXT NOT NULL,
                    detail TEXT DEFAULT '',
                    paper TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    expires_at TEXT
                )
                """
            )
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS context_checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    previous_cutoff INTEGER NOT NULL,
                    through_message_id INTEGER NOT NULL,
                    summary TEXT NOT NULL,
                    stats_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS checkpoints_session ON context_checkpoints(session_id, id);
                CREATE TABLE IF NOT EXISTS session_tool_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tool_results_session ON session_tool_results(session_id, id);
                CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, id);
            """)
            self._init_preference_fts(conn)
            conn.commit()

    def _init_preference_fts(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS preference_memory_fts
                USING fts5(topic, detail, paper, content='preference_memory', content_rowid='id')
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS preference_memory_ai
                AFTER INSERT ON preference_memory BEGIN
                    INSERT INTO preference_memory_fts(rowid, topic, detail, paper)
                    VALUES (new.id, new.topic, new.detail, new.paper);
                END;
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS preference_memory_ad
                AFTER DELETE ON preference_memory BEGIN
                    INSERT INTO preference_memory_fts(preference_memory_fts, rowid, topic, detail, paper)
                    VALUES ('delete', old.id, old.topic, old.detail, old.paper);
                END;
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS preference_memory_au
                AFTER UPDATE ON preference_memory BEGIN
                    INSERT INTO preference_memory_fts(preference_memory_fts, rowid, topic, detail, paper)
                    VALUES ('delete', old.id, old.topic, old.detail, old.paper);
                    INSERT INTO preference_memory_fts(rowid, topic, detail, paper)
                    VALUES (new.id, new.topic, new.detail, new.paper);
                END;
                """
            )
            conn.execute(
                "INSERT INTO preference_memory_fts(preference_memory_fts) VALUES ('rebuild')"
            )
            self._preference_fts_enabled = True
        except sqlite3.OperationalError:
            self._preference_fts_enabled = False

    # ===========================================================
    #  会话管理（原有方法，保持不变）
    # ===========================================================

    def create_session(self, title: str = "新会话", settings: dict = None) -> str:
        session_id = str(uuid.uuid4())
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO sessions (id, title, created_at, settings_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    session_id,
                    title,
                    self._now_iso(),
                    self._dump_json(settings or {}),
                ),
            )
            conn.commit()
        return session_id

    def ensure_session(self, session_id: str, title: str = "新会话", settings: dict = None) -> str:
        session_id = (session_id or "").strip()
        if not session_id:
            return self.create_session(title=title, settings=settings)

        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT id FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                conn.execute(
                    """
                    INSERT INTO sessions (id, title, created_at, settings_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        title,
                        self._now_iso(),
                        self._dump_json(settings or {}),
                    ),
                )
                conn.commit()
        return session_id

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT id, title, created_at, settings_json FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "settings": self._load_json(row["settings_json"]),
        }

    def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: dict = None,
    ) -> int:
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                INSERT INTO messages (session_id, role, content, metadata_json, created_at)
                SELECT ?, ?, ?, ?, ?
                WHERE EXISTS (SELECT 1 FROM sessions WHERE id = ?)
                """,
                (
                    session_id,
                    role,
                    content,
                    self._dump_json(metadata or {}),
                    self._now_iso(),
                    session_id,
                ),
            )
            conn.commit()
            # A late model response must not recreate messages after deletion.
            return int(cursor.lastrowid) if cursor.rowcount else 0

    def get_messages(self, session_id: str, last_n: int = 80) -> List[Dict[str, Any]]:
        """Return raw persisted messages for explicit conversation commands.

        Display history remains lossless.  Callers such as ``/wiki`` and
        ``/compact`` use these stable database ids as provenance instead of
        relying on transient ids generated by the browser.
        """
        bounded = max(1, min(int(last_n), 500))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT id, role, content, metadata_json, created_at
                FROM messages
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, bounded),
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "role": str(row["role"] or ""),
                "content": str(row["content"] or ""),
                "metadata": self._load_json(row["metadata_json"]),
                "created_at": str(row["created_at"] or ""),
            }
            for row in reversed(rows)
        ]

    def get_context_messages(self, session_id: str) -> list:
        """All uncompacted records in order; unlike display pagination, no 500-row cap."""
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT settings_json FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if not row:
                return []
            cutoff = int(self._load_json(row["settings_json"]).get("compacted_through_message_id") or 0)
            rows = conn.execute(
                "SELECT id, role, content, metadata_json FROM messages WHERE session_id = ? AND id > ? ORDER BY id",
                (session_id, cutoff),
            ).fetchall()
        return [dict(id=int(row["id"]), role=row["role"], content=row["content"] or "",
                     metadata=self._load_json(row["metadata_json"])) for row in rows
                if not self._load_json(row["metadata_json"]).get("cancelled")
                and self._load_json(row["metadata_json"]).get("command") != "compact"]

    def commit_context_summary(self, session_id: str, summary: str, cutoff: int,
                               expected_cutoff: int, expected_summary: str, stats=None) -> bool:
        """Compare-and-swap prevents stale/manual compactions and deleted-session resurrection."""
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT settings_json FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if not row:
                return False
            settings = self._load_json(row["settings_json"])
            if (int(settings.get("compacted_through_message_id") or 0) != expected_cutoff
                    or str(settings.get("context_summary") or "").strip() != expected_summary.strip()):
                return False
            if cutoff <= expected_cutoff or not summary.strip():
                return False
            if not conn.execute("SELECT 1 FROM messages WHERE session_id = ? AND id = ?", (session_id, cutoff)).fetchone():
                return False
            conn.execute(
                "INSERT INTO context_checkpoints(session_id, previous_cutoff, through_message_id, summary, stats_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, expected_cutoff, cutoff, summary, self._dump_json(stats), self._now_iso()),
            )
            settings.update(context_summary=summary, compacted_through_message_id=cutoff,
                            context_summary_updated_at=self._now_iso())
            conn.execute("UPDATE sessions SET settings_json = ? WHERE id = ?", (self._dump_json(settings), session_id))
            conn.commit()
            return True

    def get_context_checkpoints(self, session_id, limit=20):
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM context_checkpoints WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                                (session_id, max(1, min(int(limit), 100)))).fetchall()
        return [dict(row) for row in rows]

    def search_session_history(self, session_id, query, limit=5):
        """Search raw current-session messages, including compacted records. Literal substring, supports Chinese."""
        query = str(query or "").strip()
        if not query:
            return []
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, role, content FROM messages WHERE session_id = ? AND instr(lower(content), lower(?)) > 0 ORDER BY id DESC LIMIT ?",
                (session_id, query, max(1, min(int(limit), 8))),
            ).fetchall()
        result = []
        for row in rows:
            content = row["content"] or ""
            offset = max(0, content.lower().find(query.lower()) - 120)
            result.append({"message_id": row["id"], "role": row["role"], "offset": offset,
                           "excerpt": content[offset:offset + 1000], "total_chars": len(content)})
        return result

    def read_session_messages(self, session_id, start_id, end_id, offset=0, limit=8):
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, role, content FROM messages WHERE session_id = ? AND id BETWEEN ? AND ? ORDER BY id LIMIT ?",
                (session_id, int(start_id), int(end_id), max(1, min(int(limit), 8))),
            ).fetchall()
        offset = max(0, int(offset))
        return [{"message_id": row["id"], "role": row["role"], "content": (row["content"] or "")[offset:offset + 4000],
                 "offset": offset, "next_offset": offset + 4000 if len(row["content"] or "") > offset + 4000 else None}
                for row in rows]

    def save_tool_result(self, session_id, tool, arguments, result):
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "INSERT INTO session_tool_results(session_id, tool, arguments_json, result_json, created_at) "
                "SELECT ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM sessions WHERE id = ?)",
                (session_id, tool, self._dump_json(arguments), self._dump_json(result), self._now_iso(), session_id))
            conn.commit()
            return cursor.lastrowid if cursor.rowcount else None

    def read_tool_result(self, session_id, result_id, offset=0):
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT tool, result_json FROM session_tool_results WHERE session_id = ? AND id = ?",
                               (session_id, int(result_id))).fetchone()
        if not row:
            return []
        offset = max(0, int(offset))
        content = row["result_json"]
        return [{"result_id": int(result_id), "tool": row["tool"], "content": content[offset:offset + 4000],
                 "offset": offset, "next_offset": offset + 4000 if len(content) > offset + 4000 else None}]

    def get_history(self, session_id: str, last_n: Optional[int] = 5) -> list:
        with closing(self._connect()) as conn:
            session_row = conn.execute(
                "SELECT settings_json FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            settings = self._load_json(session_row["settings_json"]) if session_row else {}
            compacted_through = int(settings.get("compacted_through_message_id") or 0)
            context_summary = str(settings.get("context_summary") or "").strip()
            rows = conn.execute(
                """
                SELECT role, content, metadata_json
                FROM messages
                WHERE session_id = ? AND id > ?
                ORDER BY id ASC
                """,
                (session_id, compacted_through),
            ).fetchall()

        pairs: List[tuple] = []
        pending_user: Optional[str] = None
        for row in rows:
            role = row["role"]
            content = row["content"]
            metadata = self._load_json(row["metadata_json"])
            # A compaction acknowledgement is part of the visible transcript,
            # not fresh conversational context for the model.
            if metadata.get("command") == "compact" or metadata.get("cancelled"):
                continue
            if role == "user":
                if pending_user is not None:
                    pairs.append((pending_user, ""))
                pending_user = content
            elif role == "assistant" and pending_user is not None:
                pairs.append((pending_user, content))
                pending_user = None

        if pending_user is not None:
            pairs.append((pending_user, ""))

        if last_n is not None and last_n <= 0:
            return []
        recent = pairs if last_n is None else pairs[-last_n:]
        if context_summary:
            return [("[SYSTEM_CONTEXT_SUMMARY]", context_summary), *recent]
        return recent

    def get_display_history(self, session_id: str, last_n: int = 20) -> list:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT role, content, metadata_json
                FROM messages
                WHERE session_id = ?
                ORDER BY id ASC
                """,
                (session_id,),
            ).fetchall()

        items: List[tuple] = []
        pending_user: Optional[str] = None
        pending_metadata: Dict[str, Any] = {}

        for row in rows:
            role = row["role"]
            content = row["content"]
            metadata = self._load_json(row["metadata_json"])
            if role == "user":
                if pending_user is not None:
                    items.append((pending_user, "", {}))
                pending_user = content
                pending_metadata = {}
            elif role == "assistant" and pending_user is not None:
                items.append((pending_user, content, metadata))
                pending_user = None
                pending_metadata = {}

        if pending_user is not None:
            items.append((pending_user, "", pending_metadata))

        if last_n <= 0:
            return []
        return items[-last_n:]

    def list_sessions(self) -> list:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT id, title, created_at
                FROM sessions
                ORDER BY created_at DESC, id DESC
                """
            ).fetchall()

        return [
            {
                "id": row["id"],
                "title": row["title"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def update_session_title(self, session_id: str, title: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE sessions SET title = ? WHERE id = ?",
                (title, session_id),
            )
            conn.commit()

    def update_session_settings(self, session_id: str, settings: dict) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE sessions SET settings_json = ? WHERE id = ?",
                (self._dump_json(settings or {}), session_id),
            )
            conn.commit()

    def get_session_settings(self, session_id: str) -> dict:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT settings_json FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()

        if not row:
            return {}
        return self._load_json(row["settings_json"])

    def clear_session(self, session_id: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM context_checkpoints WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM session_tool_results WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute(
                """
                UPDATE sessions
                SET title = ?, settings_json = ?
                WHERE id = ?
                """,
                ("新会话", self._dump_json({}), session_id),
            )
            conn.commit()

    def delete_session(self, session_id: str) -> bool:
        """Delete one chat session and its messages. Long-term memory is untouched."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT id FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if not row:
                return False
            conn.execute("DELETE FROM context_checkpoints WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM session_tool_results WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()
        return True

    def delete_all_sessions(self) -> int:
        """Delete all chat sessions and messages. Long-term memory is untouched."""
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM sessions").fetchone()
            count = int(row["count"] if row else 0)
            conn.execute("DELETE FROM context_checkpoints")
            conn.execute("DELETE FROM session_tool_results")
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM sessions")
            conn.commit()
        return count


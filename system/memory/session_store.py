import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import re


class SessionStore:
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
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO messages (session_id, role, content, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    role,
                    content,
                    self._dump_json(metadata or {}),
                    self._now_iso(),
                ),
            )
            conn.commit()

    def get_history(self, session_id: str, last_n: int = 5) -> list:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT role, content
                FROM messages
                WHERE session_id = ?
                ORDER BY id ASC
                """,
                (session_id,),
            ).fetchall()

        pairs: List[tuple] = []
        pending_user: Optional[str] = None
        for row in rows:
            role = row["role"]
            content = row["content"]
            if role == "user":
                if pending_user is not None:
                    pairs.append((pending_user, ""))
                pending_user = content
            elif role == "assistant" and pending_user is not None:
                pairs.append((pending_user, content))
                pending_user = None

        if pending_user is not None:
            pairs.append((pending_user, ""))

        if last_n <= 0:
            return []
        return pairs[-last_n:]

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
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()
        return True

    def delete_all_sessions(self) -> int:
        """Delete all chat sessions and messages. Long-term memory is untouched."""
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM sessions").fetchone()
            count = int(row["count"] if row else 0)
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM sessions")
            conn.commit()
        return count

    # ===========================================================
    #  用户偏好管理（user_profile 表）
    # ===========================================================

    def upsert_preference(self, key: str, value: str, evidence: str = "") -> None:
        """按 key 写入或覆盖一条稳定偏好。"""
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO user_profile (key, value, evidence, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    evidence = excluded.evidence,
                    updated_at = excluded.updated_at
                """,
                (key, value, evidence, self._now_iso()),
            )
            conn.commit()
        print(f"[SessionStore] Upsert preference: {key} = {value}")

    def get_preference(self, key: str) -> Optional[str]:
        """按 key 查询单条偏好值，不存在返回 None。"""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT value FROM user_profile WHERE key = ?",
                (key,),
            ).fetchone()
        return row["value"] if row else None

    def get_all_preferences(self) -> Dict[str, str]:
        """返回所有偏好，格式 {key: value}。"""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT key, value FROM user_profile ORDER BY updated_at DESC"
            ).fetchall()
        return {row["key"]: row["value"] for row in rows}

    def get_all_preferences_detailed(self) -> List[Dict[str, str]]:
        """返回所有偏好的详细信息，含证据和更新时间。"""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT key, value, evidence, updated_at FROM user_profile ORDER BY updated_at DESC"
            ).fetchall()
        return [
            {
                "key": row["key"],
                "value": row["value"],
                "evidence": row["evidence"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def delete_preference(self, key: str) -> None:
        """按 key 删除一条偏好。"""
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM user_profile WHERE key = ?", (key,))
            conn.commit()
        print(f"[SessionStore] Deleted preference: {key}")

    def clear_all_preferences(self) -> None:
        """清空所有偏好。"""
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM user_profile")
            conn.commit()
        print("[SessionStore] Cleared all preferences")

    # ===========================================================
    #  情节记忆管理（preference_memory 表）
    # ===========================================================

    def add_episode(
        self,
        topic: str,
        detail: str = "",
        paper: str = "",
        ttl_days: int = 30,
    ) -> None:
        """
        写入一条情节记忆。

        Args:
            topic: 话题简述
            detail: 具体讲了什么
            paper: 相关论文名
            ttl_days: 过期天数，默认 30 天
        """
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(days=ttl_days)).isoformat(timespec="seconds")

        with closing(self._connect()) as conn:
            # 去重：如果同一个 topic 已存在，更新它而不是重复插入
            existing = conn.execute(
                "SELECT id FROM preference_memory WHERE topic = ?",
                (topic,),
            ).fetchone()

            if existing:
                conn.execute(
                    """
                    UPDATE preference_memory
                    SET detail = ?, paper = ?, created_at = ?, expires_at = ?
                    WHERE id = ?
                    """,
                    (detail, paper, now.isoformat(timespec="seconds"), expires_at, existing["id"]),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO preference_memory (topic, detail, paper, created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (topic, detail, paper, now.isoformat(timespec="seconds"), expires_at),
                )
            conn.commit()
        print(f"[SessionStore] Added episode: {topic}")

    def get_recent_episodes(self, limit: int = 5) -> List[Dict[str, str]]:
        """
        查询最近的未过期情节记忆。

        自动清理过期记录后返回。
        """
        now_iso = self._now_iso()
        with closing(self._connect()) as conn:
            # 先清理过期记录
            conn.execute(
                "DELETE FROM preference_memory WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now_iso,),
            )
            conn.commit()

            rows = conn.execute(
                """
                SELECT topic, detail, paper, created_at
                FROM preference_memory
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [
            {
                "topic": row["topic"],
                "detail": row["detail"],
                "paper": row["paper"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def search_episodes(self, keyword: str, limit: int = 5) -> List[Dict[str, str]]:
        """按问题语义/关键词检索情节记忆。"""
        now_iso = self._now_iso()
        keyword = (keyword or "").strip()
        with closing(self._connect()) as conn:
            conn.execute(
                "DELETE FROM preference_memory WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now_iso,),
            )
            conn.commit()

            if keyword:
                rows = self._search_episodes_with_fts(conn, keyword, now_iso, limit)
                if rows:
                    return rows

            rows = conn.execute(
                """
                SELECT topic, detail, paper, created_at
                FROM preference_memory
                WHERE expires_at IS NULL OR expires_at >= ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (now_iso, limit),
            ).fetchall()

        return [
            {
                "topic": row["topic"],
                "detail": row["detail"],
                "paper": row["paper"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _search_episodes_with_fts(
        self,
        conn: sqlite3.Connection,
        keyword: str,
        now_iso: str,
        limit: int,
    ) -> List[sqlite3.Row]:
        terms = self._query_terms(keyword)
        if not terms:
            return []

        if self._preference_fts_enabled:
            try:
                fts_query = " OR ".join(self._escape_fts_term(term) for term in terms[:8])
                rows = conn.execute(
                    """
                    SELECT p.topic, p.detail, p.paper, p.created_at, bm25(preference_memory_fts) AS rank
                    FROM preference_memory_fts
                    JOIN preference_memory AS p ON p.id = preference_memory_fts.rowid
                    WHERE preference_memory_fts MATCH ?
                      AND (p.expires_at IS NULL OR p.expires_at >= ?)
                    ORDER BY rank ASC, p.created_at DESC
                    LIMIT ?
                    """,
                    (fts_query, now_iso, limit),
                ).fetchall()
                if rows:
                    return rows
            except sqlite3.OperationalError:
                pass

        rows = conn.execute(
            """
            SELECT topic, detail, paper, created_at
            FROM preference_memory
            WHERE expires_at IS NULL OR expires_at >= ?
            """,
            (now_iso,),
        ).fetchall()

        scored = []
        for row in rows:
            score = self._score_episode(keyword, terms, row["topic"], row["detail"], row["paper"], row["created_at"])
            if score > 0:
                scored.append((score, row))

        scored.sort(key=lambda item: (item[0], item[1]["created_at"]), reverse=True)
        return [row for _, row in scored[:limit]]

    @staticmethod
    def _query_terms(keyword: str) -> List[str]:
        text = (keyword or "").lower()
        terms = set()
        for token in re.findall(r"[a-z0-9_]{2,}", text):
            terms.add(token)
        for block in re.findall(r"[\u4e00-\u9fff]{2,}", keyword or ""):
            terms.add(block)
            for i in range(len(block) - 1):
                terms.add(block[i:i+2])
        return [term for term in terms if term.strip()]

    @staticmethod
    def _escape_fts_term(term: str) -> str:
        term = term.replace('"', '""')
        if " " in term or "-" in term:
            return f'"{term}"'
        return term

    @staticmethod
    def _score_episode(query: str, terms: List[str], topic: str, detail: str, paper: str, created_at: str) -> float:
        text_topic = (topic or "").lower()
        text_detail = (detail or "").lower()
        text_paper = (paper or "").lower()
        score = 0.0

        query_l = (query or "").lower()
        if query_l and query_l in text_topic:
            score += 5.0
        if query_l and query_l in text_detail:
            score += 3.0

        for term in terms:
            if term in text_topic:
                score += 3.0
            if term in text_detail:
                score += 2.0
            if term in text_paper:
                score += 1.0

        try:
            created = datetime.fromisoformat(created_at)
            age_days = max((datetime.now(timezone.utc) - created).days, 0)
            score += 1.0 / (1.0 + age_days)
        except Exception:
            pass

        return score

    def clear_all_episodes(self) -> None:
        """清空所有情节记忆。"""
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM preference_memory")
            conn.commit()
        print("[SessionStore] Cleared all episodes")

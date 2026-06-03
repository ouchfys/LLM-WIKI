"""
Learning Profile — tracks goals, weak points, mastery, and review state.

Wraps SessionStore's user_profile table for goals, and manages a new
learning_events table for scored learning activities.
"""

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class LearningProfileStore:
    """Tracks learning events, weak points, mastered topics, and review schedules."""

    def __init__(self, session_store, db_path: str = None):
        base_dir = Path(__file__).resolve().parents[2]
        path = Path(db_path) if db_path else base_dir / "sessions.db"
        self.db_path = str(path)
        self.store = session_store
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
        return json.dumps(data, ensure_ascii=False) if data is not None else "{}"

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
                CREATE TABLE IF NOT EXISTS learning_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    detail TEXT DEFAULT '',
                    score REAL,
                    metadata_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_profile_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_type TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    weight REAL DEFAULT 1.0,
                    evidence TEXT DEFAULT '',
                    source TEXT DEFAULT '',
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS recommendation_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id TEXT NOT NULL,
                    item_type TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT DEFAULT '',
                    metadata_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
            """)
            conn.commit()

    # ---- Events ----

    def log_event(
        self,
        event_type: str,
        topic: str,
        detail: str = "",
        score: Optional[float] = None,
        metadata: dict = None,
    ) -> int:
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """INSERT INTO learning_events
                   (event_type, topic, detail, score, metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    event_type,
                    topic,
                    detail,
                    score,
                    self._dump_json(metadata or {}),
                    self._now_iso(),
                ),
            )
            conn.commit()
            return cursor.lastrowid

    def get_recent_events(
        self,
        event_type: str = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            if event_type:
                rows = conn.execute(
                    """SELECT * FROM learning_events WHERE event_type = ?
                       ORDER BY created_at DESC LIMIT ?""",
                    (event_type, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM learning_events ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    # ---- Weak points & Mastery ----

    def get_weak_points(self, min_events: int = 2, score_threshold: float = 6.0) -> List[Dict[str, Any]]:
        """Topics with consistently low interview scores."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT topic, AVG(score) as avg_score, COUNT(*) as event_count,
                          MAX(created_at) as last_seen
                   FROM learning_events
                   WHERE event_type = 'interview_answer' AND score IS NOT NULL
                   GROUP BY topic
                   HAVING COUNT(*) >= ? AND AVG(score) < ?
                   ORDER BY avg_score ASC""",
                (min_events, score_threshold),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_mastered_topics(self, min_events: int = 2, score_threshold: float = 7.5) -> List[Dict[str, Any]]:
        """Topics with consistently high interview scores."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT topic, AVG(score) as avg_score, COUNT(*) as event_count,
                          MAX(created_at) as last_seen
                   FROM learning_events
                   WHERE event_type = 'interview_answer' AND score IS NOT NULL
                   GROUP BY topic
                   HAVING COUNT(*) >= ? AND AVG(score) >= ?
                   ORDER BY avg_score DESC""",
                (min_events, score_threshold),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- Review scheduling ----

    def get_due_review_topics(self, review_interval_days: int = 7) -> List[Dict[str, Any]]:
        """Topics that need review based on spaced repetition (not reviewed in N days)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=review_interval_days)).isoformat(timespec="seconds")
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT topic, MAX(created_at) as last_reviewed, COUNT(*) as review_count
                   FROM learning_events
                   WHERE event_type = 'reviewed'
                   GROUP BY topic
                   HAVING MAX(created_at) < ?
                   ORDER BY last_reviewed ASC""",
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_reviewed(self, topic: str):
        self.log_event("reviewed", topic, detail="Spaced repetition review")

    # ---- Profile signals for recommendation ----

    def upsert_signal(
        self,
        signal_type: str,
        key: str,
        value: str,
        weight: float = 1.0,
        evidence: str = "",
        source: str = "",
    ) -> None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT id, weight FROM user_profile_signals
                   WHERE signal_type = ? AND key = ? AND value = ?
                   ORDER BY updated_at DESC LIMIT 1""",
                (signal_type, key, value),
            ).fetchone()
            if row:
                conn.execute(
                    """UPDATE user_profile_signals
                       SET weight = ?, evidence = ?, source = ?, updated_at = ?
                       WHERE id = ?""",
                    (weight, evidence, source, self._now_iso(), row["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO user_profile_signals
                       (signal_type, key, value, weight, evidence, source, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (signal_type, key, value, weight, evidence, source, self._now_iso()),
                )
            conn.commit()

    def get_profile_signals(self, signal_type: str = None, limit: int = 100) -> List[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            if signal_type:
                rows = conn.execute(
                    """SELECT * FROM user_profile_signals WHERE signal_type = ?
                       ORDER BY weight DESC, updated_at DESC LIMIT ?""",
                    (signal_type, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM user_profile_signals
                       ORDER BY weight DESC, updated_at DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
        result = []
        for row in rows:
            item = self._sanitize_signal(dict(row))
            if item:
                result.append(item)
        return result[:limit]

    def log_recommendation_feedback(
        self,
        item_id: str,
        item_type: str,
        action: str,
        reason: str = "",
        metadata: dict = None,
    ) -> int:
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """INSERT INTO recommendation_feedback
                   (item_id, item_type, action, reason, metadata_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    item_id,
                    item_type,
                    action,
                    reason,
                    self._dump_json(metadata or {}),
                    self._now_iso(),
                ),
            )
            conn.commit()
            return cursor.lastrowid

    def get_recommendation_feedback(self, limit: int = 200) -> List[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT * FROM recommendation_feedback
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = self._load_json(item.pop("metadata_json"))
            result.append(item)
        return result

    @staticmethod
    def _sanitize_signal(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        value = str(row.get("value", "")).strip()
        signal_type = str(row.get("signal_type", "")).strip()
        if not value:
            return None
        if signal_type not in {"interest", "weak_point"}:
            return row

        stopwords = {
            "wiki", "copilot", "基于我的", "帮我总结", "你是谁", "这个", "那个", "总结",
            "问题", "内容", "资料", "笔记", "面试表达", "我对", "不太熟", "还不太熟", "尤其是", "这块",
        }
        if value.lower() in stopwords or value in stopwords:
            return None
        if len(value) <= 1 or len(value) > 32:
            return None
        return row

    # ---- Goals (delegated to SessionStore) ----

    def set_goal(self, goal: str):
        self.store.upsert_preference("learning_goal", goal)

    def get_goals(self) -> List[str]:
        goal = self.store.get_preference("learning_goal")
        if not goal:
            return []
        return [g.strip() for g in goal.split("\n") if g.strip()]

    # ---- Stats ----

    def get_interview_stats(self) -> Dict[str, Any]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT COUNT(*) as total, AVG(score) as avg_score,
                          MAX(score) as best_score, MIN(score) as worst_score
                   FROM learning_events
                   WHERE event_type = 'interview_answer' AND score IS NOT NULL"""
            ).fetchone()
        return dict(row) if row else {"total": 0, "avg_score": 0, "best_score": 0, "worst_score": 0}

    # ----

    def _row_to_dict(self, row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "event_type": row["event_type"],
            "topic": row["topic"],
            "detail": row["detail"],
            "score": row["score"],
            "metadata": self._load_json(row["metadata_json"]),
            "created_at": row["created_at"],
        }

"""User preferences and expiring conversation topics. Wiki documents are not memory."""
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional


class UserMemoryStore:
    # ===========================================================
    #  用户偏好管理（user_profile 表）
    # ===========================================================

    def upsert_preference(self, key: str, value: str, evidence: str = "", *, source_session_id: str = "",
                          evidence_message_id: int = 0, confidence: float = 1.0) -> None:
        """按 key 写入或覆盖一条稳定偏好。"""
        with closing(self._connect()) as conn:
            previous = conn.execute("SELECT value FROM user_profile WHERE key = ?", (key,)).fetchone()
            conn.execute(
                """
                INSERT INTO user_profile
                    (key, value, evidence, updated_at, source_session_id, evidence_message_id, confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    evidence = excluded.evidence,
                    updated_at = excluded.updated_at,
                    source_session_id = excluded.source_session_id,
                    evidence_message_id = excluded.evidence_message_id,
                    confidence = excluded.confidence
                """,
                (key, value, evidence, self._now_iso(), source_session_id,
                 int(evidence_message_id or 0), max(0.0, min(float(confidence), 1.0))),
            )
            if not previous or str(previous["value"]) != str(value):
                conn.execute(
                    """INSERT INTO user_profile_history
                       (key, previous_value, new_value, evidence, source_session_id,
                        evidence_message_id, confidence, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (key, str(previous["value"] if previous else ""), value, evidence, source_session_id,
                     int(evidence_message_id or 0), max(0.0, min(float(confidence), 1.0)), self._now_iso()),
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
                """SELECT key, value, evidence, updated_at, source_session_id,
                          evidence_message_id, confidence
                   FROM user_profile ORDER BY updated_at DESC"""
            ).fetchall()
        return [
            {
                "key": row["key"],
                "value": row["value"],
                "evidence": row["evidence"],
                "source_session_id": row["source_session_id"],
                "evidence_message_id": int(row["evidence_message_id"] or 0),
                "confidence": float(row["confidence"] if row["confidence"] is not None else 1.0),
                "scope": "user",
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

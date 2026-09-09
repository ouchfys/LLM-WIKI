"""Project-scoped working state and cross-session memory for the local assistant."""

from __future__ import annotations

import json
import re
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional


DEFAULT_PROJECT_ID = "paperwiki-default"
DEFAULT_PROJECT_NAME = "PaperWiki 研究项目"


class ProjectMemoryStore:
    """SQLite-backed project scope shared by every conversation in a workspace."""

    _PROJECT_MEMORY_TYPES = {
        "goal", "constraint", "decision", "open_question", "milestone", "topic",
    }

    def _init_project_memory_schema(self, conn) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                purpose TEXT NOT NULL DEFAULT '',
                purpose_evidence TEXT NOT NULL DEFAULT '',
                purpose_source_session_id TEXT NOT NULL DEFAULT '',
                purpose_updated_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session_state (
                session_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                state_json TEXT NOT NULL DEFAULT '{}',
                updated_through_message_id INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS project_state (
                project_id TEXT PRIMARY KEY,
                state_json TEXT NOT NULL DEFAULT '{}',
                updated_through_message_id INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS project_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                content TEXT NOT NULL,
                source_session_id TEXT NOT NULL DEFAULT '',
                evidence_message_ids_json TEXT NOT NULL DEFAULT '[]',
                confidence REAL NOT NULL DEFAULT 1.0,
                importance REAL NOT NULL DEFAULT 0.5,
                status TEXT NOT NULL DEFAULT 'active',
                supersedes_id INTEGER,
                expires_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS project_memories_scope
                ON project_memories(project_id, status, memory_type, updated_at);
            CREATE TABLE IF NOT EXISTS project_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS project_events_scope
                ON project_events(project_id, id);
            """
        )
        session_columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        if "project_id" not in session_columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN project_id TEXT DEFAULT ''")
        now = self._now_iso()
        conn.execute(
            """INSERT OR IGNORE INTO projects
               (id, name, purpose, purpose_evidence, purpose_source_session_id,
                purpose_updated_at, created_at, updated_at)
               VALUES (?, ?, '', '', '', NULL, ?, ?)""",
            (DEFAULT_PROJECT_ID, DEFAULT_PROJECT_NAME, now, now),
        )
        conn.execute(
            "UPDATE sessions SET project_id = ? WHERE project_id IS NULL OR trim(project_id) = ''",
            (DEFAULT_PROJECT_ID,),
        )
        conn.execute(
            """INSERT OR IGNORE INTO project_state
               (project_id, state_json, updated_through_message_id, version, updated_at)
               VALUES (?, '{}', 0, 0, ?)""",
            (DEFAULT_PROJECT_ID, now),
        )

    def get_project(self, project_id: str = DEFAULT_PROJECT_ID) -> Optional[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return dict(row) if row else None

    def create_project(self, name: str, project_id: str) -> Dict[str, Any]:
        project_id = str(project_id or "").strip()
        name = str(name or "").strip()
        if not project_id or not name:
            raise ValueError("Project id and name are required")
        now = self._now_iso()
        with closing(self._connect()) as conn:
            conn.execute(
                """INSERT INTO projects
                   (id, name, purpose, purpose_evidence, purpose_source_session_id,
                    purpose_updated_at, created_at, updated_at)
                   VALUES (?, ?, '', '', '', NULL, ?, ?)""",
                (project_id, name, now, now),
            )
            conn.execute(
                """INSERT INTO project_state
                   (project_id, state_json, updated_through_message_id, version, updated_at)
                   VALUES (?, '{}', 0, 0, ?)""",
                (project_id, now),
            )
            conn.commit()
        return self.get_project(project_id) or {}

    def get_session_project_id(self, session_id: str) -> str:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT project_id FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return str(row["project_id"] or DEFAULT_PROJECT_ID) if row else DEFAULT_PROJECT_ID

    def set_project_purpose(
        self,
        project_id: str,
        purpose: str,
        *,
        evidence: str = "",
        source_session_id: str = "",
    ) -> Dict[str, Any]:
        purpose = str(purpose or "").strip()
        now = self._now_iso()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute("SELECT purpose FROM projects WHERE id = ?", (project_id,)).fetchone()
            if not previous:
                raise ValueError("Project not found")
            conn.execute(
                """UPDATE projects
                   SET purpose = ?, purpose_evidence = ?, purpose_source_session_id = ?,
                       purpose_updated_at = ?, updated_at = ?
                   WHERE id = ?""",
                (purpose, evidence, source_session_id, now, now, project_id),
            )
            self._append_project_event_conn(
                conn,
                project_id,
                source_session_id,
                "purpose.updated" if purpose else "purpose.cleared",
                {"before": str(previous["purpose"] or ""), "after": purpose, "evidence": evidence},
                now,
            )
            conn.commit()
        return self.get_project(project_id) or {}

    def get_session_state(self, session_id: str) -> Dict[str, Any]:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM session_state WHERE session_id = ?", (session_id,)).fetchone()
        if not row:
            return {}
        result = self._load_json(row["state_json"])
        result.update(version=int(row["version"]), updated_at=row["updated_at"])
        return result

    def get_project_state(self, project_id: str = DEFAULT_PROJECT_ID) -> Dict[str, Any]:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM project_state WHERE project_id = ?", (project_id,)).fetchone()
        if not row:
            return {}
        result = self._load_json(row["state_json"])
        result.update(version=int(row["version"]), updated_at=row["updated_at"])
        return result

    def update_session_state(
        self,
        session_id: str,
        patch: Dict[str, Any],
        *,
        through_message_id: int = 0,
    ) -> Dict[str, Any]:
        project_id = self.get_session_project_id(session_id)
        now = self._now_iso()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT state_json, version FROM session_state WHERE session_id = ?", (session_id,)).fetchone()
            current = self._load_json(row["state_json"]) if row else {}
            merged = self._merge_state(current, patch)
            version = int(row["version"] if row else 0) + 1
            conn.execute(
                """INSERT INTO session_state
                   (session_id, project_id, state_json, updated_through_message_id, version, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                       project_id=excluded.project_id, state_json=excluded.state_json,
                       updated_through_message_id=excluded.updated_through_message_id,
                       version=excluded.version, updated_at=excluded.updated_at""",
                (session_id, project_id, self._dump_json(merged), int(through_message_id or 0), version, now),
            )
            conn.commit()
        return {**merged, "version": version, "updated_at": now}

    def replace_session_state(
        self,
        session_id: str,
        state: Dict[str, Any],
        *,
        through_message_id: int = 0,
    ) -> Dict[str, Any]:
        """Replace the current conversation's working set after a completed turn."""
        project_id = self.get_session_project_id(session_id)
        clean = {key: value for key, value in (state or {}).items() if value not in (None, "", [], {})}
        now = self._now_iso()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT version FROM session_state WHERE session_id=?", (session_id,)).fetchone()
            version = int(row["version"] if row else 0) + 1
            conn.execute(
                """INSERT INTO session_state
                   (session_id, project_id, state_json, updated_through_message_id, version, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                       project_id=excluded.project_id, state_json=excluded.state_json,
                       updated_through_message_id=excluded.updated_through_message_id,
                       version=excluded.version, updated_at=excluded.updated_at""",
                (session_id, project_id, self._dump_json(clean), int(through_message_id or 0), version, now),
            )
            conn.commit()
        return {**clean, "version": version, "updated_at": now}

    def update_project_state(
        self,
        project_id: str,
        patch: Dict[str, Any],
        *,
        through_message_id: int = 0,
        source_session_id: str = "",
    ) -> Dict[str, Any]:
        now = self._now_iso()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT state_json, version FROM project_state WHERE project_id = ?", (project_id,)).fetchone()
            current = self._load_json(row["state_json"]) if row else {}
            merged = self._merge_state(current, patch)
            version = int(row["version"] if row else 0) + 1
            conn.execute(
                """INSERT INTO project_state
                   (project_id, state_json, updated_through_message_id, version, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(project_id) DO UPDATE SET
                       state_json=excluded.state_json,
                       updated_through_message_id=excluded.updated_through_message_id,
                       version=excluded.version, updated_at=excluded.updated_at""",
                (project_id, self._dump_json(merged), int(through_message_id or 0), version, now),
            )
            self._append_project_event_conn(
                conn, project_id, source_session_id, "project_state.updated",
                {"patch": patch, "version": version, "through_message_id": int(through_message_id or 0)}, now,
            )
            conn.commit()
        return {**merged, "version": version, "updated_at": now}

    def replace_project_state(
        self,
        project_id: str,
        state: Dict[str, Any],
        *,
        through_message_id: int = 0,
        source_session_id: str = "",
    ) -> Dict[str, Any]:
        """Replace the materialized state after active durable memories change."""
        clean = {key: value for key, value in (state or {}).items() if value not in (None, "", [], {})}
        now = self._now_iso()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT version FROM project_state WHERE project_id=?", (project_id,)).fetchone()
            version = int(row["version"] if row else 0) + 1
            conn.execute(
                """INSERT INTO project_state
                   (project_id, state_json, updated_through_message_id, version, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(project_id) DO UPDATE SET
                       state_json=excluded.state_json,
                       updated_through_message_id=excluded.updated_through_message_id,
                       version=excluded.version, updated_at=excluded.updated_at""",
                (project_id, self._dump_json(clean), int(through_message_id or 0), version, now),
            )
            self._append_project_event_conn(
                conn, project_id, source_session_id, "project_state.rebuilt",
                {"version": version, "through_message_id": int(through_message_id or 0)}, now,
            )
            conn.commit()
        return {**clean, "version": version, "updated_at": now}

    def add_project_memory(
        self,
        project_id: str,
        memory_type: str,
        content: str,
        *,
        source_session_id: str = "",
        evidence_message_ids: Optional[Iterable[int]] = None,
        confidence: float = 1.0,
        importance: float = 0.5,
        ttl_days: Optional[int] = None,
        supersedes_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        memory_type = str(memory_type or "topic").strip().lower()
        if memory_type not in self._PROJECT_MEMORY_TYPES:
            return None
        content = " ".join(str(content or "").split()).strip()
        if len(content) < 2:
            return None
        confidence = max(0.0, min(float(confidence), 1.0))
        importance = max(0.0, min(float(importance), 1.0))
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(days=int(ttl_days))).isoformat(timespec="seconds") if ttl_days else None
        now_iso = now.isoformat(timespec="seconds")
        evidence = [int(item) for item in (evidence_message_ids or []) if int(item) > 0]
        normalized = self._normalize_memory(content)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """SELECT id, content FROM project_memories
                   WHERE project_id = ? AND memory_type = ? AND status = 'active'""",
                (project_id, memory_type),
            ).fetchall()
            existing = next((row for row in rows if self._normalize_memory(row["content"]) == normalized), None)
            if existing:
                memory_id = int(existing["id"])
                conn.execute(
                    """UPDATE project_memories SET source_session_id=?, evidence_message_ids_json=?,
                       confidence=max(confidence, ?), importance=max(importance, ?),
                       expires_at=?, updated_at=? WHERE id=?""",
                    (source_session_id, json.dumps(evidence), confidence, importance, expires_at, now_iso, memory_id),
                )
                event_type = "memory.refreshed"
            else:
                supersedes = None
                if supersedes_id:
                    replaced = conn.execute(
                        """SELECT id FROM project_memories
                           WHERE id=? AND project_id=? AND memory_type=? AND status='active'""",
                        (int(supersedes_id), project_id, memory_type),
                    ).fetchone()
                    if replaced:
                        supersedes = int(replaced["id"])
                        conn.execute(
                            "UPDATE project_memories SET status='superseded', updated_at=? WHERE id=?",
                            (now_iso, supersedes),
                        )
                cursor = conn.execute(
                    """INSERT INTO project_memories
                       (project_id, memory_type, content, source_session_id,
                        evidence_message_ids_json, confidence, importance, status,
                        supersedes_id, expires_at, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
                    (project_id, memory_type, content, source_session_id, json.dumps(evidence),
                     confidence, importance, supersedes, expires_at, now_iso, now_iso),
                )
                memory_id = int(cursor.lastrowid)
                event_type = "memory.added"
            self._append_project_event_conn(
                conn, project_id, source_session_id, event_type,
                {"memory_id": memory_id, "memory_type": memory_type, "content": content,
                 "evidence_message_ids": evidence, "supersedes_id": supersedes_id}, now_iso,
            )
            conn.commit()
        return self.get_project_memory(memory_id)

    def get_project_memory(self, memory_id: int) -> Optional[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM project_memories WHERE id = ?", (int(memory_id),)).fetchone()
        return self._memory_row(row) if row else None

    def search_project_memories(
        self,
        project_id: str,
        query: str = "",
        *,
        limit: int = 6,
    ) -> List[Dict[str, Any]]:
        now = self._now_iso()
        with closing(self._connect()) as conn:
            conn.execute(
                """UPDATE project_memories SET status='expired'
                   WHERE project_id=? AND status='active' AND expires_at IS NOT NULL AND expires_at < ?""",
                (project_id, now),
            )
            conn.commit()
            rows = conn.execute(
                """SELECT * FROM project_memories
                   WHERE project_id=? AND status='active'
                   ORDER BY importance DESC, updated_at DESC LIMIT 200""",
                (project_id,),
            ).fetchall()
        terms = self._memory_terms(query)
        ranked = []
        for row in rows:
            item = self._memory_row(row)
            text = f"{item['memory_type']} {item['content']}".lower()
            lexical = sum(1.0 for term in terms if term in text)
            if query and not lexical:
                continue
            score = lexical * 2.0 + float(item["importance"]) + float(item["confidence"])
            ranked.append((score, item))
        ranked.sort(key=lambda pair: (pair[0], pair[1]["updated_at"]), reverse=True)
        if not query and not ranked:
            return []
        return [item for _, item in ranked[: max(1, min(int(limit), 200))]]

    def list_project_events(self, project_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM project_events WHERE project_id=? ORDER BY id DESC LIMIT ?",
                (project_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [{**dict(row), "payload": self._load_json(row["payload_json"])} for row in rows]

    def search_project_history(
        self,
        project_id: str,
        query: str,
        *,
        limit: int = 6,
        exclude_session_id: str = "",
    ) -> List[Dict[str, Any]]:
        """Search raw messages across conversations in one project."""
        query = str(query or "").strip()
        if not query:
            return []
        params: List[Any] = [project_id, query]
        exclude = ""
        if exclude_session_id:
            exclude = " AND m.session_id <> ?"
            params.append(exclude_session_id)
        params.append(max(1, min(int(limit), 20)))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""SELECT m.id AS message_id, m.session_id, s.title AS session_title,
                           m.role, m.content
                    FROM messages m
                    JOIN sessions s ON s.id = m.session_id
                    WHERE s.project_id = ?
                      AND instr(lower(m.content), lower(?)) > 0
                      {exclude}
                    ORDER BY m.id DESC LIMIT ?""",
                params,
            ).fetchall()
        result = []
        for row in rows:
            content = str(row["content"] or "")
            offset = max(0, content.lower().find(query.lower()) - 120)
            result.append({
                "message_id": int(row["message_id"]),
                "session_id": row["session_id"],
                "session_title": row["session_title"],
                "role": row["role"],
                "offset": offset,
                "excerpt": content[offset:offset + 1000],
                "total_chars": len(content),
            })
        return result

    def read_project_messages(
        self,
        project_id: str,
        source_session_id: str,
        start_id: int,
        end_id: int,
        *,
        offset: int = 0,
        limit: int = 8,
    ) -> List[Dict[str, Any]]:
        """Read another conversation only when it belongs to the same project."""
        with closing(self._connect()) as conn:
            belongs = conn.execute(
                "SELECT 1 FROM sessions WHERE id=? AND project_id=?",
                (source_session_id, project_id),
            ).fetchone()
            if not belongs:
                return []
            rows = conn.execute(
                """SELECT id, role, content FROM messages
                   WHERE session_id=? AND id BETWEEN ? AND ? ORDER BY id LIMIT ?""",
                (source_session_id, int(start_id), int(end_id), max(1, min(int(limit), 8))),
            ).fetchall()
        offset = max(0, int(offset))
        return [{
            "message_id": int(row["id"]),
            "session_id": source_session_id,
            "role": row["role"],
            "content": str(row["content"] or "")[offset:offset + 4000],
            "offset": offset,
            "next_offset": offset + 4000 if len(str(row["content"] or "")) > offset + 4000 else None,
        } for row in rows]

    def render_project_context(self, session_id: str, query: str = "", memory_limit: int = 6) -> str:
        project_id = self.get_session_project_id(session_id)
        project = self.get_project(project_id) or {}
        project_state = self.get_project_state(project_id)
        session_state = self.get_session_state(session_id)
        memories = self.search_project_memories(project_id, query, limit=memory_limit)
        lines = [f"project: {project.get('name') or DEFAULT_PROJECT_NAME}"]
        if str(project.get("purpose") or "").strip():
            lines.append("purpose:\n" + str(project["purpose"]).strip())
        for label, state in (("project_state", project_state), ("session_state", session_state)):
            visible = {key: value for key, value in state.items() if key not in {"version", "updated_at"} and value not in (None, "", [], {})}
            if visible:
                lines.append(f"{label}: " + json.dumps(visible, ensure_ascii=False))
        if memories:
            lines.append("relevant_project_memories:")
            lines.extend(f"- [memory:{item['id']}][{item['memory_type']}] {item['content']}" for item in memories)
        return "\n".join(lines)

    @staticmethod
    def _merge_state(current: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(current or {})
        for key, value in (patch or {}).items():
            if value in (None, "", [], {}):
                continue
            if isinstance(value, list):
                existing = merged.get(key) if isinstance(merged.get(key), list) else []
                values = []
                seen = set()
                for item in [*existing, *value]:
                    marker = json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, (dict, list)) else str(item).strip()
                    if not marker or marker in seen:
                        continue
                    seen.add(marker)
                    values.append(item)
                merged[key] = values[-20:]
            else:
                merged[key] = value
        return merged

    @staticmethod
    def _normalize_memory(value: str) -> str:
        return re.sub(r"[\W_]+", "", value or "", flags=re.UNICODE).lower()

    @staticmethod
    def _memory_terms(query: str) -> List[str]:
        text = (query or "").lower()
        terms = set(re.findall(r"[a-z0-9_\-]{2,}", text))
        for block in re.findall(r"[\u4e00-\u9fff]{2,}", query or ""):
            terms.add(block)
            terms.update(block[index:index + 2] for index in range(len(block) - 1))
        return list(terms)

    def _memory_row(self, row) -> Dict[str, Any]:
        item = dict(row)
        try:
            item["evidence_message_ids"] = json.loads(item.pop("evidence_message_ids_json"))
        except (TypeError, json.JSONDecodeError):
            item["evidence_message_ids"] = []
        return item

    @staticmethod
    def _append_project_event_conn(conn, project_id: str, session_id: str, event_type: str,
                                   payload: Dict[str, Any], created_at: str) -> None:
        conn.execute(
            """INSERT INTO project_events(project_id, session_id, event_type, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (project_id, session_id, event_type, json.dumps(payload, ensure_ascii=False), created_at),
        )

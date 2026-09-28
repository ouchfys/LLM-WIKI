"""Optional task checklists stored alongside conversations in SQLite.

The old Markdown directory is a read-only migration source. New plans never
create directories or files. Tool-call history retains earlier submitted steps.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from system.storage.layout import get_storage_layout, resolve_database_path


class TaskPlanStore:
    def __init__(self, root=None, max_chars=20_000, db_path=None):
        # root remains accepted for importing existing plans and isolated callers.
        self.db_path = str(resolve_database_path(
            db_path or (Path(root).parent / "sessions.db" if root else None)))
        database = Path(self.db_path)
        uses_application_database = database == resolve_database_path(create_parent=False)
        configured_root = os.getenv("PAPERWIKI_TASKS_ROOT", "").strip() if uses_application_database else ""
        legacy_root = database.parent / ".paperwiki" / "tasks"
        if uses_application_database and not legacy_root.is_dir():
            legacy_root = get_storage_layout().data_root / ".paperwiki" / "tasks"
        self.root = Path(root or configured_root or legacy_root).expanduser()
        self.max_chars = max(2_000, int(max_chars))
        with closing(self._connect()) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS task_plans (
                    task_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    goal TEXT NOT NULL, steps_json TEXT NOT NULL,
                    explanation TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                    version INTEGER NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, legacy_markdown TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS task_plan_sessions (
                    task_id TEXT NOT NULL REFERENCES task_plans(task_id) ON DELETE CASCADE,
                    session_id TEXT NOT NULL, PRIMARY KEY(task_id, session_id)
                );
                CREATE INDEX IF NOT EXISTS task_plan_session_idx ON task_plan_sessions(session_id);
                CREATE TABLE IF NOT EXISTS task_plan_imports (
                    source_path TEXT PRIMARY KEY, task_id TEXT NOT NULL, imported_at TEXT NOT NULL
                );
            """)
        self._import_legacy()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")

    @staticmethod
    def _has_sessions(conn):
        return bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'").fetchone())

    @contextmanager
    def session_guard(self, session_id):
        """Commit plan writes and session deletion under the same database lock."""
        from system.agent_runtime.control import get_run_control, RunCancelled
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if self._has_sessions(conn) and not conn.execute(
                "SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
                raise RunCancelled("Conversation was deleted")
            control = get_run_control()
            if control and control.run_id:
                row = conn.execute(
                    "SELECT cancel_requested,current_state,lease_owner,lease_expires_at "
                    "FROM agent_runs WHERE id=?", (control.run_id,)).fetchone()
                if (not row or row[0] or row[1] != "CHAT_RUNNING"
                    or (getattr(control, "lease_owner", "")
                        and (row[2] != control.lease_owner or row[3] <= control.store.now_iso()))):
                    raise RunCancelled("Chat stopped or was deleted")
            yield conn
            conn.commit()

    def write(self, markdown=None, *, steps=None, goal="", explanation="",
              project_id, session_id, task_id="", reason="", create_new=False, status=None):
        # Accept old saved tool calls during recovery; only steps are advertised.
        legacy = str(markdown or "") if steps is None else ""
        if steps is None:
            if not legacy.strip():
                raise ValueError("plan steps are required")
            goal, steps = self._legacy_steps(legacy)
        steps = self._validate_steps(steps)
        goal = str(goal or "").strip()
        explanation = str(explanation or reason or "").strip()
        if len(json.dumps([goal, steps, explanation, legacy], ensure_ascii=False)) > self.max_chars:
            raise ValueError(f"plan exceeds {self.max_chars} characters")
        derived_status = "completed" if all(s["status"] == "completed" for s in steps) else "active"
        if status not in {None, "active", "completed"}:
            raise ValueError("plan status must be active or completed")
        # Preserve historical explicit status, without guessing old checkbox semantics.
        status = (status or derived_status) if legacy else derived_status
        if create_new and task_id:
            raise ValueError("create_new cannot be combined with task_id")
        with self.session_guard(session_id) as conn:
            task_id = str(task_id or "").strip()
            if not task_id and not create_new:
                task_id = self._current_task_id(conn, project_id, session_id)
            task_id = task_id or str(uuid.uuid4())
            old = conn.execute("SELECT * FROM task_plans WHERE task_id=?", (task_id,)).fetchone()
            if old and old["project_id"] != project_id:
                raise ValueError("task belongs to another project")
            if old and not goal:
                goal = old["goal"]
            now = self._now()
            conn.execute("""INSERT INTO task_plans VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(task_id) DO UPDATE SET
                goal=excluded.goal,steps_json=excluded.steps_json,
                explanation=excluded.explanation,status=excluded.status,
                version=excluded.version,updated_at=excluded.updated_at,
                legacy_markdown=excluded.legacy_markdown""",
                (task_id, project_id, goal, json.dumps(steps, ensure_ascii=False),
                 explanation, status, old["version"] + 1 if old else 1,
                 old["created_at"] if old else now, now, legacy))
            conn.execute("INSERT OR IGNORE INTO task_plan_sessions VALUES(?,?)", (task_id, session_id))
            return self._read(conn, task_id, project_id, session_id)

    @staticmethod
    def _validate_steps(steps):
        if not isinstance(steps, list) or not steps:
            raise ValueError("steps must be a non-empty list")
        result = []
        for item in steps:
            if (not isinstance(item, dict) or not isinstance(item.get("step"), str)
                or not item["step"].strip()
                or item.get("status") not in {"pending", "in_progress", "completed"}):
                raise ValueError("each step needs text and status: pending, in_progress or completed")
            result.append({"step": item["step"].strip(), "status": item["status"]})
        return result

    def _read(self, conn, task_id, project_id, session_id):
        row = conn.execute("SELECT * FROM task_plans WHERE task_id=? AND project_id=?",
                           (task_id, project_id)).fetchone()
        if not row:
            raise ValueError("task plan not found in current project")
        owners = [r[0] for r in conn.execute(
            "SELECT session_id FROM task_plan_sessions WHERE task_id=? ORDER BY session_id", (task_id,))]
        item = dict(row)
        item["steps"] = json.loads(item.pop("steps_json"))
        legacy = item.pop("legacy_markdown")
        if legacy:
            item["markdown"] = legacy  # Preserve all original migration/recovery notes.
        attached = session_id in owners
        return {**item, "session_ids": owners, "plan_scope": "attached" if attached else "reference",
                "attached_to_session": attached, "storage": "session_database"}

    def read(self, *, project_id, session_id, task_id=""):
        with closing(self._connect()) as conn:
            task_id = task_id or self._current_task_id(conn, project_id, session_id)
            if task_id:
                return self._read(conn, task_id, project_id, session_id)
            return {"task_id": "", "plan_scope": "index", "attached_to_session": False,
                    "active_tasks": self._list_active(conn, project_id)}

    def attach(self, *, task_id, project_id, session_id):
        with self.session_guard(session_id) as conn:
            self._read(conn, task_id, project_id, session_id)
            conn.execute("INSERT OR IGNORE INTO task_plan_sessions VALUES(?,?)", (task_id, session_id))
            return self._read(conn, task_id, project_id, session_id)

    def detach_sessions(self, session_ids, *, all_sessions=False, conn=None):
        if conn is None:
            with closing(self._connect()) as owned:
                owned.execute("BEGIN IMMEDIATE")
                result = self.detach_sessions(session_ids, all_sessions=all_sessions, conn=owned)
                owned.commit()
                return result
        if all_sessions:
            conn.execute("DELETE FROM task_plan_sessions")
        else:
            conn.executemany("DELETE FROM task_plan_sessions WHERE session_id=?",
                             [(sid,) for sid in session_ids])
        removed = [r[0] for r in conn.execute("""SELECT task_id FROM task_plans
            WHERE task_id NOT IN (SELECT task_id FROM task_plan_sessions)""")]
        conn.executemany("DELETE FROM task_plans WHERE task_id=?", [(tid,) for tid in removed])
        return removed

    @staticmethod
    def _current_task_id(conn, project_id, session_id):
        row = conn.execute("""SELECT p.task_id FROM task_plans p JOIN task_plan_sessions s
            ON s.task_id=p.task_id WHERE p.project_id=? AND s.session_id=? AND p.status='active'
            ORDER BY p.updated_at DESC,p.task_id DESC LIMIT 1""", (project_id, session_id)).fetchone()
        return row[0] if row else ""

    def current_task_id(self, *, project_id, session_id):
        with closing(self._connect()) as conn:
            return self._current_task_id(conn, project_id, session_id)

    @staticmethod
    def _list_active(conn, project_id, limit=12):
        return [dict(r) for r in conn.execute("""SELECT task_id,goal AS title,version,updated_at
            FROM task_plans WHERE project_id=? AND status='active'
            ORDER BY updated_at DESC,task_id DESC LIMIT ?""", (project_id, max(1, min(int(limit), 50))))]

    def list_active(self, *, project_id, limit=12):
        with closing(self._connect()) as conn:
            return self._list_active(conn, project_id, limit)

    def cache_token(self, *, project_id, session_id, task_id=""):
        with closing(self._connect()) as conn:
            task_id = task_id or self._current_task_id(conn, project_id, session_id)
            if task_id:
                item = self._read(conn, task_id, project_id, session_id)
                return f"{task_id}:{item['version']}:{item['attached_to_session']}"
            return "index:" + json.dumps(self._list_active(conn, project_id), sort_keys=True)

    def prompt_context(self, *, project_id, session_id):
        task_id = self.current_task_id(project_id=project_id, session_id=session_id)
        if not task_id:
            return "(no active task plan for this conversation)"
        item = self.read(project_id=project_id, session_id=session_id, task_id=task_id)
        return ("Saved checklist for this conversation. Apply it only if the current request continues "
                "this goal; answer independent questions directly. The latest steps are already here, "
                "so do not reread an unchanged plan.\n" + json.dumps(item, ensure_ascii=False))

    @staticmethod
    def _legacy_steps(markdown):
        def section(name):
            match = re.search(r"(?ms)^#\s+" + re.escape(name) + r"\s*\n(.*?)(?=^#\s|\Z)", markdown)
            return match.group(1).strip() if match else ""
        goal = section("Goal")
        steps = []
        for line in section("Steps").splitlines():
            match = re.match(r"\s*[-*]\s+(?:\[([ xX])\]\s*)?(.+)", line)
            if match:
                steps.append({"step": match[2], "status": "completed" if match[1] in {"x", "X"} else "pending"})
        return goal, steps or [{"step": goal or "Continue the saved task", "status": "pending"}]

    def _import_legacy(self):
        # Keep originals intact. The ledger prevents resurrection after deletion.
        if not self.root.is_dir():
            return
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for path in self.root.glob("*/metadata.json"):
                source = str(path.resolve())
                if conn.execute("SELECT 1 FROM task_plan_imports WHERE source_path=?", (source,)).fetchone():
                    continue
                try:
                    metadata = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(metadata, dict) or not metadata.get("project_id"):
                        raise ValueError("missing plan metadata")
                    markdown = path.with_name("PLAN.md").read_text(encoding="utf-8")
                except (OSError, UnicodeError, ValueError) as exc:
                    logging.getLogger(__name__).warning("Cannot import legacy plan %s: %s", path, exc)
                    continue
                task_id = str(metadata.get("task_id") or path.parent.name)
                owners = list(dict.fromkeys(filter(None, metadata.get("session_ids") or [])))
                if self._has_sessions(conn):
                    owners = [sid for sid in owners if conn.execute(
                        "SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone()]
                if owners and not conn.execute("SELECT 1 FROM task_plans WHERE task_id=?", (task_id,)).fetchone():
                    goal, steps = self._legacy_steps(markdown)
                    now = self._now()
                    conn.execute("INSERT INTO task_plans VALUES(?,?,?,?,?,?,?,?,?,?)",
                                 (task_id, metadata["project_id"], goal, json.dumps(steps, ensure_ascii=False),
                                  metadata.get("reason") or "", metadata.get("status") or "active",
                                  metadata.get("version") or 1, metadata.get("created_at") or now,
                                  metadata.get("updated_at") or now, markdown))
                    conn.executemany("INSERT INTO task_plan_sessions VALUES(?,?)", [(task_id, sid) for sid in owners])
                conn.execute("INSERT INTO task_plan_imports VALUES(?,?,?)", (source, task_id, self._now()))
            conn.commit()

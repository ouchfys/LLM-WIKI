from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


TERMINAL_STATES = {"COMPLETED", "REJECTED", "CANCELLED"}
WAITING_STATES = {"AWAITING_APPROVAL", "COMMIT_FAILED"}
EXECUTING_STATES = {
    "QUEUED", "EXTRACTING", "DISTILLING", "VERIFYING", "REVISING",
    "COMPILING_PROPOSAL", "COMMITTING", "REINDEXING",
}
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "QUEUED": {"EXTRACTING", "CHAT_RUNNING", "FAILED", "CANCELLED"},
    "CHAT_RUNNING": {"COMPLETED", "FAILED", "CANCELLED"},
    "EXTRACTING": {"DISTILLING", "FAILED", "CANCELLED"},
    "DISTILLING": {"VERIFYING", "FAILED", "CANCELLED"},
    "VERIFYING": {"REVISING", "COMPILING_PROPOSAL", "REJECTED", "FAILED", "CANCELLED"},
    "REVISING": {"VERIFYING", "REJECTED", "FAILED", "CANCELLED"},
    "COMPILING_PROPOSAL": {"AWAITING_APPROVAL", "COMMITTING", "REJECTED", "FAILED", "CANCELLED"},
    "AWAITING_APPROVAL": {"COMMITTING", "REJECTED", "CANCELLED", "FAILED"},
    "COMMITTING": {"REINDEXING", "COMMIT_FAILED", "FAILED"},
    "REINDEXING": {"COMPLETED", "COMMIT_FAILED", "FAILED"},
    "COMMIT_FAILED": {"COMMITTING", "AWAITING_APPROVAL", "CANCELLED", "FAILED"},
    "COMPLETED": set(),
    "REJECTED": set(),
    "FAILED": {"QUEUED"},
    "CANCELLED": set(),
}


class InvalidStateTransition(RuntimeError):
    pass


class AgentRunStore:
    """SQLite-backed state, checkpoint, trace, and approval store."""

    def __init__(self, db_path: str | None = None):
        repo_root = Path(__file__).resolve().parents[2]
        self.db_path = str(Path(db_path) if db_path else repo_root / "sessions.db")
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    @staticmethod
    def future_iso(seconds: int) -> str:
        return (datetime.now(timezone.utc) + timedelta(seconds=max(1, int(seconds)))).isoformat(
            timespec="milliseconds"
        )

    @staticmethod
    def dump_json(value: Any) -> str:
        return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)

    @staticmethod
    def load_json(value: str, default: Any = None) -> Any:
        if not value:
            return {} if default is None else default
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {} if default is None else default

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_runs (
                    id TEXT PRIMARY KEY,
                    run_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_state TEXT NOT NULL,
                    state_version INTEGER NOT NULL DEFAULT 0,
                    approval_mode TEXT NOT NULL DEFAULT 'manual',
                    source_uri TEXT DEFAULT '',
                    source_packet_id TEXT DEFAULT '',
                    ingestion_job_id TEXT DEFAULT '',
                    context_json TEXT DEFAULT '{}',
                    result_json TEXT DEFAULT '{}',
                    error TEXT DEFAULT '',
                    lease_owner TEXT DEFAULT '',
                    lease_expires_at TEXT DEFAULT '',
                    heartbeat_at TEXT DEFAULT '',
                    attempt INTEGER NOT NULL DEFAULT 0,
                    resume_from_state TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_agent_runs_job ON agent_runs(ingestion_job_id);

                CREATE TABLE IF NOT EXISTS agent_checkpoints (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    state_version INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    context_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES agent_runs(id) ON DELETE CASCADE,
                    UNIQUE(run_id, state_version)
                );

                CREATE TABLE IF NOT EXISTS agent_events (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    trace_id TEXT NOT NULL,
                    span_id TEXT NOT NULL,
                    parent_span_id TEXT DEFAULT '',
                    event_type TEXT NOT NULL,
                    node_name TEXT DEFAULT '',
                    status TEXT DEFAULT '',
                    model TEXT DEFAULT '',
                    tool_name TEXT DEFAULT '',
                    input_json TEXT DEFAULT '{}',
                    output_json TEXT DEFAULT '{}',
                    usage_json TEXT DEFAULT '{}',
                    duration_ms REAL DEFAULT 0,
                    error TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES agent_runs(id) ON DELETE CASCADE,
                    UNIQUE(run_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_events_run ON agent_events(run_id, sequence);

                CREATE TABLE IF NOT EXISTS agent_approvals (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL,
                    page_id TEXT NOT NULL,
                    title TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    decision_reason TEXT DEFAULT '',
                    commit_error TEXT DEFAULT '',
                    superseded_by TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    decided_at TEXT DEFAULT '',
                    FOREIGN KEY(run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_agent_approvals_status ON agent_approvals(status, created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_approvals_revision ON agent_approvals(revision_id);

                CREATE TABLE IF NOT EXISTS agent_run_inputs (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_run_inputs_pending
                    ON agent_run_inputs(run_id, status, created_at);
                """
            )
            self._ensure_column(conn, "agent_runs", "lease_owner", "TEXT DEFAULT ''")
            self._ensure_column(conn, "agent_runs", "lease_expires_at", "TEXT DEFAULT ''")
            self._ensure_column(conn, "agent_runs", "heartbeat_at", "TEXT DEFAULT ''")
            self._ensure_column(conn, "agent_runs", "attempt", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "agent_runs", "resume_from_state", "TEXT DEFAULT ''")
            self._ensure_column(conn, "agent_runs", "cancel_requested", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "agent_runs", "input_closed", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "agent_approvals", "commit_error", "TEXT DEFAULT ''")
            conn.commit()

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def create_run(
        self,
        *,
        run_type: str,
        source_uri: str = "",
        approval_mode: str = "risk",
        ingestion_job_id: str = "",
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        now = self.now_iso()
        mode = approval_mode if approval_mode in {"risk", "manual", "auto"} else "risk"
        with closing(self._connect()) as conn:
            conn.execute(
                """INSERT INTO agent_runs
                   (id, run_type, status, current_state, state_version, approval_mode,
                    source_uri, ingestion_job_id, context_json, created_at, updated_at)
                   VALUES (?, ?, 'queued', 'QUEUED', 0, ?, ?, ?, ?, ?, ?)""",
                (run_id, run_type, mode, source_uri, ingestion_job_id, self.dump_json(context or {}), now, now),
            )
            conn.execute(
                """INSERT INTO agent_checkpoints
                   (id, run_id, state_version, state, context_json, created_at)
                   VALUES (?, ?, 0, 'QUEUED', ?, ?)""",
                (str(uuid.uuid4()), run_id, self.dump_json(context or {}), now),
            )
            conn.commit()
        self.append_event(run_id, event_type="run.started", node_name="QUEUED", status="queued")
        return self.get_run(run_id) or {}

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        return self._run_row(row) if row else None

    def find_by_ingestion_job(self, job_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM agent_runs WHERE ingestion_job_id=? ORDER BY created_at DESC LIMIT 1",
                (job_id,),
            ).fetchone()
        return self._run_row(row) if row else None

    def request_cancel(self, run_id: str) -> dict[str, Any]:
        """Stop accepting input immediately; the worker exits at a safe point."""
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(run_id)
            if row["current_state"] in TERMINAL_STATES | {"FAILED"}:
                return self._run_row(row)
            if row["run_type"] != "wiki_chat":
                raise ValueError("This cancellation endpoint currently supports Wiki chat runs only.")
            if row["input_closed"]:
                raise ValueError("The answer is already being saved; wait for completion.")
            changed = not bool(row["cancel_requested"])
            conn.execute(
                "UPDATE agent_runs SET cancel_requested=1, updated_at=? WHERE id=?",
                (self.now_iso(), run_id),
            )
            conn.commit()
        if changed:
            self.append_event(run_id, event_type="run.cancel_requested", status="cancelling")
        return self.get_run(run_id) or {}

    def enqueue_input(self, run_id: str, *, kind: str, content: str, input_id: str) -> dict[str, Any]:
        if kind not in {"interrupt", "followup", "command"}:
            raise ValueError("Unsupported input kind")
        content = content.strip()
        if not content or len(content) > 8000:
            raise ValueError("Input must contain 1–8000 characters")
        if kind == "command" and not (
            content == "/compact" or content.startswith("/wiki ")
        ):
            raise ValueError("Only /wiki <instruction> and /compact can be queued as commands")
        now = self.now_iso()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM agent_run_inputs WHERE id=?", (input_id,)).fetchone()
            if existing:
                if existing["run_id"] != run_id or existing["kind"] != kind or existing["content"] != content:
                    raise ValueError("Input id already belongs to a different request")
                return dict(existing)
            run = conn.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise KeyError(run_id)
            if run["run_type"] != "wiki_chat" or run["current_state"] != "CHAT_RUNNING" or run["cancel_requested"] or run["input_closed"]:
                raise ValueError("This run no longer accepts messages; send a new turn.")
            count = conn.execute(
                "SELECT COUNT(*) FROM agent_run_inputs WHERE run_id=? AND status='pending'", (run_id,)
            ).fetchone()[0]
            if count >= 20:
                raise ValueError("The queue is full (20 messages)")
            conn.execute(
                "INSERT INTO agent_run_inputs (id,run_id,kind,content,status,created_at,updated_at) VALUES (?,?,?,?,'pending',?,?)",
                (input_id, run_id, kind, content, now, now),
            )
            conn.commit()
        self.append_event(run_id, event_type=f"input.{kind}.queued", status="pending", input_data={"input_id": input_id, "chars": len(content)})
        return {"id": input_id, "run_id": run_id, "kind": kind, "content": content, "status": "pending"}

    def list_inputs(self, run_id: str = "", *, session_id: str = "") -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            if run_id:
                rows = conn.execute("SELECT * FROM agent_run_inputs WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
            else:
                rows = conn.execute(
                    """SELECT i.* FROM agent_run_inputs i JOIN agent_runs r ON r.id=i.run_id
                       WHERE r.source_uri=? AND i.status IN ('pending','ready','blocked','running','failed')
                       ORDER BY i.rowid LIMIT 100""", (f"session:{session_id}",)
                ).fetchall()
        return [dict(row) for row in rows]

    def take_interrupts(self, run_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute("SELECT cancel_requested,input_closed,current_state FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if not run or run["cancel_requested"] or run["input_closed"] or run["current_state"] != "CHAT_RUNNING":
                return []
            rows = conn.execute(
                "SELECT * FROM agent_run_inputs WHERE run_id=? AND kind='interrupt' AND status='pending' ORDER BY rowid", (run_id,)
            ).fetchall()
            conn.execute(
                "UPDATE agent_run_inputs SET status='consumed',updated_at=? WHERE run_id=? AND kind='interrupt' AND status='pending'",
                (self.now_iso(), run_id),
            )
            conn.commit()
        return [dict(row) for row in rows]

    def close_chat_input(self, run_id: str) -> bool:
        """Atomic fence: no cancellation/interruption can race answer persistence."""
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """UPDATE agent_runs SET input_closed=1 WHERE id=? AND cancel_requested=0
                   AND current_state='CHAT_RUNNING' AND NOT EXISTS (
                       SELECT 1 FROM agent_run_inputs WHERE run_id=? AND kind='interrupt' AND status='pending')""",
                (run_id, run_id),
            )
            conn.commit()
        return cursor.rowcount == 1

    def update_input(self, run_id: str, input_id: str, status: str) -> dict[str, Any]:
        # A durable claim prevents two browser tabs executing the same queued write.
        expected = {"running": "ready", "completed": "running", "failed": "running", "blocked": "ready", "dismissed": "blocked"}.get(status)
        if not expected:
            raise ValueError("Unsupported queue transition")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM agent_run_inputs WHERE id=? AND run_id=?", (input_id, run_id)).fetchone()
            if not row:
                raise KeyError(input_id)
            if row["status"] != expected and not (status == "dismissed" and row["status"] == "failed"):
                raise ValueError(f"Input is {row['status']}, expected {expected}")
            conn.execute("UPDATE agent_run_inputs SET status=?,updated_at=? WHERE id=?", (status, self.now_iso(), input_id))
            conn.commit()
        self.append_event(run_id, event_type=f"input.{status}", status=status, input_data={"input_id": input_id})
        return {**dict(row), "status": status}

    def list_runs(self, *, limit: int = 100, status: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM agent_runs"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._run_row(row) for row in rows]

    def acquire_lease(self, run_id: str, owner: str, *, ttl_seconds: int = 90) -> bool:
        if not owner:
            raise ValueError("A non-empty lease owner is required.")
        now = self.now_iso()
        expires = self.future_iso(ttl_seconds)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT current_state, lease_owner, lease_expires_at FROM agent_runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"Agent run not found: {run_id}")
            if str(row["current_state"]) in TERMINAL_STATES:
                conn.rollback()
                return False
            current_owner = str(row["lease_owner"] or "")
            current_expiry = str(row["lease_expires_at"] or "")
            if current_owner and current_owner != owner and current_expiry > now:
                conn.rollback()
                return False
            cursor = conn.execute(
                """UPDATE agent_runs
                   SET lease_owner=?, lease_expires_at=?, heartbeat_at=?, updated_at=?
                   WHERE id=?""",
                (owner, expires, now, now, run_id),
            )
            conn.commit()
        if cursor.rowcount == 1:
            self.append_event(
                run_id, event_type="lease.acquired", node_name="worker_lease", status="acquired",
                output_data={"owner": owner, "expires_at": expires},
            )
            return True
        return False

    def heartbeat(self, run_id: str, owner: str, *, ttl_seconds: int = 90) -> bool:
        now = self.now_iso()
        expires = self.future_iso(ttl_seconds)
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """UPDATE agent_runs SET heartbeat_at=?, lease_expires_at=?, updated_at=?
                   WHERE id=? AND lease_owner=?""",
                (now, expires, now, run_id, owner),
            )
            conn.commit()
        return cursor.rowcount == 1

    def release_lease(self, run_id: str, owner: str) -> bool:
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """UPDATE agent_runs
                   SET lease_owner='', lease_expires_at='', updated_at=?
                   WHERE id=? AND lease_owner=?""",
                (self.now_iso(), run_id, owner),
            )
            conn.commit()
        if cursor.rowcount == 1:
            self.append_event(
                run_id, event_type="lease.released", node_name="worker_lease", status="released",
                output_data={"owner": owner},
            )
            return True
        return False

    def list_recoverable_runs(
        self, *, limit: int = 100, include_failed: bool = False,
        include_commit_failed: bool = False,
    ) -> list[dict[str, Any]]:
        now = self.now_iso()
        states = set(EXECUTING_STATES)
        if include_commit_failed:
            states.add("COMMIT_FAILED")
        if include_failed:
            states.add("FAILED")
        states = sorted(states)
        placeholders = ",".join("?" for _ in states)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""SELECT * FROM agent_runs
                    WHERE (
                        current_state IN ({placeholders})
                        OR (
                            current_state='AWAITING_APPROVAL'
                            AND NOT EXISTS (
                                SELECT 1 FROM agent_approvals aa
                                WHERE aa.run_id=agent_runs.id AND aa.status='pending'
                            )
                            AND EXISTS (
                                SELECT 1 FROM agent_approvals aa
                                WHERE aa.run_id=agent_runs.id
                                  AND aa.status IN ('accepted', 'committing')
                            )
                        )
                    )
                      AND (lease_owner='' OR lease_expires_at='' OR lease_expires_at<=?)
                    ORDER BY updated_at LIMIT ?""",
                [*states, now, max(1, min(int(limit), 500))],
            ).fetchall()
        return [self._run_row(row) for row in rows]

    def restart_run(self, run_id: str, *, reason: str = "manual retry") -> dict[str, Any]:
        """Replay an interrupted ingestion from its immutable source while preserving audit history."""
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(f"Agent run not found: {run_id}")
            current = str(row["current_state"])
            if current in {"AWAITING_APPROVAL", "COMPLETED", "REJECTED", "CANCELLED"}:
                raise InvalidStateTransition(f"Run in {current} cannot be replayed from source.")
            version = int(row["state_version"]) + 1
            attempt = int(row["attempt"] or 0) + 1
            context = self.load_json(row["context_json"], {})
            context["recovery_reason"] = reason
            context["resume_from_state"] = current
            now = self.now_iso()
            conn.execute(
                """UPDATE agent_runs SET status='queued', current_state='QUEUED',
                   state_version=?, context_json=?, error='', attempt=?, resume_from_state=?,
                   lease_owner='', lease_expires_at='', updated_at=? WHERE id=?""",
                (version, self.dump_json(context), attempt, current, now, run_id),
            )
            conn.execute(
                """INSERT INTO agent_checkpoints
                   (id, run_id, state_version, state, context_json, created_at)
                   VALUES (?, ?, ?, 'QUEUED', ?, ?)""",
                (str(uuid.uuid4()), run_id, version, self.dump_json(context), now),
            )
            conn.commit()
        self.append_event(
            run_id, event_type="run.restarted", node_name="QUEUED", status="queued",
            input_data={"from": current, "reason": reason, "attempt": attempt},
        )
        return self.get_run(run_id) or {}

    def transition(
        self,
        run_id: str,
        next_state: str,
        *,
        context_updates: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        reason: str = "",
        expected_state: str | None = None,
    ) -> dict[str, Any]:
        next_state = str(next_state).upper()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(f"Agent run not found: {run_id}")
            current = str(row["current_state"])
            if expected_state and current != str(expected_state).upper():
                raise InvalidStateTransition(
                    f"Expected {str(expected_state).upper()}, current state is {current}"
                )
            if next_state != current and next_state not in ALLOWED_TRANSITIONS.get(current, set()):
                raise InvalidStateTransition(f"{current} -> {next_state} is not allowed")
            version = int(row["state_version"]) + (0 if next_state == current else 1)
            context = self.load_json(row["context_json"], {})
            if isinstance(context_updates, dict):
                context.update(context_updates)
            current_result = self.load_json(row["result_json"], {})
            if isinstance(result, dict):
                current_result.update(result)
            status = self._status_for_state(next_state)
            now = self.now_iso()
            conn.execute(
                """UPDATE agent_runs SET status=?, current_state=?, state_version=?, context_json=?,
                   result_json=?, error=?, updated_at=? WHERE id=?""",
                (status, next_state, version, self.dump_json(context), self.dump_json(current_result),
                 error if error is not None else row["error"], now, run_id),
            )
            if next_state != current:
                conn.execute(
                    """INSERT INTO agent_checkpoints
                       (id, run_id, state_version, state, context_json, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (str(uuid.uuid4()), run_id, version, next_state, self.dump_json(context), now),
                )
            if next_state in TERMINAL_STATES | {"FAILED"}:
                conn.execute(
                    "UPDATE agent_run_inputs SET status=?,updated_at=? WHERE run_id=? AND status='pending'",
                    ("ready" if next_state == "COMPLETED" else "blocked", now, run_id),
                )
            conn.commit()
        if next_state != current:
            self.append_event(
                run_id,
                event_type="state.transitioned",
                node_name=next_state,
                status=status,
                input_data={"from": current, "to": next_state, "reason": reason},
            )
            self.append_event(run_id, event_type="checkpoint.saved", node_name=next_state, status="saved")
        return self.get_run(run_id) or {}

    def mark_failed(self, run_id: str, error: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        if not run:
            raise KeyError(run_id)
        if run["current_state"] in TERMINAL_STATES:
            return run
        return self.transition(run_id, "FAILED", error=error, reason=error[:300])

    def append_event(
        self,
        run_id: str,
        *,
        event_type: str,
        node_name: str = "",
        status: str = "",
        trace_id: str = "",
        span_id: str = "",
        parent_span_id: str = "",
        model: str = "",
        tool_name: str = "",
        input_data: Any = None,
        output_data: Any = None,
        usage: Any = None,
        duration_ms: float = 0.0,
        error: str = "",
    ) -> dict[str, Any]:
        event_id = str(uuid.uuid4())
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM agent_runs WHERE id=?", (run_id,)).fetchone():
                raise KeyError(f"Agent run not found: {run_id}")
            sequence = int(conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM agent_events WHERE run_id=?", (run_id,)
            ).fetchone()[0])
            resolved_trace = trace_id or run_id
            resolved_span = span_id or event_id
            conn.execute(
                """INSERT INTO agent_events
                   (id, run_id, sequence, trace_id, span_id, parent_span_id, event_type,
                    node_name, status, model, tool_name, input_json, output_json, usage_json,
                    duration_ms, error, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_id, run_id, sequence, resolved_trace, resolved_span, parent_span_id,
                 event_type, node_name, status, model, tool_name, self.dump_json(input_data),
                 self.dump_json(output_data), self.dump_json(usage), float(duration_ms or 0),
                 str(error or "")[:2000], self.now_iso()),
            )
            conn.commit()
        return self.get_event(event_id) or {}

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM agent_events WHERE id=?", (event_id,)).fetchone()
        return self._event_row(row) if row else None

    def list_events(self, run_id: str, *, after: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT * FROM agent_events WHERE run_id=? AND sequence>?
                   ORDER BY sequence LIMIT ?""",
                (run_id, max(0, int(after)), max(1, min(int(limit), 5000))),
            ).fetchall()
        return [self._event_row(row) for row in rows]

    def summarize_trace(self, run_id: str) -> dict[str, Any]:
        """Aggregate model/tool observability without double-counting started events."""
        events = self.list_events(run_id, after=0, limit=5000)
        run = self.get_run(run_id) or {}
        model_events = [
            item for item in events
            if item.get("event_type") in {"model.completed", "model.failed", "model.cancelled", "model.interrupted"}
        ]
        tool_events = [
            item for item in events
            if item.get("event_type") in {"tool.completed", "tool.failed", "tool.cancelled", "tool.interrupted"}
        ]
        retry_events = [item for item in events if str(item.get("event_type") or "").endswith(".retry")]
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        estimated_calls = 0
        for item in model_events:
            usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
            prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            total = int(usage.get("total_tokens") or (prompt + completion))
            prompt_tokens += prompt
            completion_tokens += completion
            total_tokens += total
            if usage.get("estimated"):
                estimated_calls += 1
        wall_time_ms = 0.0
        try:
            created = datetime.fromisoformat(str(run.get("created_at") or ""))
            updated = datetime.fromisoformat(str(run.get("updated_at") or ""))
            wall_time_ms = round(max(0.0, (updated - created).total_seconds() * 1000), 2)
        except (TypeError, ValueError):
            pass
        return {
            "run_id": run_id,
            "status": str(run.get("status") or ""),
            "current_state": str(run.get("current_state") or ""),
            "event_count": len(events),
            "wall_time_ms": wall_time_ms,
            "model_calls": len(model_events),
            "model_failures": sum(1 for item in model_events if item.get("event_type") == "model.failed"),
            "model_cancellations": sum(1 for item in model_events if item.get("event_type") == "model.cancelled"),
            "model_interruptions": sum(1 for item in model_events if item.get("event_type") == "model.interrupted"),
            "model_duration_ms": round(sum(float(item.get("duration_ms") or 0) for item in model_events), 2),
            "tool_calls": len(tool_events),
            "tool_failures": sum(1 for item in tool_events if item.get("event_type") == "tool.failed"),
            "tool_cancellations": sum(1 for item in tool_events if item.get("event_type") == "tool.cancelled"),
            "tool_interruptions": sum(1 for item in tool_events if item.get("event_type") == "tool.interrupted"),
            "tool_duration_ms": round(sum(float(item.get("duration_ms") or 0) for item in tool_events), 2),
            "retry_count": len(retry_events),
            "token_usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "estimated_calls": estimated_calls,
                "contains_estimates": estimated_calls > 0,
            },
        }

    def create_approval(self, *, run_id: str, revision_id: str, page_id: str, title: str = "") -> dict[str, Any]:
        existing = self.get_approval_by_revision(revision_id)
        if existing:
            return existing
        approval_id = str(uuid.uuid4())
        with closing(self._connect()) as conn:
            conn.execute(
                """INSERT INTO agent_approvals
                   (id, run_id, revision_id, page_id, title, status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                (approval_id, run_id, revision_id, page_id, title, self.now_iso()),
            )
            conn.commit()
        self.append_event(
            run_id,
            event_type="approval.requested",
            node_name="AWAITING_APPROVAL",
            status="pending",
            input_data={"approval_id": approval_id, "revision_id": revision_id, "page_id": page_id},
        )
        return self.get_approval(approval_id) or {}

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM agent_approvals WHERE id=?", (approval_id,)).fetchone()
        return dict(row) if row else None

    def get_approval_by_revision(self, revision_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM agent_approvals WHERE revision_id=?", (revision_id,)).fetchone()
        return dict(row) if row else None

    def list_approvals(self, *, status: str = "pending", run_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status == "action_required":
            clauses.append("status IN ('pending', 'commit_failed')")
        elif status:
            clauses.append("status=?")
            params.append(status)
        if run_id:
            clauses.append("run_id=?")
            params.append(run_id)
        sql = "SELECT * FROM agent_approvals"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def transition_approval(
        self,
        approval_id: str,
        *,
        status: str,
        expected_statuses: set[str],
        reason: str = "",
        commit_error: str = "",
        superseded_by: str = "",
    ) -> dict[str, Any]:
        if not expected_statuses:
            raise ValueError("expected_statuses cannot be empty")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM agent_approvals WHERE id=?", (approval_id,)
            ).fetchone()
            if not row or str(row["status"]) not in expected_statuses:
                actual = str(row["status"]) if row else "missing"
                label = "pending approval" if expected_statuses == {"pending"} else "approval"
                raise ValueError(
                    f"Expected {label} in {sorted(expected_statuses)}, current status is {actual}."
                )
            approval = dict(row)
            final = status in {"approved", "rejected", "superseded"}
            cursor = conn.execute(
                """UPDATE agent_approvals
                   SET status=?, decision_reason=?, commit_error=?, superseded_by=?, decided_at=?
                   WHERE id=? AND status=?""",
                (
                    status, reason, str(commit_error or "")[:2000], superseded_by,
                    self.now_iso() if final or status == "accepted" else str(row["decided_at"] or ""),
                    approval_id, row["status"],
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Approval transition lost a concurrency race.")
            conn.commit()
        self.append_event(
            str(approval["run_id"]),
            event_type=f"approval.{status}",
            node_name="AWAITING_APPROVAL" if status in {"pending", "accepted"} else "COMMITTING",
            status=status,
            input_data={
                "approval_id": approval_id, "from": approval["status"], "reason": reason,
                "commit_error": str(commit_error or "")[:500], "superseded_by": superseded_by,
            },
        )
        return self.get_approval(approval_id) or {}

    def accept_approval(self, approval_id: str, *, reason: str = "") -> dict[str, Any]:
        return self.transition_approval(
            approval_id, status="accepted", expected_statuses={"pending"}, reason=reason
        )

    def mark_approval_committing(self, approval_id: str) -> dict[str, Any]:
        return self.transition_approval(
            approval_id, status="committing",
            expected_statuses={"accepted", "committing", "commit_failed"},
            reason="commit started or resumed",
        )

    def complete_approval(self, approval_id: str) -> dict[str, Any]:
        return self.transition_approval(
            approval_id, status="approved",
            expected_statuses={"accepted", "committing", "commit_failed"},
            reason="revision committed",
        )

    def fail_approval(self, approval_id: str, error: str) -> dict[str, Any]:
        return self.transition_approval(
            approval_id, status="commit_failed",
            expected_statuses={"accepted", "committing", "commit_failed"},
            reason="revision commit failed", commit_error=error,
        )

    def decide_approval(
        self,
        approval_id: str,
        *,
        status: str,
        reason: str = "",
        superseded_by: str = "",
    ) -> dict[str, Any]:
        if status not in {"approved", "rejected", "superseded"}:
            raise ValueError(f"Unsupported approval decision: {status}")
        return self.transition_approval(
            approval_id, status=status, expected_statuses={"pending"},
            reason=reason, superseded_by=superseded_by,
        )

    def _run_row(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["context"] = self.load_json(item.pop("context_json", "{}"), {})
        item["result"] = self.load_json(item.pop("result_json", "{}"), {})
        return item

    def _event_row(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["input"] = self.load_json(item.pop("input_json", "{}"), {})
        item["output"] = self.load_json(item.pop("output_json", "{}"), {})
        item["usage"] = self.load_json(item.pop("usage_json", "{}"), {})
        return item

    @staticmethod
    def _status_for_state(state: str) -> str:
        if state == "QUEUED":
            return "queued"
        if state in WAITING_STATES:
            return "waiting"
        if state == "COMPLETED":
            return "completed"
        if state in {"REJECTED", "FAILED", "CANCELLED"}:
            return state.lower()
        return "running"

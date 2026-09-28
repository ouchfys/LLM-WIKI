from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from system.storage.layout import resolve_database_path
from typing import Any


class IngestionJobStore:
    def __init__(self, db_path: str = None):
        path = resolve_database_path(db_path)
        self.db_path = str(path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def dump_json(data: Any) -> str:
        return json.dumps(data if data is not None else {}, ensure_ascii=False)

    @staticmethod
    def load_json(data: str) -> Any:
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
                CREATE TABLE IF NOT EXISTS ingestion_jobs (
                    id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    source_uri TEXT DEFAULT '',
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress REAL DEFAULT 0,
                    error TEXT DEFAULT '',
                    source_packet_id TEXT DEFAULT '',
                    paper_card_id TEXT DEFAULT '',
                    result_json TEXT DEFAULT '{}',
                    metadata_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_status ON ingestion_jobs(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_updated ON ingestion_jobs(updated_at)")
            conn.commit()

    def create_job(
        self,
        *,
        source_type: str,
        source_uri: str,
        stage: str = "queued",
        metadata: dict[str, Any] | None = None,
        job_id: str = "",
        connection=None,
    ) -> dict[str, Any]:
        job_id = job_id or str(uuid.uuid4())
        now = self.now_iso()
        with (nullcontext(connection) if connection is not None else closing(self._connect())) as conn:
            conn.execute(
                """
                INSERT INTO ingestion_jobs
                (id, source_type, source_uri, status, stage, progress, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, 'queued', ?, 0, ?, ?, ?)
                """,
                (job_id, source_type, source_uri, stage, self.dump_json(metadata or {}), now, now),
            )
            if connection is None:
                conn.commit()
        if connection is not None:
            return self._row(conn.execute("SELECT * FROM ingestion_jobs WHERE id=?", (job_id,)).fetchone())
        return self.get_job(job_id) or {}

    @staticmethod
    def validate_owner(conn, session_id, run_id, call_id):
        if not session_id:
            return
        row = conn.execute("""SELECT 1 FROM sessions s JOIN agent_runs r ON r.id=?
            JOIN chat_tool_calls c ON c.id=? AND c.run_id=r.id
            WHERE s.id=? AND r.source_uri=? AND r.current_state='CHAT_RUNNING'
            AND r.cancel_requested=0 AND c.status='started' AND c.tool='arxiv_import_paper'""",
            (run_id, call_id, session_id, f"session:{session_id}")).fetchone()
        if not row:
            raise ValueError("Owning chat is no longer active")

    @classmethod
    def attach_owner(cls, conn, job_id, session_id, run_id, call_id):
        if not session_id:
            return
        cls.validate_owner(conn, session_id, run_id, call_id)
        row = conn.execute("SELECT metadata_json FROM ingestion_jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return
        metadata = cls.load_json(row[0])
        if "owner_session_ids" not in metadata:
            metadata["independent"] = True  # Legacy/UI-created work is not chat-owned.
        metadata["owner_session_ids"] = list(dict.fromkeys([*(metadata.get("owner_session_ids") or []), session_id]))
        conn.execute("UPDATE ingestion_jobs SET metadata_json=? WHERE id=?", (cls.dump_json(metadata), job_id))
        conn.execute("UPDATE chat_tool_calls SET job_id=? WHERE id=? AND run_id=?", (job_id, call_id, run_id))

    def update_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        stage: str | None = None,
        progress: float | None = None,
        error: str | None = None,
        source_packet_id: str | None = None,
        paper_card_id: str | None = None,
        result: Any = None,
        metadata: Any = None,
    ) -> None:
        fields: list[str] = []
        params: list[Any] = []
        if status is not None:
            fields.append("status = ?")
            params.append(status)
        if stage is not None:
            fields.append("stage = ?")
            params.append(stage)
        if progress is not None:
            fields.append("progress = ?")
            params.append(max(0.0, min(float(progress), 1.0)))
        if error is not None:
            fields.append("error = ?")
            params.append(error)
        if source_packet_id is not None:
            fields.append("source_packet_id = ?")
            params.append(source_packet_id)
        if paper_card_id is not None:
            fields.append("paper_card_id = ?")
            params.append(paper_card_id)
        if result is not None:
            fields.append("result_json = ?")
            params.append(self.dump_json(result))
        if metadata is not None:
            fields.append("metadata_json = ?")
            params.append(self.dump_json(metadata))
        if not fields:
            return
        fields.append("updated_at = ?")
        params.append(self.now_iso())
        params.append(job_id)
        with closing(self._connect()) as conn:
            # Late pipeline callbacks cannot resurrect a cancelled job.
            conn.execute(f"UPDATE ingestion_jobs SET {', '.join(fields)} WHERE id = ? AND status<>'cancelled'", params)
            conn.commit()

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM ingestion_jobs WHERE id = ? LIMIT 1", (job_id,)).fetchone()
        return self._row(row) if row else None

    def merge_metadata(self, job_id: str, values: dict[str, Any]) -> None:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT metadata_json FROM ingestion_jobs WHERE id=? AND status<>'cancelled'", (job_id,)).fetchone()
            if row:
                metadata = self.load_json(row[0])
                metadata.update(values or {})
                conn.execute("UPDATE ingestion_jobs SET metadata_json=? WHERE id=?", (self.dump_json(metadata), job_id))
            conn.commit()

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM ingestion_jobs ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [self._row(row) for row in rows]

    def _row(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["result"] = self.load_json(item.pop("result_json", "{}"))
        item["metadata"] = self.load_json(item.pop("metadata_json", "{}"))
        return item

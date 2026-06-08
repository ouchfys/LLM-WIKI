from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class WikiMaintenanceStore:
    """SQLite store for deterministic maintenance runs.

    This store is deliberately small. It records validation reports and repair
    task shells; the existing ingestion/merge tables remain the source of truth
    for official wiki state.
    """

    def __init__(self, db_path: str | Path | None = None):
        repo_root = Path(__file__).resolve().parents[3]
        self.db_path = str(Path(db_path) if db_path else repo_root / "sessions.db")
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
                CREATE TABLE IF NOT EXISTS wiki_validation_runs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    error_count INTEGER DEFAULT 0,
                    warning_count INTEGER DEFAULT 0,
                    report_json TEXT NOT NULL,
                    artifact_uri TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_wiki_validation_runs_created "
                "ON wiki_validation_runs(created_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wiki_repair_tasks (
                    id TEXT PRIMARY KEY,
                    validation_run_id TEXT DEFAULT '',
                    task_type TEXT NOT NULL,
                    target_entity_type TEXT DEFAULT '',
                    target_entity_id TEXT DEFAULT '',
                    repair_target TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER DEFAULT 0,
                    payload_json TEXT NOT NULL,
                    result_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_wiki_repair_tasks_status "
                "ON wiki_repair_tasks(status)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wiki_query_insights (
                    id TEXT PRIMARY KEY,
                    session_id TEXT DEFAULT '',
                    message_id TEXT DEFAULT '',
                    question TEXT NOT NULL,
                    answer_excerpt TEXT DEFAULT '',
                    insight_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    candidate_id TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS wiki_maintenance_candidates (
                    id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    source_id TEXT DEFAULT '',
                    candidate_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    artifact_uri TEXT DEFAULT '',
                    review_result_json TEXT DEFAULT '{}',
                    merge_result_json TEXT DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_wiki_maintenance_candidates_status "
                "ON wiki_maintenance_candidates(status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_wiki_maintenance_candidates_source "
                "ON wiki_maintenance_candidates(source_type, source_id)"
            )
            conn.commit()

    def add_validation_run(
        self,
        *,
        report: dict[str, Any],
        artifact_uri: str = "",
    ) -> str:
        run_id = str(report.get("run_id") or uuid.uuid4())
        report["run_id"] = run_id
        status = "passed" if report.get("ok") else "failed"
        errors = report.get("errors") or []
        warnings = report.get("warnings") or []
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO wiki_validation_runs
                (id, status, error_count, warning_count, report_json, artifact_uri, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    status,
                    len(errors),
                    len(warnings),
                    self.dump_json(report),
                    artifact_uri,
                    self.now_iso(),
                ),
            )
            conn.commit()
        return run_id

    def list_validation_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM wiki_validation_runs
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._validation_row(row) for row in rows]

    def get_validation_run(self, run_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM wiki_validation_runs WHERE id = ? LIMIT 1",
                (run_id,),
            ).fetchone()
        return self._validation_row(row) if row else None

    def add_repair_task(
        self,
        *,
        validation_run_id: str,
        task_type: str,
        repair_target: str,
        payload: dict[str, Any],
        target_entity_type: str = "",
        target_entity_id: str = "",
        status: str = "pending",
    ) -> str:
        now = self.now_iso()
        task_id = str(uuid.uuid4())
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO wiki_repair_tasks
                (id, validation_run_id, task_type, target_entity_type, target_entity_id,
                 repair_target, status, attempts, payload_json, result_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, '{}', ?, ?)
                """,
                (
                    task_id,
                    validation_run_id,
                    task_type,
                    target_entity_type,
                    target_entity_id,
                    repair_target,
                    status,
                    self.dump_json(payload),
                    now,
                    now,
                ),
            )
            conn.commit()
        return task_id

    def list_repair_tasks(self, status: str = "", limit: int = 100) -> list[dict[str, Any]]:
        where = ""
        params: list[Any] = []
        if status:
            where = "WHERE status = ?"
            params.append(status)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM wiki_repair_tasks
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params + [limit],
            ).fetchall()
        return [self._repair_row(row) for row in rows]

    def update_repair_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        attempts: int | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        fields: list[str] = []
        params: list[Any] = []
        if status is not None:
            fields.append("status = ?")
            params.append(status)
        if attempts is not None:
            fields.append("attempts = ?")
            params.append(attempts)
        if result is not None:
            fields.append("result_json = ?")
            params.append(self.dump_json(result))
        if not fields:
            return
        fields.append("updated_at = ?")
        params.append(self.now_iso())
        params.append(task_id)
        with closing(self._connect()) as conn:
            conn.execute(
                f"UPDATE wiki_repair_tasks SET {', '.join(fields)} WHERE id = ?",
                params,
            )
            conn.commit()

    def add_query_insight(
        self,
        *,
        question: str,
        insight: dict[str, Any],
        status: str = "archived",
        session_id: str = "",
        message_id: str = "",
        answer_excerpt: str = "",
        candidate_id: str = "",
    ) -> str:
        insight_id = str(uuid.uuid4())
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO wiki_query_insights
                (id, session_id, message_id, question, answer_excerpt, insight_json,
                 status, candidate_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    insight_id,
                    session_id,
                    message_id,
                    question,
                    answer_excerpt,
                    self.dump_json(insight),
                    status,
                    candidate_id,
                    self.now_iso(),
                ),
            )
            conn.commit()
        return insight_id

    def list_query_insights(self, status: str = "", limit: int = 100) -> list[dict[str, Any]]:
        where = ""
        params: list[Any] = []
        if status:
            where = "WHERE status = ?"
            params.append(status)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM wiki_query_insights
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params + [limit],
            ).fetchall()
        return [self._query_insight_row(row) for row in rows]

    def get_query_insight(self, insight_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM wiki_query_insights WHERE id = ? LIMIT 1",
                (insight_id,),
            ).fetchone()
        return self._query_insight_row(row) if row else None

    def update_query_insight(
        self,
        insight_id: str,
        *,
        status: str | None = None,
        insight: dict[str, Any] | None = None,
        candidate_id: str | None = None,
    ) -> None:
        fields: list[str] = []
        params: list[Any] = []
        if status is not None:
            fields.append("status = ?")
            params.append(status)
        if insight is not None:
            fields.append("insight_json = ?")
            params.append(self.dump_json(insight))
        if candidate_id is not None:
            fields.append("candidate_id = ?")
            params.append(candidate_id)
        if not fields:
            return
        params.append(insight_id)
        with closing(self._connect()) as conn:
            conn.execute(
                f"UPDATE wiki_query_insights SET {', '.join(fields)} WHERE id = ?",
                params,
            )
            conn.commit()

    def add_candidate(
        self,
        *,
        source_type: str,
        candidate_type: str,
        title: str,
        payload: dict[str, Any],
        source_id: str = "",
        status: str = "candidate_ready",
        artifact_uri: str = "",
    ) -> str:
        candidate_id = str(payload.get("candidate_id") or uuid.uuid4())
        payload["candidate_id"] = candidate_id
        now = self.now_iso()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO wiki_maintenance_candidates
                (id, source_type, source_id, candidate_type, title, status,
                 payload_json, artifact_uri, review_result_json, merge_result_json,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', '{}', ?, ?)
                """,
                (
                    candidate_id,
                    source_type,
                    source_id,
                    candidate_type,
                    title,
                    status,
                    self.dump_json(payload),
                    artifact_uri,
                    now,
                    now,
                ),
            )
            conn.commit()
        return candidate_id

    def get_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM wiki_maintenance_candidates WHERE id = ? LIMIT 1",
                (candidate_id,),
            ).fetchone()
        return self._candidate_row(row) if row else None

    def list_candidates(
        self,
        *,
        status: str = "",
        source_type: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("status = ?")
            params.append(status)
        if source_type:
            where.append("source_type = ?")
            params.append(source_type)
        where_sql = "WHERE " + " AND ".join(where) if where else ""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM wiki_maintenance_candidates
                {where_sql}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params + [limit],
            ).fetchall()
        return [self._candidate_row(row) for row in rows]

    def update_candidate(
        self,
        candidate_id: str,
        *,
        status: str | None = None,
        payload: dict[str, Any] | None = None,
        artifact_uri: str | None = None,
        review_result: dict[str, Any] | None = None,
        merge_result: dict[str, Any] | None = None,
    ) -> None:
        fields: list[str] = []
        params: list[Any] = []
        if status is not None:
            fields.append("status = ?")
            params.append(status)
        if payload is not None:
            fields.append("payload_json = ?")
            params.append(self.dump_json(payload))
        if artifact_uri is not None:
            fields.append("artifact_uri = ?")
            params.append(artifact_uri)
        if review_result is not None:
            fields.append("review_result_json = ?")
            params.append(self.dump_json(review_result))
        if merge_result is not None:
            fields.append("merge_result_json = ?")
            params.append(self.dump_json(merge_result))
        if not fields:
            return
        fields.append("updated_at = ?")
        params.append(self.now_iso())
        params.append(candidate_id)
        with closing(self._connect()) as conn:
            conn.execute(
                f"UPDATE wiki_maintenance_candidates SET {', '.join(fields)} WHERE id = ?",
                params,
            )
            conn.commit()

    def create_repair_tasks_from_report(self, report: dict[str, Any]) -> list[str]:
        run_id = str(report.get("run_id") or "")
        task_ids: list[str] = []
        for item in report.get("errors", []) or []:
            if not isinstance(item, dict):
                continue
            repair_target = str(item.get("repair_target") or "")
            if not repair_target or repair_target == "none":
                continue
            task_ids.append(
                self.add_repair_task(
                    validation_run_id=run_id,
                    task_type=str(item.get("error_type") or "validation_error"),
                    repair_target=repair_target,
                    target_entity_type=str(item.get("entity_type") or ""),
                    target_entity_id=str(item.get("entity_id") or ""),
                    payload=item,
                )
            )
        return task_ids

    def _validation_row(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["report"] = self.load_json(item.pop("report_json", "{}"))
        return item

    def _repair_row(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = self.load_json(item.pop("payload_json", "{}"))
        item["result"] = self.load_json(item.pop("result_json", "{}"))
        return item

    def _query_insight_row(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["insight"] = self.load_json(item.pop("insight_json", "{}"))
        return item

    def _candidate_row(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = self.load_json(item.pop("payload_json", "{}"))
        item["review_result"] = self.load_json(item.pop("review_result_json", "{}"))
        item["merge_result"] = self.load_json(item.pop("merge_result_json", "{}"))
        return item

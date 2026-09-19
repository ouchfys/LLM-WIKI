"""Durable, structured progress ledger for long-horizon research tasks."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


TERMINAL_JOB_STATES = {"done", "completed", "failed", "rejected", "cancelled"}
SUCCESS_JOB_STATES = {"done", "completed"}
VALID_PHASES = {
    "DISCOVER", "WAIT_INGEST", "VERIFY_CORPUS", "READ_LOCAL_CORPUS",
    "SYNTHESIZE", "COMPLETE", "BUDGET_EXHAUSTED", "FAILED",
}


class ResearchTaskLedgerStore:
    """Persist facts that must survive chat turns and context compaction.

    Conversation summaries remain useful prose, but they are never the source
    of truth for paper IDs, ingestion jobs, opened cards, coverage, or budgets.
    """

    def __init__(self, db_path: str | None = None):
        repo_root = Path(__file__).resolve().parents[2]
        self.db_path = str(Path(db_path) if db_path else repo_root / "sessions.db")
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def _load(value: str, default: Any) -> Any:
        try:
            return json.loads(value) if value else default
        except json.JSONDecodeError:
            return default

    @staticmethod
    def _unique(values: Iterable[Any]) -> list[str]:
        return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_task_ledgers (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    task_key TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    phase TEXT NOT NULL DEFAULT 'DISCOVER',
                    target_papers INTEGER NOT NULL DEFAULT 0,
                    topic_minimum INTEGER NOT NULL DEFAULT 1,
                    required_topics_json TEXT NOT NULL DEFAULT '{}',
                    selected_paper_ids_json TEXT NOT NULL DEFAULT '[]',
                    ingestion_jobs_json TEXT NOT NULL DEFAULT '{}',
                    verified_card_ids_json TEXT NOT NULL DEFAULT '[]',
                    opened_card_ids_json TEXT NOT NULL DEFAULT '[]',
                    coverage_json TEXT NOT NULL DEFAULT '{}',
                    remaining_requirements_json TEXT NOT NULL DEFAULT '[]',
                    budget_json TEXT NOT NULL DEFAULT '{}',
                    usage_json TEXT NOT NULL DEFAULT '{}',
                    deliverable_type TEXT NOT NULL DEFAULT 'research_report',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id, task_key)
                );
                CREATE INDEX IF NOT EXISTS idx_research_tasks_session
                    ON research_task_ledgers(session_id, updated_at);
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(research_task_ledgers)")}
            if "topic_minimum" not in columns:
                conn.execute(
                    "ALTER TABLE research_task_ledgers ADD COLUMN topic_minimum INTEGER NOT NULL DEFAULT 1"
                )
            conn.commit()

    def ensure_task(
        self,
        *,
        project_id: str,
        session_id: str,
        task_key: str,
        title: str = "",
        target_papers: int = 0,
        topic_minimum: int = 1,
        required_topics: dict[str, list[str]] | None = None,
        budget: dict[str, Any] | None = None,
        deliverable_type: str = "research_report",
        initial_card_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        project_id = str(project_id or session_id or "default")
        session_id = str(session_id or "")
        task_key = str(task_key or "research").strip()
        now = self._now()
        task_id = str(uuid.uuid4())
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM research_task_ledgers WHERE project_id=? AND task_key=?",
                (project_id, task_key),
            ).fetchone()
            if row:
                conn.execute(
                    """UPDATE research_task_ledgers
                       SET session_id=?, title=?, target_papers=?, topic_minimum=?,
                           required_topics_json=?, budget_json=?, deliverable_type=?, updated_at=?
                       WHERE id=?""",
                    (
                        session_id, title, max(0, int(target_papers)), max(1, int(topic_minimum)),
                        self._dump(required_topics or {}), self._dump(budget or {}),
                        str(deliverable_type or "research_report"), now, row["id"],
                    ),
                )
                conn.commit()
                return self.get_task(str(row["id"])) or {}
            conn.execute(
                """INSERT INTO research_task_ledgers
                   (id,project_id,session_id,task_key,title,phase,target_papers,topic_minimum,
                    required_topics_json,verified_card_ids_json,budget_json,
                    deliverable_type,created_at,updated_at)
                   VALUES (?,?,?,?,?,'DISCOVER',?,?,?,?,?,?,?,?)""",
                (
                    task_id, project_id, session_id, task_key, title,
                    max(0, int(target_papers)), max(1, int(topic_minimum)), self._dump(required_topics or {}),
                    self._dump(self._unique(initial_card_ids)), self._dump(budget or {}),
                    str(deliverable_type or "research_report"), now, now,
                ),
            )
            conn.commit()
        return self.get_task(task_id) or {}

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM research_task_ledgers WHERE id=?", (str(task_id),)
            ).fetchone()
        return self._row(row) if row else None

    def get_active_for_session(self, session_id: str) -> dict[str, Any] | None:
        if not session_id:
            return None
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT * FROM research_task_ledgers
                   WHERE session_id=? AND phase NOT IN ('COMPLETE','FAILED')
                   ORDER BY updated_at DESC LIMIT 1""",
                (str(session_id),),
            ).fetchone()
        return self._row(row) if row else None

    def get_active_for_project(self, project_id: str) -> dict[str, Any] | None:
        if not project_id:
            return None
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT * FROM research_task_ledgers
                   WHERE project_id=? AND phase NOT IN ('COMPLETE','FAILED')
                   ORDER BY updated_at DESC LIMIT 1""",
                (str(project_id),),
            ).fetchone()
        return self._row(row) if row else None

    def attach_session(self, task_id: str, session_id: str) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                "UPDATE research_task_ledgers SET session_id=?, updated_at=? WHERE id=?",
                (str(session_id), self._now(), str(task_id)),
            )
            conn.commit()
        if not cursor.rowcount:
            raise KeyError(task_id)
        return self.get_task(task_id) or {}

    def record_tool_observation(
        self,
        task_id: str,
        *,
        tool_name: str,
        status: str,
        items: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not task:
            raise KeyError(task_id)
        selected = list(task["selected_paper_ids"])
        jobs = dict(task["ingestion_jobs"])
        verified = list(task["verified_card_ids"])
        opened = list(task["opened_card_ids"])
        usage = dict(task["usage"])
        usage["tool_calls"] = int(usage.get("tool_calls") or 0) + 1
        if tool_name == "web_search":
            usage["web_calls"] = int(usage.get("web_calls") or 0) + 1
        if tool_name in {"web_search", "resource_recommend", "arxiv_search"}:
            usage["discovery_calls"] = int(usage.get("discovery_calls") or 0) + 1
        rows = [item for item in items if isinstance(item, dict)]
        if status == "done" and tool_name == "arxiv_import_paper":
            for item in rows:
                arxiv_id = str(item.get("arxiv_id") or "").strip()
                job_id = str(item.get("job_id") or "").strip()
                card_id = str(item.get("paper_card_id") or "").strip()
                if arxiv_id:
                    selected.append(arxiv_id)
                if job_id:
                    jobs[job_id] = {"status": "submitted", "arxiv_id": arxiv_id, "paper_card_id": card_id}
                if card_id:
                    verified.append(card_id)
            usage["paper_imports"] = int(usage.get("paper_imports") or 0) + len(rows)
        elif status == "done" and tool_name == "arxiv_ingestion_status":
            for item in rows:
                job_id = str(item.get("id") or item.get("job_id") or "").strip()
                job_status = str(item.get("status") or "unknown").strip().lower()
                card_id = str(item.get("paper_card_id") or "").strip()
                current = dict(jobs.get(job_id) or {})
                current.update({"status": job_status, "paper_card_id": card_id})
                if job_id:
                    jobs[job_id] = current
                if card_id and job_status in SUCCESS_JOB_STATES:
                    verified.append(card_id)
        elif status == "done" and tool_name in {"wiki_open", "wiki_card", "workspace_read"}:
            for item in rows:
                card_id = str(item.get("card_id") or item.get("id") or "").strip()
                if card_id:
                    opened.append(card_id)
        return self._update(
            task_id,
            selected_paper_ids=self._unique(selected),
            ingestion_jobs=jobs,
            verified_card_ids=self._unique(verified),
            opened_card_ids=self._unique(opened),
            usage=usage,
        )

    def reconcile(self, task_id: str, cards: Iterable[dict[str, Any]]) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not task:
            raise KeyError(task_id)
        by_id = {
            str(card.get("id") or card.get("card_id") or ""): card
            for card in cards if isinstance(card, dict)
        }
        # Corpus membership is explicit. Reconciliation verifies that known IDs
        # still exist, but never sweeps unrelated project pages into this task.
        verified = self._unique(
            card_id for card_id in task["verified_card_ids"] if card_id in by_id
        )
        coverage: dict[str, list[str]] = {}
        for topic, aliases in task["required_topics"].items():
            matched: list[str] = []
            for card_id in verified:
                card = by_id[card_id]
                haystack = self._dump({
                    "title": card.get("title"), "summary": card.get("summary"),
                    "topics": card.get("related_topics"), "content": card.get("content_json"),
                }).lower()
                if any(str(alias).lower() in haystack for alias in aliases):
                    matched.append(card_id)
            coverage[str(topic)] = self._unique(matched)
        pending_jobs = [
            job_id for job_id, value in task["ingestion_jobs"].items()
            if str((value or {}).get("status") or "submitted").lower() not in TERMINAL_JOB_STATES
        ]
        topic_minimum = max(1, int(task.get("topic_minimum") or 1))
        missing_topics = [topic for topic, ids in coverage.items() if len(ids) < topic_minimum]
        missing_count = max(0, int(task["target_papers"]) - len(verified))
        unopened = [card_id for card_id in verified if card_id not in set(task["opened_card_ids"])]
        remaining: list[str] = []
        if missing_count:
            remaining.append(f"need {missing_count} more verified PaperPage(s)")
        if missing_topics:
            remaining.append(
                "topic coverage below minimum " + str(topic_minimum) + ": " + ", ".join(missing_topics)
            )
        if unopened:
            remaining.append(f"need to open {len(unopened)} verified Wiki card(s)")
        if task["phase"] in {"COMPLETE", "FAILED", "BUDGET_EXHAUSTED"}:
            phase = task["phase"]
        elif pending_jobs:
            phase = "WAIT_INGEST"
        elif missing_count or missing_topics:
            phase = "DISCOVER"
        elif unopened:
            phase = "READ_LOCAL_CORPUS"
        else:
            phase = "SYNTHESIZE"
        return self._update(
            task_id,
            phase=phase,
            verified_card_ids=verified,
            coverage=coverage,
            remaining_requirements=remaining,
        )

    def mark_phase(self, task_id: str, phase: str) -> dict[str, Any]:
        phase = str(phase).upper()
        if phase not in VALID_PHASES:
            raise ValueError(f"Unsupported research phase: {phase}")
        return self._update(task_id, phase=phase)

    def _update(self, task_id: str, **updates: Any) -> dict[str, Any]:
        columns = {
            "phase": "phase", "selected_paper_ids": "selected_paper_ids_json",
            "ingestion_jobs": "ingestion_jobs_json", "verified_card_ids": "verified_card_ids_json",
            "opened_card_ids": "opened_card_ids_json", "coverage": "coverage_json",
            "remaining_requirements": "remaining_requirements_json", "budget": "budget_json",
            "usage": "usage_json",
        }
        assignments: list[str] = []
        params: list[Any] = []
        for key, value in updates.items():
            column = columns.get(key)
            if not column:
                continue
            assignments.append(f"{column}=?")
            params.append(value if key == "phase" else self._dump(value))
        if not assignments:
            return self.get_task(task_id) or {}
        assignments.append("updated_at=?")
        params.extend([self._now(), str(task_id)])
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                f"UPDATE research_task_ledgers SET {', '.join(assignments)} WHERE id=?", params
            )
            conn.commit()
        if not cursor.rowcount:
            raise KeyError(task_id)
        return self.get_task(task_id) or {}

    def _row(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        for key, default in (
            ("required_topics", {}), ("selected_paper_ids", []), ("ingestion_jobs", {}),
            ("verified_card_ids", []), ("opened_card_ids", []), ("coverage", {}),
            ("remaining_requirements", []), ("budget", {}), ("usage", {}),
        ):
            item[key] = self._load(item.pop(f"{key}_json"), default)
        item["verified_count"] = len(item["verified_card_ids"])
        item["opened_count"] = len(item["opened_card_ids"])
        item["corpus_ready"] = (
            item["verified_count"] >= int(item["target_papers"])
            and all(
                len(item["coverage"].get(topic) or []) >= max(1, int(item.get("topic_minimum") or 1))
                for topic in item["required_topics"]
            )
        )
        return item

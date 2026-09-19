"""Durable state and deterministic scheduling for long-horizon research tasks."""

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
    "PLANNING", "DISCOVER", "WAIT_INGEST", "VERIFY_CORPUS", "BUILD_QUEUE",
    "READING", "CHECK_GATES", "TARGETED_DISCOVERY", "SYNTHESIZE", "PAUSED",
    "COMPLETE", "BUDGET_EXHAUSTED", "FAILED", "READ_LOCAL_CORPUS",
}
READING_NOVELTY = {"new", "supporting", "duplicate", "irrelevant", "uncertain"}


class ResearchTaskLedgerStore:
    """Keep plans, batches and reading receipts outside model context."""

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
    def _load(value: str | None, default: Any) -> Any:
        try:
            return json.loads(value) if value else default
        except (json.JSONDecodeError, TypeError):
            return default

    @staticmethod
    def _unique(values: Iterable[Any]) -> list[str]:
        return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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
                    phase TEXT NOT NULL DEFAULT 'PLANNING',
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
                    plan_json TEXT NOT NULL DEFAULT '{}',
                    gate_status_json TEXT NOT NULL DEFAULT '{}',
                    deliverable_type TEXT NOT NULL DEFAULT 'research_report',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id, task_key)
                );
                CREATE INDEX IF NOT EXISTS idx_research_tasks_session
                    ON research_task_ledgers(session_id, updated_at);
                CREATE TABLE IF NOT EXISTS research_plans (
                    task_id TEXT PRIMARY KEY,
                    plan_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reading_batches (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ASSIGNED',
                    assigned_card_ids_json TEXT NOT NULL DEFAULT '[]',
                    purposes_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(task_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_reading_batches_task
                    ON reading_batches(task_id, status, sequence);
                CREATE TABLE IF NOT EXISTS paper_readings (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    batch_id TEXT NOT NULL,
                    card_id TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'VERIFIED',
                    receipt_json TEXT NOT NULL,
                    prompt_version TEXT NOT NULL DEFAULT 'research-reading-v1',
                    reopen_reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    verified_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(task_id, card_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_paper_readings_task
                    ON paper_readings(task_id, status, card_id);
                """
            )
            self._ensure_column(conn, "research_task_ledgers", "topic_minimum", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(conn, "research_task_ledgers", "plan_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(conn, "research_task_ledgers", "gate_status_json", "TEXT NOT NULL DEFAULT '{}'")
            conn.commit()

    @staticmethod
    def _default_plan(
        *, title: str, target_papers: int, topic_minimum: int,
        required_topics: dict[str, list[str]], budget: dict[str, Any], deliverable_type: str,
    ) -> dict[str, Any]:
        default_max = max(target_papers + max(4, target_papers // 5), 30) if target_papers else 30
        max_papers = int(budget.get("max_papers") or default_max)
        return {
            "research_questions": [title] if title else [],
            "dimensions": [
                {"id": str(topic), "minimum_sources": max(1, int(topic_minimum))}
                for topic in required_topics
            ],
            "selection_policy": {"batch_size": 4, "prioritize_coverage_gaps": True},
            "stop_policy": {
                "saturation_batches": 1 if 0 < target_papers <= 4 else 2,
                "max_papers": max_papers,
                "max_tool_calls": int(budget.get("max_tool_calls") or max(30, max_papers * 4)),
            },
            "deliverable_requirements": [deliverable_type] if deliverable_type else [],
        }

    def ensure_task(
        self, *, project_id: str, session_id: str, task_key: str, title: str = "",
        target_papers: int = 0, topic_minimum: int = 1,
        required_topics: dict[str, list[str]] | None = None,
        budget: dict[str, Any] | None = None, deliverable_type: str = "research_report",
        initial_card_ids: Iterable[str] = (), plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        project_id = str(project_id or session_id or "default")
        session_id = str(session_id or "")
        task_key = str(task_key or "research").strip()
        required_topics = dict(required_topics or {})
        budget = dict(budget or {})
        now = self._now()
        task_id = str(uuid.uuid4())
        normalized_plan = dict(plan or self._default_plan(
            title=title, target_papers=target_papers, topic_minimum=topic_minimum,
            required_topics=required_topics, budget=budget, deliverable_type=deliverable_type,
        ))
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM research_task_ledgers WHERE project_id=? AND task_key=?",
                (project_id, task_key),
            ).fetchone()
            if row:
                task_id = str(row["id"])
                conn.execute(
                    """UPDATE research_task_ledgers
                       SET session_id=?, title=?, target_papers=?, topic_minimum=?,
                           required_topics_json=?, budget_json=?, plan_json=?,
                           deliverable_type=?, updated_at=? WHERE id=?""",
                    (session_id, title, max(0, int(target_papers)), max(1, int(topic_minimum)),
                     self._dump(required_topics), self._dump(budget), self._dump(normalized_plan),
                     str(deliverable_type or "research_report"), now, task_id),
                )
            else:
                conn.execute(
                    """INSERT INTO research_task_ledgers
                       (id,project_id,session_id,task_key,title,phase,target_papers,topic_minimum,
                        required_topics_json,verified_card_ids_json,budget_json,plan_json,
                        deliverable_type,created_at,updated_at)
                       VALUES (?,?,?,?,?,'PLANNING',?,?,?,?,?,?,?,?,?)""",
                    (task_id, project_id, session_id, task_key, title,
                     max(0, int(target_papers)), max(1, int(topic_minimum)), self._dump(required_topics),
                     self._dump(self._unique(initial_card_ids)), self._dump(budget),
                     self._dump(normalized_plan), str(deliverable_type or "research_report"), now, now),
                )
            conn.execute(
                """INSERT INTO research_plans(task_id,plan_json,created_at,updated_at)
                   VALUES (?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET
                   plan_json=excluded.plan_json, updated_at=excluded.updated_at""",
                (task_id, self._dump(normalized_plan), now, now),
            )
            conn.commit()
        return self.get_task(task_id) or {}

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM research_task_ledgers WHERE id=?", (str(task_id),)).fetchone()
        return self._row(row) if row else None

    def get_active_for_session(self, session_id: str) -> dict[str, Any] | None:
        if not session_id:
            return None
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT * FROM research_task_ledgers
                   WHERE session_id=? AND phase NOT IN ('COMPLETE','FAILED')
                   ORDER BY updated_at DESC LIMIT 1""", (str(session_id),),
            ).fetchone()
        return self._row(row) if row else None

    def get_active_for_project(self, project_id: str) -> dict[str, Any] | None:
        if not project_id:
            return None
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT * FROM research_task_ledgers
                   WHERE project_id=? AND phase NOT IN ('COMPLETE','FAILED')
                   ORDER BY updated_at DESC LIMIT 1""", (str(project_id),),
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

    def get_plan(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not task:
            raise KeyError(task_id)
        return dict(task.get("plan") or {})

    def list_batches(self, task_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM reading_batches WHERE task_id=? ORDER BY sequence", (str(task_id),)
            ).fetchall()
        return [self._batch_row(row) for row in rows]

    def active_batch(self, task_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT * FROM reading_batches WHERE task_id=? AND status IN ('ASSIGNED','OPENED')
                   ORDER BY sequence LIMIT 1""", (str(task_id),),
            ).fetchone()
        return self._batch_row(row) if row else None

    def list_readings(self, task_id: str, *, status: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM paper_readings WHERE task_id=?"
        params: list[Any] = [str(task_id)]
        if status:
            sql += " AND status=?"
            params.append(str(status).upper())
        sql += " ORDER BY created_at, version"
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._reading_row(row) for row in rows]

    def latest_reading(self, task_id: str, card_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT * FROM paper_readings WHERE task_id=? AND card_id=? AND status='VERIFIED'
                   ORDER BY version DESC LIMIT 1""", (str(task_id), str(card_id)),
            ).fetchone()
        return self._reading_row(row) if row else None

    def completed_card_ids(self, task_id: str) -> list[str]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT card_id FROM paper_readings WHERE task_id=? AND status='VERIFIED'
                   GROUP BY card_id ORDER BY MIN(created_at)""", (str(task_id),),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def record_tool_observation(
        self, task_id: str, *, tool_name: str, status: str,
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
        if tool_name in {"web_search", "web_fetch"}:
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
            self._mark_active_batch_opened(task_id, opened)
        return self._update(
            task_id, selected_paper_ids=self._unique(selected), ingestion_jobs=jobs,
            verified_card_ids=self._unique(verified), opened_card_ids=self._unique(opened), usage=usage,
        )

    def _mark_active_batch_opened(self, task_id: str, opened: Iterable[str]) -> None:
        opened_set = set(self._unique(opened))
        batch = self.active_batch(task_id)
        if not batch or not opened_set.intersection(batch["assigned_card_ids"]):
            return
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE reading_batches SET status='OPENED', updated_at=? WHERE id=? AND status='ASSIGNED'",
                (self._now(), batch["id"]),
            )
            conn.commit()

    @staticmethod
    def _card_haystack(card: dict[str, Any]) -> str:
        return json.dumps({
            "title": card.get("title"), "summary": card.get("summary"),
            "topics": card.get("related_topics"), "content": card.get("content_json"),
        }, ensure_ascii=False, default=str).lower()

    def _corpus_topic_coverage(
        self, verified: list[str], by_id: dict[str, dict[str, Any]],
        required_topics: dict[str, list[str]],
    ) -> dict[str, list[str]]:
        coverage: dict[str, list[str]] = {}
        for topic, aliases in required_topics.items():
            coverage[str(topic)] = self._unique(
                card_id for card_id in verified
                if any(str(alias).lower() in self._card_haystack(by_id[card_id]) for alias in aliases)
            )
        return coverage

    def _reading_coverage(self, task_id: str, required_topics: dict[str, list[str]]) -> dict[str, list[str]]:
        coverage = {str(topic): [] for topic in required_topics}
        for reading in self.list_readings(task_id, status="VERIFIED"):
            for topic in reading["receipt"].get("covered_dimensions") or []:
                topic = str(topic)
                if topic in coverage:
                    coverage[topic].append(str(reading["card_id"]))
        return {topic: self._unique(ids) for topic, ids in coverage.items()}

    def _reading_blockers(self, task_id: str) -> list[str]:
        blockers: list[str] = []
        latest: dict[str, dict[str, Any]] = {}
        for reading in self.list_readings(task_id, status="VERIFIED"):
            latest[str(reading["card_id"])] = reading
        for card_id, reading in latest.items():
            receipt = reading["receipt"]
            if bool(receipt.get("needs_follow_up")):
                blockers.append(f"follow-up required for {card_id}")
            if receipt.get("conflicts"):
                blockers.append(f"unresolved conflict reported for {card_id}")
        return blockers

    def _saturation_status(self, task_id: str, plan: dict[str, Any]) -> dict[str, Any]:
        required_batches = max(1, int((plan.get("stop_policy") or {}).get("saturation_batches") or 2))
        completed_batches = [
            batch for batch in self.list_batches(task_id) if batch["status"] == "COMPLETED"
        ]
        recent = completed_batches[-required_batches:]
        if len(recent) < required_batches:
            return {
                "saturated": False, "required_batches": required_batches,
                "observed_batches": len(recent), "reason": "not enough completed batches",
            }
        recent_ids = {batch["id"] for batch in recent}
        readings = [
            reading for reading in self.list_readings(task_id, status="VERIFIED")
            if reading["batch_id"] in recent_ids
        ]
        novelty_found = any(
            reading["receipt"].get("novelty") == "new" or reading["receipt"].get("conflicts")
            for reading in readings
        )
        return {
            "saturated": not novelty_found, "required_batches": required_batches,
            "observed_batches": len(recent),
            "reason": "new method category or conflict in recent batches" if novelty_found else "recent batches added no new category or conflict",
        }

    def reconcile(self, task_id: str, cards: Iterable[dict[str, Any]]) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not task:
            raise KeyError(task_id)
        by_id = {
            str(card.get("id") or card.get("card_id") or ""): card
            for card in cards if isinstance(card, dict) and (card.get("id") or card.get("card_id"))
        }
        verified = self._unique(card_id for card_id in task["verified_card_ids"] if card_id in by_id)
        corpus_coverage = self._corpus_topic_coverage(verified, by_id, task["required_topics"])
        reading_coverage = self._reading_coverage(task_id, task["required_topics"])
        completed = self.completed_card_ids(task_id)
        active_batch = self.active_batch(task_id)
        pending_jobs = [
            job_id for job_id, value in task["ingestion_jobs"].items()
            if str((value or {}).get("status") or "submitted").lower() not in TERMINAL_JOB_STATES
        ]
        minimum = max(1, int(task.get("topic_minimum") or 1))
        missing_corpus_topics = [topic for topic, ids in corpus_coverage.items() if len(ids) < minimum]
        missing_read_topics = [topic for topic, ids in reading_coverage.items() if len(ids) < minimum]
        missing_corpus_count = max(0, int(task["target_papers"]) - len(verified))
        missing_read_count = max(0, int(task["target_papers"]) - len(completed))
        blockers = self._reading_blockers(task_id)
        saturation = self._saturation_status(task_id, task.get("plan") or {})
        unread_verified = [card_id for card_id in verified if card_id not in set(completed)]
        remaining: list[str] = []
        if missing_corpus_count:
            remaining.append(f"need {missing_corpus_count} more verified PaperPage(s)")
        if missing_corpus_topics:
            remaining.append(f"corpus coverage below minimum {minimum}: " + ", ".join(missing_corpus_topics))
        corpus_ready = not missing_corpus_count and not missing_corpus_topics
        if corpus_ready and missing_read_count:
            remaining.append(f"need {missing_read_count} more verified reading receipt(s)")
        if corpus_ready and missing_read_topics:
            remaining.append(f"reading coverage below minimum {minimum}: " + ", ".join(missing_read_topics))
        remaining.extend(blockers)
        if corpus_ready and not missing_read_count and not saturation["saturated"]:
            remaining.append("information saturation gate not met: " + saturation["reason"])
        stop_policy = (task.get("plan") or {}).get("stop_policy") or {}
        max_tool_calls = int(stop_policy.get("max_tool_calls") or 0)
        max_papers = int(stop_policy.get("max_papers") or 0)
        budget_exhausted = bool(
            (max_tool_calls and int(task["usage"].get("tool_calls") or 0) >= max_tool_calls)
            or (max_papers and len(completed) >= max_papers and (missing_read_count or missing_read_topics or blockers or not saturation["saturated"]))
        )
        terminal = task["phase"] in {"COMPLETE", "FAILED", "BUDGET_EXHAUSTED", "PAUSED"}
        if terminal:
            phase = task["phase"]
        elif budget_exhausted:
            phase = "BUDGET_EXHAUSTED"
        elif pending_jobs:
            phase = "WAIT_INGEST"
        elif not corpus_ready:
            phase = "DISCOVER"
        elif active_batch:
            phase = "READING"
        elif missing_read_count or missing_read_topics or blockers:
            phase = "BUILD_QUEUE" if missing_read_count else "CHECK_GATES"
        elif not saturation["saturated"]:
            phase = "BUILD_QUEUE" if unread_verified else "TARGETED_DISCOVERY"
        else:
            phase = "SYNTHESIZE"
        gate_status = {
            "corpus_ready": corpus_ready, "completed_count": len(completed),
            "target_papers": int(task["target_papers"]),
            "missing_corpus_topics": missing_corpus_topics,
            "missing_read_topics": missing_read_topics, "blocking_issues": blockers,
            "saturation": saturation, "budget_exhausted": budget_exhausted,
            "completion_ready": phase == "SYNTHESIZE",
        }
        return self._update(
            task_id, phase=phase, verified_card_ids=verified, coverage=reading_coverage,
            remaining_requirements=remaining, gate_status=gate_status,
        )

    def next_batch(
        self, task_id: str, cards: Iterable[dict[str, Any]], *, batch_size: int = 4,
    ) -> dict[str, Any]:
        task = self.reconcile(task_id, cards)
        existing = self.active_batch(task_id)
        if existing:
            return existing
        if task["phase"] not in {"BUILD_QUEUE", "READING"}:
            raise ValueError(f"research task is not ready for a reading batch: {task['phase']}")
        by_id = {
            str(card.get("id") or card.get("card_id") or ""): card
            for card in cards if isinstance(card, dict) and (card.get("id") or card.get("card_id"))
        }
        completed = set(self.completed_card_ids(task_id))
        candidates = [card_id for card_id in task["verified_card_ids"] if card_id not in completed and card_id in by_id]
        minimum = max(1, int(task.get("topic_minimum") or 1))
        deficits = {
            topic: max(0, minimum - len(task["coverage"].get(topic) or []))
            for topic in task["required_topics"]
        }

        def matched_topics(card_id: str) -> list[str]:
            haystack = self._card_haystack(by_id[card_id])
            return [
                topic for topic, aliases in task["required_topics"].items()
                if any(str(alias).lower() in haystack for alias in aliases)
            ]

        order = {card_id: index for index, card_id in enumerate(task["verified_card_ids"])}
        candidates.sort(key=lambda card_id: (
            -sum(deficits.get(topic, 0) for topic in matched_topics(card_id)),
            order.get(card_id, 10**9), card_id,
        ))
        selected = candidates[: max(1, min(int(batch_size), 5))]
        if not selected:
            raise ValueError("no unread verified paper remains; inspect gate blockers")
        purposes = {card_id: matched_topics(card_id) or ["general_review"] for card_id in selected}
        now = self._now()
        batch_id = str(uuid.uuid4())
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                "SELECT id FROM reading_batches WHERE task_id=? AND status IN ('ASSIGNED','OPENED') LIMIT 1",
                (str(task_id),),
            ).fetchone()
            if active:
                conn.rollback()
                return self.active_batch(task_id) or {}
            sequence = int(conn.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM reading_batches WHERE task_id=?", (str(task_id),)
            ).fetchone()[0])
            conn.execute(
                """INSERT INTO reading_batches
                   (id,task_id,sequence,status,assigned_card_ids_json,purposes_json,created_at,updated_at)
                   VALUES (?,?,?,'ASSIGNED',?,?,?,?)""",
                (batch_id, str(task_id), sequence, self._dump(selected), self._dump(purposes), now, now),
            )
            conn.execute(
                "UPDATE research_task_ledgers SET phase='READING', updated_at=? WHERE id=?",
                (now, str(task_id)),
            )
            conn.commit()
        return self.active_batch(task_id) or {}

    def submit_reading(
        self, task_id: str, batch_id: str, card_id: str, receipt: dict[str, Any], *,
        allowed_evidence_ids: Iterable[str] = (), prompt_version: str = "research-reading-v1",
        reopen_reason: str = "",
    ) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not task:
            raise KeyError(task_id)
        batch = next((item for item in self.list_batches(task_id) if item["id"] == str(batch_id)), None)
        if not batch:
            raise ValueError("batch does not belong to this task")
        card_id = str(card_id or "").strip()
        if card_id not in batch["assigned_card_ids"]:
            raise ValueError("card is not assigned to this batch")
        if not isinstance(receipt, dict):
            raise ValueError("receipt must be an object")
        required = {
            "covered_dimensions", "claims", "experimental_settings", "novelty",
            "conflicts", "open_questions", "needs_follow_up",
        }
        missing = sorted(required.difference(receipt))
        if missing:
            raise ValueError("missing reading fields: " + ", ".join(missing))
        novelty = str(receipt.get("novelty") or "").strip().lower()
        if novelty not in READING_NOVELTY:
            raise ValueError("invalid novelty")
        dimensions = self._unique(receipt.get("covered_dimensions") or [])
        invalid_dimensions = sorted(set(dimensions).difference(task["required_topics"]))
        if invalid_dimensions:
            raise ValueError("unknown covered dimensions: " + ", ".join(invalid_dimensions))
        claims = receipt.get("claims")
        if not isinstance(claims, list):
            raise ValueError("claims must be a list")
        if novelty != "irrelevant" and not claims:
            raise ValueError("a relevant paper must contain at least one claim")
        allowed = set(self._unique(allowed_evidence_ids))
        normalized_claims: list[dict[str, Any]] = []
        for claim in claims:
            if not isinstance(claim, dict) or not str(claim.get("statement") or "").strip():
                raise ValueError("each claim requires a statement")
            evidence_ids = self._unique(claim.get("evidence_ids") or [])
            if not evidence_ids:
                raise ValueError("each claim requires at least one evidence id")
            if not allowed or not set(evidence_ids).issubset(allowed):
                raise ValueError("claim references evidence outside the assigned Wiki card")
            normalized_claims.append({
                "statement": str(claim["statement"]).strip(), "evidence_ids": evidence_ids,
            })
        normalized = {
            "task_id": str(task_id), "batch_id": str(batch_id), "card_id": card_id,
            "covered_dimensions": dimensions, "claims": normalized_claims,
            "experimental_settings": receipt.get("experimental_settings") if isinstance(receipt.get("experimental_settings"), dict) else {},
            "novelty": novelty,
            "conflicts": receipt.get("conflicts") if isinstance(receipt.get("conflicts"), list) else [],
            "open_questions": receipt.get("open_questions") if isinstance(receipt.get("open_questions"), list) else [],
            "needs_follow_up": bool(receipt.get("needs_follow_up")),
        }
        now = self._now()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = int(conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM paper_readings WHERE task_id=? AND card_id=?",
                (str(task_id), card_id),
            ).fetchone()[0])
            reading_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO paper_readings
                   (id,task_id,batch_id,card_id,version,status,receipt_json,prompt_version,
                    reopen_reason,created_at,verified_at)
                   VALUES (?,?,?,?,?,'VERIFIED',?,?,?,?,?)""",
                (reading_id, str(task_id), str(batch_id), card_id, version, self._dump(normalized),
                 str(prompt_version), str(reopen_reason or ""), now, now),
            )
            completed = {
                str(row[0]) for row in conn.execute(
                    "SELECT DISTINCT card_id FROM paper_readings WHERE task_id=? AND status='VERIFIED'",
                    (str(task_id),),
                ).fetchall()
            }
            if set(batch["assigned_card_ids"]).issubset(completed):
                conn.execute(
                    "UPDATE reading_batches SET status='COMPLETED', updated_at=?, completed_at=? WHERE id=?",
                    (now, now, str(batch_id)),
                )
                conn.execute(
                    "UPDATE research_task_ledgers SET phase='CHECK_GATES', updated_at=? WHERE id=?",
                    (now, str(task_id)),
                )
            conn.commit()
        return self.latest_reading(task_id, card_id) or {}

    def reopen_paper(
        self, task_id: str, card_id: str, *, reason: str, section: str = "",
    ) -> dict[str, Any]:
        if not str(reason or "").strip():
            raise ValueError("reopen_reason is required")
        if not self.latest_reading(task_id, card_id):
            raise ValueError("paper has no verified reading to reopen")
        now = self._now()
        batch_id = str(uuid.uuid4())
        purpose = {str(card_id): [str(reason).strip(), str(section or "").strip()]}
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM reading_batches WHERE task_id=? AND status IN ('ASSIGNED','OPENED') LIMIT 1",
                (str(task_id),),
            ).fetchone():
                raise ValueError("finish the active reading batch before reopening a paper")
            sequence = int(conn.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM reading_batches WHERE task_id=?", (str(task_id),)
            ).fetchone()[0])
            conn.execute(
                """INSERT INTO reading_batches
                   (id,task_id,sequence,status,assigned_card_ids_json,purposes_json,created_at,updated_at)
                   VALUES (?,?,?,'ASSIGNED',?,?,?,?)""",
                (batch_id, str(task_id), sequence, self._dump([str(card_id)]), self._dump(purpose), now, now),
            )
            conn.execute(
                "UPDATE research_task_ledgers SET phase='READING', updated_at=? WHERE id=?",
                (now, str(task_id)),
            )
            conn.commit()
        return self.active_batch(task_id) or {}

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
            "usage": "usage_json", "plan": "plan_json", "gate_status": "gate_status_json",
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
                f"UPDATE research_task_ledgers SET {', '.join(assignments)} WHERE id=?", params,
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
            ("plan", {}), ("gate_status", {}),
        ):
            item[key] = self._load(item.pop(f"{key}_json", None), default)
        item["verified_count"] = len(item["verified_card_ids"])
        item["opened_count"] = len(item["opened_card_ids"])
        item["completed_card_ids"] = self.completed_card_ids(str(item["id"]))
        item["completed_count"] = len(item["completed_card_ids"])
        item["active_batch"] = self.active_batch(str(item["id"]))
        item["corpus_ready"] = bool(item["gate_status"].get("corpus_ready"))
        item["completion_ready"] = bool(item["gate_status"].get("completion_ready"))
        return item

    def _batch_row(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["assigned_card_ids"] = self._load(item.pop("assigned_card_ids_json", None), [])
        item["purposes"] = self._load(item.pop("purposes_json", None), {})
        return item

    def _reading_row(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["receipt"] = self._load(item.pop("receipt_json", None), {})
        return item

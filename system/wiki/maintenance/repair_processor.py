from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from system.wiki.maintenance.index_generator import WikiIndexGenerator
from system.wiki.maintenance.store import WikiMaintenanceStore


DETERMINISTIC_TASK_TYPES = {
    "fts_count_mismatch",
    "missing_fts_table",
    "index_stale",
    "duplicate_chunk_index",
    "empty_chunk",
}


class DeterministicRepairProcessor:
    """Process repair tasks that do not require semantic judgment."""

    def __init__(self, db_path: str | Path | None = None):
        repo_root = Path(__file__).resolve().parents[3]
        self.db_path = str(Path(db_path) if db_path else repo_root / "sessions.db")
        self.store = WikiMaintenanceStore(db_path=self.db_path)

    def process_pending(self, *, limit: int = 50, upload_indices: bool = True) -> dict[str, Any]:
        tasks = self.store.list_repair_tasks(status="pending", limit=limit)
        results = []
        for task in tasks:
            if task.get("repair_target") != "deterministic_fixer":
                continue
            results.append(self.process_task(task, upload_indices=upload_indices))
        return {
            "ok": True,
            "processed": len(results),
            "items": results,
        }

    def process_task(self, task: dict[str, Any], *, upload_indices: bool = True) -> dict[str, Any]:
        task_id = task.get("id", "")
        task_type = task.get("task_type", "")
        attempts = int(task.get("attempts") or 0) + 1
        try:
            if task_type in {"fts_count_mismatch", "missing_fts_table"}:
                result = self._rebuild_fts()
                status = "applied"
            elif task_type == "index_stale":
                result = WikiIndexGenerator(db_path=self.db_path).generate_all(upload=upload_indices)
                status = "applied"
            elif task_type in {"duplicate_chunk_index", "empty_chunk"}:
                result = self._rebuild_chunks_fts_only()
                status = "applied"
            elif task_type in DETERMINISTIC_TASK_TYPES:
                result = {"ok": False, "reason": "no deterministic handler implemented"}
                status = "quarantined"
            else:
                result = {"ok": False, "reason": "not a deterministic task type"}
                status = "pending"
            self.store.update_repair_task(task_id, status=status, attempts=attempts, result=result)
            return {"id": task_id, "task_type": task_type, "status": status, "result": result}
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
            status = "quarantined" if attempts >= 2 else "pending"
            self.store.update_repair_task(task_id, status=status, attempts=attempts, result=result)
            return {"id": task_id, "task_type": task_type, "status": status, "result": result}

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _rebuild_fts(self) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            self._rebuild_pages_fts(conn)
            self._rebuild_chunks_fts(conn)
            conn.commit()
        return {"ok": True, "repaired": ["wiki_pages_fts", "wiki_chunks_fts"]}

    def _rebuild_chunks_fts_only(self) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            self._rebuild_chunks_fts(conn)
            conn.commit()
        return {"ok": True, "repaired": ["wiki_chunks_fts"]}

    @staticmethod
    def _rebuild_pages_fts(conn: sqlite3.Connection) -> None:
        for trigger in ("wiki_pages_ai", "wiki_pages_ad", "wiki_pages_au"):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute("DROP TABLE IF EXISTS wiki_pages_fts")
        conn.execute(
            """
            CREATE VIRTUAL TABLE wiki_pages_fts
            USING fts5(title, summary, content='wiki_pages', content_rowid='rowid')
            """
        )
        conn.execute(
            """
            CREATE TRIGGER wiki_pages_ai
            AFTER INSERT ON wiki_pages BEGIN
                INSERT INTO wiki_pages_fts(rowid, title, summary)
                VALUES (new.rowid, new.title, new.summary);
            END;
            """
        )
        conn.execute(
            """
            CREATE TRIGGER wiki_pages_ad
            AFTER DELETE ON wiki_pages BEGIN
                INSERT INTO wiki_pages_fts(wiki_pages_fts, rowid, title, summary)
                VALUES ('delete', old.rowid, old.title, old.summary);
            END;
            """
        )
        conn.execute(
            """
            CREATE TRIGGER wiki_pages_au
            AFTER UPDATE ON wiki_pages BEGIN
                INSERT INTO wiki_pages_fts(wiki_pages_fts, rowid, title, summary)
                VALUES ('delete', old.rowid, old.title, old.summary);
                INSERT INTO wiki_pages_fts(rowid, title, summary)
                VALUES (new.rowid, new.title, new.summary);
            END;
            """
        )
        conn.execute("INSERT INTO wiki_pages_fts(wiki_pages_fts) VALUES ('rebuild')")

    @staticmethod
    def _rebuild_chunks_fts(conn: sqlite3.Connection) -> None:
        for trigger in ("wiki_chunks_ai", "wiki_chunks_ad", "wiki_chunks_au"):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute("DROP TABLE IF EXISTS wiki_chunks_fts")
        conn.execute(
            """
            CREATE VIRTUAL TABLE wiki_chunks_fts
            USING fts5(text, content='wiki_chunks', content_rowid='rowid')
            """
        )
        conn.execute(
            """
            CREATE TRIGGER wiki_chunks_ai
            AFTER INSERT ON wiki_chunks BEGIN
                INSERT INTO wiki_chunks_fts(rowid, text)
                VALUES (new.rowid, new.text);
            END;
            """
        )
        conn.execute(
            """
            CREATE TRIGGER wiki_chunks_ad
            AFTER DELETE ON wiki_chunks BEGIN
                INSERT INTO wiki_chunks_fts(wiki_chunks_fts, rowid, text)
                VALUES ('delete', old.rowid, old.text);
            END;
            """
        )
        conn.execute(
            """
            CREATE TRIGGER wiki_chunks_au
            AFTER UPDATE ON wiki_chunks BEGIN
                INSERT INTO wiki_chunks_fts(wiki_chunks_fts, rowid, text)
                VALUES ('delete', old.rowid, old.text);
                INSERT INTO wiki_chunks_fts(rowid, text)
                VALUES (new.rowid, new.text);
            END;
            """
        )
        conn.execute("INSERT INTO wiki_chunks_fts(wiki_chunks_fts) VALUES ('rebuild')")

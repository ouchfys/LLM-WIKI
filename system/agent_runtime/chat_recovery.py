"""Durable chat boundaries, not a snapshot of a Python process.

Large observations live once in session_tool_results. This journal stores only
call identity, arguments and result references. Unknown writes require a check.
"""
from contextlib import closing
import json
import uuid

from system.agent_runtime.control import RunCancelled
from system.agent_runtime.tool_operations import tool_operation


class ChatRecovery:
    def __init__(self, runtime):
        self.runtime = runtime
        with closing(runtime._connect()) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS chat_tool_calls (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    tool TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    read_only INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'started',
                    result_id INTEGER,
                    job_id TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, ordinal)
                );
                CREATE INDEX IF NOT EXISTS chat_calls_run ON chat_tool_calls(run_id, ordinal);
                CREATE TABLE IF NOT EXISTS chat_model_state (
                    run_id TEXT PRIMARY KEY REFERENCES agent_runs(id) ON DELETE CASCADE,
                    state_json TEXT NOT NULL
                );
            """)
            runtime._ensure_column(conn, "chat_tool_calls", "job_id", "TEXT DEFAULT ''")
            conn.commit()

    def save_model_state(self, run_id, state):
        with closing(self.runtime._connect()) as conn:
            self._check(conn, run_id)
            conn.execute("INSERT OR REPLACE INTO chat_model_state(run_id,state_json) VALUES(?,?)",
                         (run_id, json.dumps(state, ensure_ascii=False)))
            conn.commit()

    def load_model_state(self, run_id):
        with closing(self.runtime._connect()) as conn:
            row = conn.execute("SELECT state_json FROM chat_model_state WHERE run_id=?", (run_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def prepare(self, run_id, calls):
        """Flush the whole batch before any tool starts; preserve model order."""
        rows = []
        with closing(self.runtime._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._check(conn, run_id)
            start = conn.execute("SELECT COALESCE(MAX(ordinal),0) FROM chat_tool_calls WHERE run_id=?", (run_id,)).fetchone()[0]
            for index, (tool, arguments, signature, read_only) in enumerate(calls, start + 1):
                call_id = str(uuid.uuid4())
                conn.execute("INSERT INTO chat_tool_calls(id,run_id,ordinal,tool,arguments_json,signature,read_only,created_at,status) VALUES(?,?,?,?,?,?,?,?,'prepared')",
                             (call_id, run_id, index, tool, json.dumps(arguments, ensure_ascii=False), signature, int(read_only), self.runtime.now_iso()))
                rows.append(call_id)
            conn.commit()
        return rows

    def begin(self, run_id, call_id):
        """Only a dispatched write can have an unknown outcome after interruption."""
        with closing(self.runtime._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._check(conn, run_id)
            changed = conn.execute("UPDATE chat_tool_calls SET status='started' WHERE id=? AND run_id=? AND status='prepared'", (call_id, run_id)).rowcount
            if not changed:
                raise RunCancelled("Tool attempt is no longer pending")
            conn.commit()

    def finish(self, run_id, call_id, session_id, tool, arguments, payload):
        """Result and journal completion commit atomically; deletion fences late results."""
        with closing(self.runtime._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._check(conn, run_id)
            if not conn.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
                raise RunCancelled("Conversation was deleted")
            row = conn.execute("SELECT * FROM chat_tool_calls WHERE id=? AND run_id=?", (call_id, run_id)).fetchone()
            if not row or row["status"] != "started":
                raise RunCancelled("Tool attempt is no longer current")
            cursor = conn.execute("INSERT INTO session_tool_results(session_id,tool,arguments_json,result_json,created_at) VALUES(?,?,?,?,?)",
                                  (session_id, tool, json.dumps(arguments, ensure_ascii=False), json.dumps(payload, ensure_ascii=False, default=str), self.runtime.now_iso()))
            conn.execute("UPDATE chat_tool_calls SET status='finished',result_id=? WHERE id=?", (cursor.lastrowid, call_id))
            conn.commit()
            return int(cursor.lastrowid)

    @staticmethod
    def _check(conn, run_id):
        row = conn.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        if not row or row["cancel_requested"] or row["current_state"] != "CHAT_RUNNING":
            raise RunCancelled("Chat stopped or was deleted")
        from system.agent_runtime.control import get_run_control
        control = get_run_control()
        if control and getattr(control, "lease_owner", "") and (row["lease_owner"] != control.lease_owner or row["lease_expires_at"] <= control.store.now_iso()):
            raise RunCancelled("Worker was replaced")
        return row

    def calls(self, run_id, *, include_results=True):
        with closing(self.runtime._connect()) as conn:
            column = "r.result_json" if include_results else "NULL AS result_json"
            rows = conn.execute(f"""SELECT c.*,{column} FROM chat_tool_calls c
                LEFT JOIN session_tool_results r ON r.id=c.result_id
                WHERE c.run_id=? ORDER BY c.ordinal""", (run_id,)).fetchall()
        return [{**dict(row), "arguments": self.runtime.load_json(row["arguments_json"]),
                 "result": self.runtime.load_json(row["result_json"])} for row in rows]

    def reconcile_imports(self, run_id):
        """An ingestion request uses its call ID as job ID, including lost HTTP replies."""
        from system.wiki.ingestion_jobs import IngestionJobStore
        jobs = IngestionJobStore(self.runtime.db_path)
        with closing(self.runtime._connect()) as conn:
            for call in self.calls(run_id, include_results=False):
                if call["status"] != "started" or tool_operation(call["tool"], call["arguments"]) != "arxiv_import_paper":
                    continue
                job = jobs.get_job(call.get("job_id") or call["id"])
                if not job:
                    continue
                conn.execute("UPDATE chat_tool_calls SET status='reconciled' WHERE id=? AND status='started'", (call["id"],))
            conn.commit()

    def list_for_session(self, session_id):
        self.runtime.expire_chat_runs()
        with closing(self.runtime._connect()) as conn:
            rows = conn.execute("""SELECT * FROM agent_runs WHERE run_type='wiki_chat' AND source_uri=?
                AND (current_state IN ('CHAT_INTERRUPTED','FAILED') OR
                     (current_state='COMPLETED' AND json_extract(result_json,'$.task_outcome.status')='partial'))
                ORDER BY updated_at DESC""", (f"session:{session_id}",)).fetchall()
        result = []
        for row in rows:
            context = self.runtime.load_json(row["context_json"])
            if "request_message" not in context:
                continue  # Legacy traces are inspectable, but have no complete request.
            self.reconcile_imports(row["id"])
            calls = self.calls(row["id"], include_results=False)
            unknown = [{"id": c["id"], "tool": c["tool"], "arguments": c["arguments"]}
                       for c in calls if c["status"] == "started" and not c["read_only"]]
            result.append({"run_id": row["id"], "message": context["request_message"],
                           "status": row["current_state"], "unknown_writes": unknown,
                           "task_outcome": self.runtime.load_json(row["result_json"]).get("task_outcome", {}),
                           "can_resume": not (row["lease_owner"] and row["lease_expires_at"] > self.runtime.now_iso()),
                           "completed_tools": sum(c["status"] in {"finished", "reconciled", "confirmed_done"} for c in calls)})
        return result

    def resolve(self, run_id, call_id, decision):
        if decision not in {"confirmed_done", "retry_allowed"}:
            raise ValueError("Choose confirmed_done or retry_allowed after checking the actual outcome")
        with closing(self.runtime._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute("SELECT current_state FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if not run or run[0] not in {"CHAT_INTERRUPTED", "FAILED"}:
                raise ValueError("Only an interrupted task can be reconciled")
            changed = conn.execute("UPDATE chat_tool_calls SET status=? WHERE id=? AND run_id=? AND status='started' AND read_only=0",
                                   (decision, call_id, run_id)).rowcount
            if not changed:
                raise ValueError("No unresolved write with that ID")
            conn.commit()
        self.runtime.append_event(run_id, event_type="recovery.write_resolved", status=decision, input_data={"call_id": call_id})

    def resume(self, run_id, session_id, owner, message=""):
        self.runtime.expire_chat_runs()
        self.reconcile_imports(run_id)
        with closing(self.runtime._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM agent_runs WHERE id=? AND run_type='wiki_chat' AND source_uri=?", (run_id, f"session:{session_id}")).fetchone()
            if not row or not conn.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
                raise ValueError("Conversation or task no longer exists")
            previous_result = self.runtime.load_json(row["result_json"])
            partial = (row["current_state"] == "COMPLETED"
                       and previous_result.get("task_outcome", {}).get("status") == "partial")
            if row["current_state"] not in {"CHAT_INTERRUPTED", "FAILED"} and not partial:
                raise ValueError("Task is active, completed or cancelled; it cannot be resumed")
            if row["lease_owner"] and row["lease_expires_at"] > self.runtime.now_iso():
                raise ValueError("Previous worker is still stopping; retry shortly")
            context = self.runtime.load_json(row["context_json"])
            if "request_message" not in context:
                raise ValueError("Legacy trace has no recoverable request")
            if conn.execute("SELECT 1 FROM chat_tool_calls WHERE run_id=? AND status='started' AND read_only=0 LIMIT 1", (run_id,)).fetchone():
                raise ValueError("Check unresolved write outcomes before continuing")
            # A committed answer may predate the final run-status update.
            completed = conn.execute("SELECT metadata_json FROM messages WHERE session_id=? AND role='assistant' AND json_extract(metadata_json,'$.completed_run_id')=? AND COALESCE(json_extract(metadata_json,'$.completed_run_attempt'),0)=?", (session_id, run_id, int(row["attempt"] or 0))).fetchone()
            saved_outcome = self.runtime.load_json(completed[0]).get("trace", {}).get("task_outcome", {}) if completed else {}
            if completed and not partial and saved_outcome.get("status") != "partial":
                conn.execute("UPDATE agent_runs SET current_state='COMPLETED',status='completed' WHERE id=?", (run_id,))
                conn.commit()
                raise ValueError("Answer already saved; refresh the conversation")
            # Historical counters remain useful, but old quotas must not limit
            # resumed conversations (including cursors already using no cap).
            budget = context.get("research_state", {}).get("budget", {})
            if budget:
                budget["max_calls"] = None
            context.pop("retrieval_blocked", None)
            # A partial answer closes this attempt, including its plan-check step.
            if partial or saved_outcome.get("status") == "partial":
                context["planner_steps"] = 0
            context.pop("stop_reason", None)
            context.pop("task_outcome", None)
            # Append new instructions atomically with claiming the run. A failed
            # resume cannot enqueue them twice or silently lose them after a crash.
            now = self.runtime.now_iso()
            if message.strip():
                conn.execute("""INSERT INTO agent_run_inputs(id,run_id,kind,content,status,created_at,updated_at)
                    VALUES(?,?,'interrupt',?,'consumed',?,?)""", (str(uuid.uuid4()), run_id, message.strip(), now, now))
            conn.execute("UPDATE agent_run_inputs SET status='consumed',updated_at=? WHERE run_id=? AND kind='interrupt' AND status IN ('pending','blocked')", (now, run_id))
            for item in conn.execute("SELECT content FROM agent_run_inputs WHERE run_id=? AND kind='interrupt' AND status='consumed' ORDER BY rowid", (run_id,)):
                context["request_message"] += f"\n\n[用户补充要求，以此为最新要求]\n{item[0]}"
            # Persist the migration before dispatch. Request text is
            # reconstructed above from inputs.
            persisted = self.runtime.load_json(row["context_json"])
            for key in ("research_state", "planner_steps", "prior_usage"):
                if key in context:
                    persisted[key] = context[key]
            persisted.pop("stop_reason", None)
            persisted.pop("task_outcome", None)
            persisted.pop("retrieval_blocked", None)
            conn.execute("UPDATE agent_runs SET current_state='CHAT_RUNNING',status='running',cancel_requested=0,input_closed=0,lease_owner=?,lease_expires_at=?,attempt=attempt+1,error='',result_json='{}',context_json=?,updated_at=? WHERE id=?",
                         (owner, self.runtime.future_iso(90), json.dumps(persisted, ensure_ascii=False), self.runtime.now_iso(), run_id))
            conn.commit()
        return context

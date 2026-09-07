from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from backend.deps import get_session_store
from backend.task_executor import submit_agent_task
from system.agent_runtime import AgentRunStore
from system.wiki.ingestion_jobs import IngestionJobStore


APPROVAL_RECOVERY_STATES = {
    "AWAITING_APPROVAL", "COMMIT_FAILED", "COMMITTING", "REINDEXING",
}


def recover_agent_runs(db_path: str = "") -> list[dict[str, Any]]:
    """Dispatch work abandoned by workers whose renewable lease has expired."""
    resolved = db_path or get_session_store().db_path
    runtime = AgentRunStore(db_path=resolved)
    dispatched: list[dict[str, Any]] = []
    for run in runtime.list_recoverable_runs(limit=200):
        try:
            dispatched.append(
                dispatch_agent_run_resume(
                    str(run["id"]), db_path=resolved, reason="startup recovery",
                    record_deferred=False,
                )
            )
        except Exception as exc:
            runtime.append_event(
                str(run["id"]), event_type="recovery.dispatch_failed",
                node_name=str(run.get("current_state") or ""), status="failed",
                error=str(exc),
            )
    return dispatched


def dispatch_agent_run_resume(
    run_id: str,
    *,
    db_path: str = "",
    reason: str = "manual resume",
    record_deferred: bool = True,
) -> dict[str, Any]:
    resolved = db_path or get_session_store().db_path
    runtime = AgentRunStore(db_path=resolved)
    run = runtime.get_run(run_id)
    if not run:
        raise KeyError(f"Agent run not found: {run_id}")
    state = str(run.get("current_state") or "")
    if state in {"COMPLETED", "REJECTED", "CANCELLED"}:
        raise ValueError(f"Run in {state} does not require recovery.")

    approvals = runtime.list_approvals(status="", run_id=run_id, limit=500)
    has_commit_intent = any(
        item.get("status") in {"accepted", "committing", "commit_failed", "approved"}
        for item in approvals
    )
    has_pending_approval = any(item.get("status") == "pending" for item in approvals)
    if state in APPROVAL_RECOVERY_STATES and has_commit_intent:
        if state == "AWAITING_APPROVAL" and has_pending_approval:
            raise ValueError("Run is still waiting for human approval.")
        target = _resume_approval_commit
        kwargs = {"run_id": run_id, "db_path": resolved}
        mode = "approval_commit"
    else:
        context = run.get("context") if isinstance(run.get("context"), dict) else {}
        pdf_path = str(context.get("pdf_path") or run.get("source_uri") or "")
        job_id = str(run.get("ingestion_job_id") or context.get("job_id") or "")
        if run.get("run_type") != "paper_ingestion" or not pdf_path or not job_id:
            raise ValueError("Run has no replayable paper-ingestion source context.")
        if not Path(pdf_path).exists():
            raise ValueError(f"Immutable ingestion source no longer exists: {pdf_path}")
        target = _prepare_and_resume_ingestion
        kwargs = {
            "run_id": run_id, "db_path": resolved, "job_id": job_id,
            "pdf_path": pdf_path, "source_url": str(context.get("source_url") or ""),
            "pipeline": str(context.get("pipeline") or "wiki_compile"),
            "approval_mode": str(run.get("approval_mode") or "manual"),
            "reason": reason,
        }
        mode = "source_replay"

    dispatch = submit_agent_task(
        run_id=run_id,
        db_path=resolved,
        job_id=str(run.get("ingestion_job_id") or ""),
        task_name=f"recovery_{mode}",
        target=target,
        kwargs=kwargs,
        record_deferred=record_deferred,
    )
    if dispatch.get("status") == "scheduled":
        runtime.append_event(
            run_id, event_type="recovery.dispatched", node_name=state,
            status="queued", input_data={"reason": reason, "mode": mode},
        )
    return {
        "ok": True,
        "run_id": run_id,
        "mode": mode,
        "state": state,
        "dispatch": dispatch,
    }


def _prepare_and_resume_ingestion(
    *,
    run_id: str,
    db_path: str,
    job_id: str,
    pdf_path: str,
    source_url: str,
    pipeline: str,
    approval_mode: str,
    reason: str,
) -> None:
    runtime = AgentRunStore(db_path=db_path)
    run = runtime.get_run(run_id) or {}
    if str(run.get("current_state") or "") != "QUEUED":
        runtime.restart_run(run_id, reason=reason)
    owner = f"recovery-{uuid.uuid4()}"
    if not runtime.acquire_lease(run_id, owner, ttl_seconds=90):
        runtime.append_event(
            run_id,
            event_type="recovery.lease_busy",
            node_name="QUEUED",
            status="deferred",
        )
        return
    IngestionJobStore(db_path=db_path).update_job(
        job_id, status="queued", stage="recovering", progress=0.0, error=""
    )
    _resume_ingestion(
        run_id=run_id,
        db_path=db_path,
        job_id=job_id,
        pdf_path=pdf_path,
        source_url=source_url,
        pipeline=pipeline,
        approval_mode=approval_mode,
        lease_owner=owner,
    )


def _resume_ingestion(**kwargs: Any) -> None:
    from backend.api.papers import _run_ingestion_job

    _run_ingestion_job(**kwargs)


def _resume_approval_commit(*, run_id: str, db_path: str) -> None:
    from backend.api.agent_runs import _finalize_run_if_decided
    from system.wiki.paper_pipeline.store import PaperWikiPipelineStore
    from system.wiki.wiki_store import WikiStore

    wiki = WikiStore(db_path=db_path)
    try:
        _finalize_run_if_decided(
            run_id,
            runtime=AgentRunStore(db_path=db_path),
            pipeline=PaperWikiPipelineStore(db_path=db_path),
            wiki=wiki,
        )
    except Exception:
        # Finalizer persists COMMIT_FAILED, the approval error, and its trace.
        return

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.deps import get_chunk_index, get_wiki_store
from system.agent_runtime import (
    AgentRunLease,
    AgentRunStore,
    InvalidStateTransition,
    RunLeaseBusy,
    TraceRecorder,
)
from system.wiki.ingestion_jobs import IngestionJobStore
from system.wiki.markdown_reindexer import MarkdownWikiReindexer
from system.wiki.paper_pipeline.store import PaperWikiPipelineStore
from system.wiki.revision import WikiRevisionManager
from system.wiki.wiki_store import WikiStore


router = APIRouter()


class ApprovalDecisionPayload(BaseModel):
    reason: str = ""


class ApprovalEditPayload(BaseModel):
    full_markdown: str
    reason: str = "human edited proposal"


class RetryPayload(BaseModel):
    reason: str = "manual retry"


class RunInputPayload(BaseModel):
    kind: Literal["interrupt", "followup", "command"] = "interrupt"
    content: str = Field(min_length=1, max_length=8000)
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), min_length=1, max_length=100)


class RunInputStatePayload(BaseModel):
    status: Literal["running", "completed", "failed", "blocked", "dismissed"]


def _control_error(exc):
    return HTTPException(status_code=404 if isinstance(exc, KeyError) else 409, detail=str(exc))


@router.post("/{run_id}/cancel")
def cancel_agent_run(run_id: str, wiki: WikiStore = Depends(get_wiki_store)):
    try:
        return AgentRunStore(db_path=wiki.db_path).request_cancel(run_id)
    except (KeyError, ValueError) as exc:
        raise _control_error(exc) from exc


@router.get("/inbox")
def list_session_inputs(session_id: str, wiki: WikiStore = Depends(get_wiki_store)):
    return {"items": AgentRunStore(db_path=wiki.db_path).list_inputs(session_id=session_id)}


@router.get("/{run_id}/inputs")
def list_run_inputs(run_id: str, wiki: WikiStore = Depends(get_wiki_store)):
    return {"items": AgentRunStore(db_path=wiki.db_path).list_inputs(run_id)}


@router.post("/{run_id}/inputs")
def enqueue_run_input(run_id: str, payload: RunInputPayload, wiki: WikiStore = Depends(get_wiki_store)):
    try:
        return AgentRunStore(db_path=wiki.db_path).enqueue_input(
            run_id, kind=payload.kind, content=payload.content, input_id=payload.id,
        )
    except (KeyError, ValueError) as exc:
        raise _control_error(exc) from exc


@router.post("/{run_id}/inputs/{input_id}/state")
def update_run_input(run_id: str, input_id: str, payload: RunInputStatePayload, wiki: WikiStore = Depends(get_wiki_store)):
    try:
        return AgentRunStore(db_path=wiki.db_path).update_input(run_id, input_id, payload.status)
    except (KeyError, ValueError) as exc:
        raise _control_error(exc) from exc


@router.post("/{run_id}/resume")
def resume_agent_run(
    run_id: str,
    payload: RetryPayload,
    wiki: WikiStore = Depends(get_wiki_store),
):
    from backend.agent_recovery import dispatch_agent_run_resume

    try:
        return dispatch_agent_run_resume(
            run_id, db_path=wiki.db_path, reason=payload.reason or "manual resume"
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _stores(wiki: WikiStore) -> tuple[AgentRunStore, PaperWikiPipelineStore]:
    return AgentRunStore(db_path=wiki.db_path), PaperWikiPipelineStore(db_path=wiki.db_path)


def _manager(wiki: WikiStore, pipeline: PaperWikiPipelineStore) -> WikiRevisionManager:
    return WikiRevisionManager(
        store=pipeline,
        vault=wiki.vault,
        reindexer=MarkdownWikiReindexer(db_path=wiki.db_path),
    )


@router.get("")
def list_agent_runs(
    limit: int = 100,
    status: str = "",
    wiki: WikiStore = Depends(get_wiki_store),
):
    runtime, _ = _stores(wiki)
    return {"items": runtime.list_runs(limit=limit, status=status)}


@router.get("/approvals")
def list_approvals(
    status: str = "action_required",
    run_id: str = "",
    limit: int = 100,
    wiki: WikiStore = Depends(get_wiki_store),
):
    runtime, pipeline = _stores(wiki)
    items = [
        _approval_detail(item, runtime=runtime, pipeline=pipeline)
        for item in runtime.list_approvals(status=status, run_id=run_id, limit=limit)
    ]
    if status == "action_required":
        items = [
            item for item in items
            if str(item.get("status") or "") == "commit_failed" or item.get("conflict_pairs")
        ]
    return {"items": items}


@router.get("/approvals/{approval_id}")
def get_approval(
    approval_id: str,
    wiki: WikiStore = Depends(get_wiki_store),
):
    runtime, pipeline = _stores(wiki)
    approval = runtime.get_approval(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")
    return _approval_detail(approval, runtime=runtime, pipeline=pipeline)


@router.post("/approvals/{approval_id}/approve")
def approve_revision(
    approval_id: str,
    payload: ApprovalDecisionPayload,
    wiki: WikiStore = Depends(get_wiki_store),
):
    runtime, pipeline = _stores(wiki)
    approval = _pending_approval(runtime, approval_id)
    try:
        runtime.accept_approval(approval_id, reason=payload.reason or "approved by human")
        committed = _finalize_run_if_decided(
            str(approval["run_id"]), runtime=runtime, pipeline=pipeline, wiki=wiki
        )
        return {"ok": True, "approval": runtime.get_approval(approval_id), "committed": committed}
    except (ValueError, RunLeaseBusy) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"Commit failed and remains retryable: {exc}"
        ) from exc


@router.post("/approvals/{approval_id}/retry")
def retry_approval_commit(
    approval_id: str,
    payload: RetryPayload,
    wiki: WikiStore = Depends(get_wiki_store),
):
    runtime, pipeline = _stores(wiki)
    approval = runtime.get_approval(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")
    if approval["status"] != "commit_failed":
        raise HTTPException(status_code=409, detail="Only a failed commit can be retried")
    runtime.append_event(
        str(approval["run_id"]), event_type="approval.retry_requested",
        node_name="COMMIT_FAILED", status="retrying",
        input_data={"approval_id": approval_id, "reason": payload.reason},
    )
    try:
        committed = _finalize_run_if_decided(
            str(approval["run_id"]), runtime=runtime, pipeline=pipeline, wiki=wiki
        )
        return {"ok": True, "approval": runtime.get_approval(approval_id), "committed": committed}
    except (ValueError, RunLeaseBusy) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"Commit retry failed and remains retryable: {exc}"
        ) from exc


@router.post("/approvals/{approval_id}/reject")
def reject_revision(
    approval_id: str,
    payload: ApprovalDecisionPayload,
    wiki: WikiStore = Depends(get_wiki_store),
):
    runtime, pipeline = _stores(wiki)
    approval = _actionable_approval(runtime, approval_id, {"pending", "commit_failed"})
    try:
        rejected = _manager(wiki, pipeline).reject_proposal(str(approval["revision_id"]))
        runtime.transition_approval(
            approval_id, status="rejected", expected_statuses={str(approval["status"])},
            reason=payload.reason or "rejected",
        )
        _finalize_run_if_decided(
            str(approval["run_id"]), runtime=runtime, pipeline=pipeline, wiki=wiki
        )
        return {"ok": True, "approval": runtime.get_approval(approval_id), "revision": rejected}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/approvals/{approval_id}/edit")
def edit_revision(
    approval_id: str,
    payload: ApprovalEditPayload,
    wiki: WikiStore = Depends(get_wiki_store),
):
    runtime, pipeline = _stores(wiki)
    approval = _actionable_approval(runtime, approval_id, {"pending", "commit_failed"})
    if not payload.full_markdown.strip():
        raise HTTPException(status_code=400, detail="full_markdown cannot be empty")
    try:
        replacement = _manager(wiki, pipeline).edit_proposal(
            str(approval["revision_id"]),
            payload.full_markdown,
            reason=payload.reason or "human edited proposal",
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if replacement["review_status"] != "proposed":
        raise HTTPException(
            status_code=409,
            detail={"message": "Edited proposal failed evidence verification", "replacement": replacement},
        )
    next_approval = runtime.create_approval(
        run_id=str(approval["run_id"]),
        revision_id=str(replacement["revision_id"]),
        page_id=str(approval["page_id"]),
        title=str(approval.get("title") or ""),
    )
    runtime.transition_approval(
        approval_id, status="superseded", expected_statuses={str(approval["status"])},
        reason=payload.reason or "edited", superseded_by=str(next_approval["id"]),
    )
    _manager(wiki, pipeline).reject_proposal(str(approval["revision_id"]))
    return {
        "ok": True,
        "previous_approval": runtime.get_approval(approval_id),
        "approval": _approval_detail(next_approval, runtime=runtime, pipeline=pipeline),
    }


@router.get("/{run_id}")
def get_agent_run(
    run_id: str,
    wiki: WikiStore = Depends(get_wiki_store),
):
    runtime, pipeline = _stores(wiki)
    run = runtime.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Agent run not found")
    return {
        **run,
        "approvals": [
            _approval_detail(item, runtime=runtime, pipeline=pipeline)
            for item in runtime.list_approvals(status="", run_id=run_id, limit=200)
        ],
    }


@router.get("/{run_id}/events")
def list_agent_events(
    run_id: str,
    after: int = 0,
    limit: int = 1000,
    wiki: WikiStore = Depends(get_wiki_store),
):
    runtime, _ = _stores(wiki)
    if not runtime.get_run(run_id):
        raise HTTPException(status_code=404, detail="Agent run not found")
    return {"run_id": run_id, "items": runtime.list_events(run_id, after=after, limit=limit)}


@router.get("/{run_id}/events/stream")
def stream_agent_events(
    run_id: str,
    after: int = 0,
    wiki: WikiStore = Depends(get_wiki_store),
):
    runtime, _ = _stores(wiki)
    if not runtime.get_run(run_id):
        raise HTTPException(status_code=404, detail="Agent run not found")

    def generate():
        cursor = max(0, int(after))
        idle = 0
        while idle < 60:
            events = runtime.list_events(run_id, after=cursor, limit=500)
            if events:
                idle = 0
                for event in events:
                    cursor = max(cursor, int(event["sequence"]))
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            else:
                idle += 1
                yield ": keep-alive\n\n"
            run = runtime.get_run(run_id) or {}
            if run.get("current_state") in {"COMPLETED", "REJECTED", "FAILED", "CANCELLED"} and not events:
                break
            time.sleep(1)

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


def _pending_approval(runtime: AgentRunStore, approval_id: str) -> dict[str, Any]:
    return _actionable_approval(runtime, approval_id, {"pending"})


def _actionable_approval(
    runtime: AgentRunStore,
    approval_id: str,
    statuses: set[str],
) -> dict[str, Any]:
    approval = runtime.get_approval(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")
    if str(approval["status"]) not in statuses:
        raise HTTPException(
            status_code=409,
            detail=f"Approval must be in {sorted(statuses)}, current status is {approval['status']}",
        )
    return approval


def _approval_detail(
    approval: dict[str, Any],
    *,
    runtime: AgentRunStore,
    pipeline: PaperWikiPipelineStore,
) -> dict[str, Any]:
    revision = pipeline.get_revision(str(approval["revision_id"])) or {}
    verification = revision.get("verification") if isinstance(revision.get("verification"), dict) else {}
    claims = [item for item in verification.get("claims") or [] if isinstance(item, dict)]
    affected_metadata = {
        str(item.get("claim_id") or ""): item
        for item in verification.get("affected_claims") or []
        if isinstance(item, dict) and str(item.get("claim_id") or "")
    }
    affected_actions = _affected_claim_actions(str(revision.get("full_markdown") or ""))
    affected_actions.update({
        claim_id: str(item.get("action") or "")
        for claim_id, item in affected_metadata.items()
    })
    claim_rows = []
    checks = {str(item.get("claim_id") or item.get("statement") or ""): item for item in verification.get("checks") or []}
    for claim in claims:
        check = checks.get(str(claim.get("id") or "")) or checks.get(str(claim.get("statement") or "")) or {}
        claim_rows.append({
            **claim,
            "action": affected_actions.get(str(claim.get("id") or ""), ""),
            "change": affected_metadata.get(str(claim.get("id") or ""), {}),
            "verification": check,
        })
    affected_claims = [item for item in claim_rows if item.get("action")]
    if not affected_claims:
        affected_claims = [item for item in claim_rows if item.get("verification")]
    conflict_pairs = _conflict_pairs(
        claim_rows,
        pipeline,
        page_title=str(revision.get("title") or approval.get("title") or ""),
    )
    return {
        **approval,
        "run": runtime.get_run(str(approval["run_id"])),
        "revision": revision,
        "claims": claim_rows,
        "affected_claims": affected_claims,
        "conflict_pairs": conflict_pairs,
        "decision_context": _approval_decision_context(revision, affected_claims, conflict_pairs),
    }


def _source_summary(source_packet_id: str, pipeline: PaperWikiPipelineStore) -> dict[str, Any]:
    packet = pipeline.get_source_packet(source_packet_id) if source_packet_id else None
    if not packet:
        return {"source_packet_id": source_packet_id, "title": "未知来源", "source_type": "unknown", "url": ""}
    return {
        "source_packet_id": packet.source_id,
        "title": packet.title,
        "source_type": packet.source_type,
        "url": packet.source_urls[0] if packet.source_urls else "",
        "parser": packet.parser_used,
    }


def _conflict_pairs(
    claims: list[dict[str, Any]],
    pipeline: PaperWikiPipelineStore,
    page_title: str = "",
) -> list[dict[str, Any]]:
    by_id = {str(item.get("id") or ""): item for item in claims if str(item.get("id") or "")}
    pairs: list[dict[str, Any]] = []
    for incoming in claims:
        action = str(incoming.get("action") or "")
        if action not in {"challenge_claim", "supersede_claim"}:
            continue
        existing_id = str(
            incoming.get("conflicts_with_claim_id")
            or incoming.get("supersedes_claim_id")
            or (incoming.get("change") or {}).get("compared_claim_id")
            or ""
        )
        existing = by_id.get(existing_id)
        if not existing:
            continue
        change = incoming.get("change") if isinstance(incoming.get("change"), dict) else {}
        relation_decision = (
            incoming.get("relation_decision")
            if isinstance(incoming.get("relation_decision"), dict)
            else change.get("relation_decision") if isinstance(change.get("relation_decision"), dict)
            else {}
        )
        incoming_source_ids = [str(value) for value in incoming.get("source_packet_ids") or [] if str(value)]
        existing_source_ids = [str(value) for value in existing.get("source_packet_ids") or [] if str(value)]
        pairs.append({
            "id": f"{incoming.get('id', '')}:{existing_id}",
            "relation": "supersedes" if action == "supersede_claim" else "contradicts",
            "reason": str(incoming.get("comparison_reason") or "incoming claim conflicts with an existing claim"),
            "confidence": float(relation_decision.get("confidence") or incoming.get("confidence") or 0),
            "object": {
                "subject": str(
                    relation_decision.get("comparison_subject")
                    or change.get("comparison_subject")
                    or incoming.get("subject")
                    or existing.get("subject")
                    or page_title
                    or ""
                ),
                "aspect": str(
                    relation_decision.get("comparison_aspect")
                    or change.get("comparison_aspect")
                    or incoming.get("aspect")
                    or existing.get("aspect")
                    or "同一知识问题（历史 proposal 未结构化 aspect）"
                ),
                "scope": (
                    relation_decision.get("comparison_scope")
                    if isinstance(relation_decision.get("comparison_scope"), dict)
                    else change.get("comparison_scope") if isinstance(change.get("comparison_scope"), dict)
                    else incoming.get("scope") if isinstance(incoming.get("scope"), dict)
                    else {}
                ),
            },
            "incoming": {
                "claim_id": str(incoming.get("id") or ""),
                "statement": str(incoming.get("statement") or ""),
                "sources": [_source_summary(source_id, pipeline) for source_id in incoming_source_ids],
            },
            "existing": {
                "claim_id": existing_id,
                "statement": str(existing.get("statement") or ""),
                "sources": [_source_summary(source_id, pipeline) for source_id in existing_source_ids],
            },
        })
    return pairs


def _affected_claim_actions(markdown: str) -> dict[str, str]:
    actions: dict[str, str] = {}
    in_section = False
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if line == "## Affected Claims":
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if not in_section or not line.startswith("- action:"):
            continue
        fields: dict[str, str] = {}
        for part in line.removeprefix("-").strip().split(";"):
            key, separator, value = part.strip().partition(":")
            if separator:
                fields[key.strip()] = value.strip()
        claim_id = fields.get("claim_id", "")
        if claim_id:
            actions[claim_id] = fields.get("action", "")
    return actions


def _markdown_section(markdown: str, heading: str) -> str:
    marker = f"## {heading}"
    if marker not in markdown:
        return ""
    body = markdown.split(marker, 1)[1]
    if "\n## " in body:
        body = body.split("\n## ", 1)[0]
    return body.strip()


def _approval_decision_context(
    revision: dict[str, Any],
    affected_claims: list[dict[str, Any]],
    conflict_pairs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    patch = str(revision.get("patch") or "")
    added_lines = [
        line[1:] for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    removed_lines = [
        line[1:] for line in patch.splitlines()
        if line.startswith("-") and not line.startswith("---")
    ]
    new_source_urls: list[str] = []
    for line in added_lines:
        stripped = line.strip()
        if stripped.startswith("- url:"):
            value = stripped.split(":", 1)[1].strip().strip('"')
            if value and value not in new_source_urls:
                new_source_urls.append(value)
    actions: dict[str, int] = {}
    for claim in affected_claims:
        action = str(claim.get("action") or "verify_claim")
        actions[action] = actions.get(action, 0) + 1
    reasons = []
    if conflict_pairs:
        reasons.append("changes_claim_history")
    if any(
        str((item.get("verification") or {}).get("result") or "supported") != "supported"
        for item in affected_claims
    ):
        reasons.append("verifier_not_clean")
    before_markdown = str(revision.get("before_markdown") or "")
    after_markdown = str(revision.get("full_markdown") or "")
    before_summary = _markdown_section(before_markdown, "Summary")
    after_summary = _markdown_section(after_markdown, "Summary")
    return {
        "kind": "update_existing" if revision.get("parent_revision_id") else "create_new",
        "risk_reasons": reasons,
        "line_changes": {"added": len(added_lines), "removed": len(removed_lines)},
        "claim_actions": actions,
        "new_source_urls": new_source_urls,
        "conflict_count": len(conflict_pairs or []),
        "before_summary": before_summary,
        "after_summary": after_summary,
        "summary_changed": bool(before_summary and after_summary and before_summary != after_summary),
    }


def _finalize_run_if_decided(
    run_id: str,
    *,
    runtime: AgentRunStore,
    pipeline: PaperWikiPipelineStore,
    wiki: WikiStore,
    lease_owner: str = "",
) -> list[dict[str, Any]]:
    """Commit a fully-decided batch without confusing intent with durable success.

    Approval moves pending -> accepted when the human clicks approve.  It only becomes
    approved after the revision, deferred metadata effects, and the page reindex have
    all succeeded.  A failure remains visible as commit_failed and can be retried.
    """
    pending = runtime.list_approvals(status="pending", run_id=run_id, limit=500)
    if pending:
        return []
    approvals = runtime.list_approvals(status="", run_id=run_id, limit=500)
    run = runtime.get_run(run_id) or {}
    actionable_statuses = {"accepted", "committing", "commit_failed", "approved"}
    accepted = [item for item in approvals if item["status"] in actionable_statuses]
    committed: list[dict[str, Any]] = []
    if not accepted:
        current = str(run.get("current_state") or "")
        if current == "AWAITING_APPROVAL":
            runtime.transition(run_id, "REJECTED", reason="all proposals rejected")
        _update_approval_job(
            run, wiki=wiki, run_id=run_id, status="rejected", stage="rejected",
            progress=1.0, approval_complete=True,
        )
        return committed

    lease = AgentRunLease(
        runtime, run_id, owner=lease_owner or f"approval-commit-{uuid.uuid4()}",
        ttl_seconds=90, heartbeat_seconds=15,
    )
    if not lease.start():
        raise RunLeaseBusy(f"Agent run {run_id} is already being finalized.")
    manager = _manager(wiki, pipeline)
    trace = TraceRecorder(runtime, run_id)
    active_approval: dict[str, Any] | None = None
    try:
        run = runtime.get_run(run_id) or run
        current_state = str(run.get("current_state") or "")
        if current_state in {"AWAITING_APPROVAL", "COMMIT_FAILED"}:
            runtime.transition(
                run_id, "COMMITTING", reason="all approval decisions collected",
                expected_state=current_state,
            )
        elif current_state not in {"COMMITTING", "REINDEXING", "COMPLETED"}:
            raise InvalidStateTransition(
                f"Run in {current_state} cannot finalize approval commits."
            )
        if current_state == "COMPLETED":
            return committed

        for approval in accepted:
            active_approval = approval
            current_approval = runtime.get_approval(str(approval["id"])) or approval
            if current_approval["status"] == "approved":
                continue
            runtime.mark_approval_committing(str(approval["id"]))
            revision = pipeline.get_revision(str(approval["revision_id"])) or {}
            with trace.span(
                "commit_approved_revision", kind="node",
                tool_name="wiki.commit_proposal",
                input_data={
                    "approval_id": approval["id"],
                    "revision_id": approval["revision_id"],
                    "resume_review_status": revision.get("review_status", ""),
                },
            ) as span:
                if revision.get("review_status") == "committed":
                    result = revision
                    span["output"] = {"already_committed": True}
                else:
                    result = manager.commit_proposal(str(approval["revision_id"]))
                    span["output"] = {
                        "revision_id": approval["revision_id"],
                        "card_id": approval["page_id"],
                    }
                committed.append(result)
            with trace.span(
                "apply_post_commit_effects", kind="tool",
                tool_name="wiki.apply_post_commit_effects",
                input_data={"revision_id": approval["revision_id"]},
            ) as span:
                span["output"] = manager.apply_post_commit_effects(
                    str(approval["revision_id"])
                )
            card = wiki.get_card(str(approval["page_id"]))
            if card:
                get_chunk_index().reindex_card(
                    card_id=str(card["id"]),
                    markdown_path=str(card.get("markdown_path") or ""),
                    source_kind="paper_pdf",
                )
            runtime.complete_approval(str(approval["id"]))

        current_state = str((runtime.get_run(run_id) or {}).get("current_state") or "")
        if current_state == "COMMITTING":
            runtime.transition(run_id, "REINDEXING", reason="approved revisions committed and indexed")
        if str((runtime.get_run(run_id) or {}).get("current_state") or "") == "REINDEXING":
            runtime.transition(
                run_id, "COMPLETED",
                result={
                    "approved": len([item for item in approvals if item["status"] in actionable_statuses]),
                    "rejected": len([item for item in approvals if item["status"] == "rejected"]),
                },
                error="",
                reason="approval workflow completed",
            )
        _update_approval_job(
            run, wiki=wiki, run_id=run_id, status="done", stage="done",
            progress=1.0, approval_complete=True,
        )
        return committed
    except Exception as exc:
        error = str(exc)
        for approval in accepted:
            current = runtime.get_approval(str(approval["id"])) or {}
            if current.get("status") in {"accepted", "committing", "commit_failed"}:
                runtime.fail_approval(str(approval["id"]), error)
        current_state = str((runtime.get_run(run_id) or {}).get("current_state") or "")
        if current_state in {"COMMITTING", "REINDEXING"}:
            runtime.transition(
                run_id, "COMMIT_FAILED", error=error,
                context_updates={
                    "failed_approval_id": str((active_approval or {}).get("id") or ""),
                    "commit_error": error,
                },
                reason="revision commit, deferred effects, or reindex failed",
            )
        _update_approval_job(
            run, wiki=wiki, run_id=run_id, status="waiting", stage="commit_failed",
            progress=0.9, error=error,
        )
        raise
    finally:
        lease.stop()


def _update_approval_job(
    run: dict[str, Any],
    *,
    wiki: WikiStore,
    run_id: str,
    status: str,
    stage: str,
    progress: float,
    approval_complete: bool = False,
    error: str = "",
) -> None:
    job_id = str(run.get("ingestion_job_id") or (run.get("context") or {}).get("job_id") or "")
    if job_id:
        IngestionJobStore(db_path=wiki.db_path).update_job(
            job_id, status=status, stage=stage, progress=progress, error=error,
            result={
                **(run.get("result") or {}), "agent_run_id": run_id,
                "approval_complete": approval_complete,
            },
        )

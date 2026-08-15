from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.api import agent_runs as agent_runs_api
from backend.api import papers as papers_api
from backend.api import wiki as wiki_api
from backend.api.agent_runs import ApprovalDecisionPayload, RetryPayload, _normalize_bbox
from system.agent_runtime import AgentRunStore, InvalidStateTransition, TraceRecorder
from system.core.llm_call import invoke_structured
from system.storage import object_storage as object_storage_module
from system.storage.object_storage import ObjectStorage
from system.wiki.markdown_reindexer import MarkdownWikiReindexer
from system.wiki.markdown_vault import MarkdownVault
from system.wiki.paper_pipeline.merger import PaperMergeAgent
from system.wiki.paper_pipeline.models import SourcePacket, SourceTable
from system.wiki.paper_pipeline.store import PaperWikiPipelineStore
from system.wiki.evidence_verifier import EvidenceVerifier
from system.wiki.ingestion_jobs import IngestionJobStore
from system.wiki.revision import WikiRevisionManager
from system.wiki.wiki_store import WikiStore


def _use_local_object_storage() -> None:
    storage = ObjectStorage()
    storage.backend = "local"
    storage.root_prefix = ""
    object_storage_module._STORAGE = storage


def test_agent_state_machine_persists_checkpoints_approvals_and_trace() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        runtime = AgentRunStore(db_path=str(Path(tmp) / "runtime.db"))
        run = runtime.create_run(
            run_type="paper_ingestion",
            source_uri="origin/paper.pdf",
            ingestion_job_id="job-1",
            approval_mode="manual",
        )
        run_id = str(run["id"])

        with pytest.raises(InvalidStateTransition):
            runtime.transition(run_id, "VERIFYING")

        for state in (
            "EXTRACTING",
            "DISTILLING",
            "VERIFYING",
            "COMPILING_PROPOSAL",
            "AWAITING_APPROVAL",
        ):
            runtime.transition(run_id, state, context_updates={"last_node": state})

        approval = runtime.create_approval(
            run_id=run_id,
            revision_id="revision-1",
            page_id="page-1",
            title="Paper page",
        )
        assert runtime.get_run(run_id)["status"] == "waiting"
        assert runtime.get_run(run_id)["context"]["last_node"] == "AWAITING_APPROVAL"
        assert runtime.list_approvals(run_id=run_id)[0]["id"] == approval["id"]

        class FakeLLM:
            model = "test-entailment-model"

            def invoke(self, prompt: str, **kwargs) -> str:
                return '{"label":"entailed"}'

        trace = TraceRecorder(runtime, run_id)
        with trace.bind():
            assert "entailed" in invoke_structured(
                FakeLLM(), "verify claim-1", max_tokens=100
            )

        runtime.decide_approval(str(approval["id"]), status="approved", reason="reviewed")
        with pytest.raises(ValueError, match="pending approval"):
            runtime.decide_approval(str(approval["id"]), status="rejected")
        for state in ("COMMITTING", "REINDEXING", "COMPLETED"):
            runtime.transition(run_id, state)

        events = runtime.list_events(run_id)
        assert [item["sequence"] for item in events] == list(range(1, len(events) + 1))
        assert any(item["event_type"] == "model.completed" for item in events)
        assert runtime.get_run(run_id)["current_state"] == "COMPLETED"
        assert runtime.find_by_ingestion_job("job-1")["id"] == run_id


def test_proposal_does_not_mutate_wiki_until_explicit_commit() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        _use_local_object_storage()
        root = Path(tmp)
        db_path = str(root / "approval.db")
        wiki = WikiStore(db_path=db_path)
        pipeline = PaperWikiPipelineStore(db_path=db_path)
        manager = WikiRevisionManager(
            store=pipeline,
            vault=MarkdownVault(vault_dir=str(root / "wiki")),
            reindexer=MarkdownWikiReindexer(db_path=db_path),
        )

        proposal = manager.propose_card(
            card_id="approval-page",
            title="Approval Page",
            page_type="ConceptPage",
            content_json={"definition": "Visible only after approval."},
            summary="Frozen proposal.",
            source_level="primary",
            source_urls=[],
            related_topics=[],
        )
        merge_agent = PaperMergeAgent(
            pipeline_store=pipeline, wiki_store=wiki, approval_mode="manual"
        )
        merge_agent.proposals = [proposal]
        merge_agent._apply_or_defer_effect(
            "approval-page",
            "aliases",
            {"card_id": "approval-page", "aliases": ["approval alias"]},
        )
        merge_agent._apply_or_defer_effect(
            "approval-page",
            "source",
            {
                "card_id": "approval-page",
                "source_packet_id": "source-1",
                "source_card_id": "approval-page",
                "claim_text": "Visible only after approval.",
            },
        )

        assert proposal["review_status"] == "proposed"
        assert wiki.get_card("approval-page") is None
        assert manager.vault.read_reference(str(proposal["target_markdown_path"])) == ""
        assert pipeline.find_card_by_alias(["approval alias"]) is None
        with pipeline._connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM wiki_card_sources WHERE card_id='approval-page'"
            ).fetchone()[0] == 0

        manager.commit_proposal(str(proposal["revision_id"]))
        effects = manager.apply_post_commit_effects(str(proposal["revision_id"]))
        assert effects["applied"] == 2
        assert manager.apply_post_commit_effects(str(proposal["revision_id"])) == effects
        committed = wiki.get_card("approval-page")
        assert committed is not None
        assert committed["summary"] == "Frozen proposal."
        assert pipeline.find_card_by_alias(["approval alias"])["card_id"] == "approval-page"
        with pipeline._connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM wiki_card_sources WHERE card_id='approval-page'"
            ).fetchone()[0] == 1


def test_commit_rejects_a_stale_human_approval() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        _use_local_object_storage()
        root = Path(tmp)
        db_path = str(root / "stale.db")
        wiki = WikiStore(db_path=db_path)
        pipeline = PaperWikiPipelineStore(db_path=db_path)
        manager = WikiRevisionManager(
            store=pipeline,
            vault=MarkdownVault(vault_dir=str(root / "wiki")),
            reindexer=MarkdownWikiReindexer(db_path=db_path),
        )
        manager.commit_card(
            card_id="page-1",
            title="Page",
            page_type="ConceptPage",
            content_json={"definition": "v1"},
            summary="v1",
            source_level="primary",
            source_urls=[],
            related_topics=[],
        )
        current = wiki.get_card("page-1")
        stale = manager.propose_card(
            card_id="page-1",
            title="Page",
            page_type="ConceptPage",
            content_json={"definition": "stale"},
            summary="stale",
            source_level="primary",
            source_urls=[],
            related_topics=[],
            existing_card=current,
        )
        manager.commit_card(
            card_id="page-1",
            title="Page",
            page_type="ConceptPage",
            content_json={"definition": "v2"},
            summary="v2",
            source_level="primary",
            source_urls=[],
            related_topics=[],
            existing_card=current,
        )

        with pytest.raises(ValueError, match="Stale proposal"):
            manager.commit_proposal(str(stale["revision_id"]))
        assert wiki.get_card("page-1")["summary"] == "v2"


def test_docling_bbox_is_converted_to_pdf_overlay_coordinates() -> None:
    normalized = _normalize_bbox(
        {"l": 20, "t": 300, "r": 120, "b": 250},
        {"width": 500, "height": 800},
    )
    assert normalized == {
        "x": 0.04,
        "y": 0.625,
        "width": 0.2,
        "height": 0.0625,
    }


def test_manual_link_only_effects_are_owned_by_paper_proposal() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        _use_local_object_storage()
        root = Path(tmp)
        db_path = str(root / "link-only.db")
        wiki = WikiStore(db_path=db_path)
        pipeline = PaperWikiPipelineStore(db_path=db_path)
        manager = WikiRevisionManager(
            store=pipeline,
            vault=MarkdownVault(vault_dir=str(root / "wiki")),
            reindexer=MarkdownWikiReindexer(db_path=db_path),
        )
        manager.commit_card(
            card_id="existing-concept", title="Existing Concept", page_type="ConceptPage",
            content_json={"definition": "Existing."}, summary="Existing.",
            source_level="primary", source_urls=[], related_topics=[],
        )
        paper = manager.propose_card(
            card_id="paper-proposal", title="Paper", page_type="PaperPage",
            content_json={"problem": "Paper."}, summary="Paper.",
            source_level="primary", source_urls=[], related_topics=[],
        )
        agent = PaperMergeAgent(
            pipeline_store=pipeline, wiki_store=wiki, approval_mode="manual"
        )
        agent.proposals = [{**paper, "page_type": "PaperPage"}]
        agent._apply_or_defer_effect(
            "existing-concept",
            "source",
            {
                "card_id": "existing-concept", "source_packet_id": "packet-1",
                "source_card_id": "paper-proposal", "claim_text": "linked evidence",
            },
        )
        agent._apply_or_defer_effect(
            "existing-concept",
            "link",
            {
                "from_card_id": "paper-proposal", "to_card_id": "existing-concept",
                "relation_type": "introduces", "source_packet_id": "packet-1",
            },
        )
        with pipeline._connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM wiki_card_sources WHERE source_packet_id='packet-1'"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM wiki_card_links WHERE source_packet_id='packet-1'"
            ).fetchone()[0] == 0
        frozen = pipeline.get_revision(str(paper["revision_id"]))
        assert len(frozen["verification"]["post_commit_effects"]) == 2

        manager.commit_proposal(str(paper["revision_id"]))
        effects = manager.apply_post_commit_effects(str(paper["revision_id"]))
        assert effects["applied"] == 2
        with pipeline._connect() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM wiki_card_sources WHERE source_packet_id='packet-1'"
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM wiki_card_links WHERE source_packet_id='packet-1'"
            ).fetchone()[0] == 1


def test_manual_mode_rejects_side_effect_without_any_proposal() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db_path = str(Path(tmp) / "unowned-effect.db")
        agent = PaperMergeAgent(
            pipeline_store=PaperWikiPipelineStore(db_path=db_path),
            wiki_store=WikiStore(db_path=db_path),
            approval_mode="manual",
        )
        with pytest.raises(RuntimeError, match="unowned source side effect"):
            agent._apply_or_defer_effect(
                "existing", "source", {"card_id": "existing", "source_packet_id": "packet"}
            )


def test_approval_api_waits_for_all_decisions_then_commits_batch(monkeypatch) -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        _use_local_object_storage()
        root = Path(tmp)
        db_path = str(root / "api-approval.db")
        wiki = WikiStore(db_path=db_path)
        wiki.vault = MarkdownVault(vault_dir=str(root / "wiki"))
        pipeline = PaperWikiPipelineStore(db_path=db_path)
        manager = WikiRevisionManager(
            store=pipeline,
            vault=wiki.vault,
            reindexer=MarkdownWikiReindexer(db_path=db_path),
        )
        proposals = [
            manager.propose_card(
                card_id=f"page-{index}",
                title=f"Page {index}",
                page_type="ConceptPage",
                content_json={"definition": f"definition {index}"},
                summary=f"summary {index}",
                source_level="primary",
                source_urls=[],
                related_topics=[],
            )
            for index in (1, 2)
        ]
        runtime = AgentRunStore(db_path=db_path)
        run = runtime.create_run(run_type="paper_ingestion", approval_mode="manual")
        for state in (
            "EXTRACTING",
            "DISTILLING",
            "VERIFYING",
            "COMPILING_PROPOSAL",
            "AWAITING_APPROVAL",
        ):
            runtime.transition(str(run["id"]), state)
        approvals = [
            runtime.create_approval(
                run_id=str(run["id"]),
                revision_id=str(proposal["revision_id"]),
                page_id=str(proposal["card_id"]),
                title=str(proposal["card_id"]),
            )
            for proposal in proposals
        ]

        class FakeChunkIndex:
            def __init__(self) -> None:
                self.cards: list[str] = []

            def reindex_card(self, *, card_id: str, **kwargs):
                self.cards.append(card_id)

        chunks = FakeChunkIndex()
        monkeypatch.setattr(agent_runs_api, "get_chunk_index", lambda: chunks)

        first = agent_runs_api.approve_revision(
            str(approvals[0]["id"]), ApprovalDecisionPayload(reason="first"), wiki=wiki
        )
        assert first["committed"] == []
        assert wiki.get_card("page-1") is None
        assert runtime.get_run(str(run["id"]))["current_state"] == "AWAITING_APPROVAL"

        second = agent_runs_api.approve_revision(
            str(approvals[1]["id"]), ApprovalDecisionPayload(reason="second"), wiki=wiki
        )
        assert len(second["committed"]) == 2
        assert wiki.get_card("page-1") is not None
        assert wiki.get_card("page-2") is not None
        assert set(chunks.cards) == {"page-1", "page-2"}
        assert runtime.get_run(str(run["id"]))["current_state"] == "COMPLETED"


def test_commit_failure_stays_visible_and_retries_idempotently(monkeypatch) -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        _use_local_object_storage()
        root = Path(tmp)
        db_path = str(root / "retryable-approval.db")
        wiki = WikiStore(db_path=db_path)
        wiki.vault = MarkdownVault(vault_dir=str(root / "wiki"))
        pipeline = PaperWikiPipelineStore(db_path=db_path)
        manager = WikiRevisionManager(
            store=pipeline,
            vault=wiki.vault,
            reindexer=MarkdownWikiReindexer(db_path=db_path),
        )
        proposal = manager.propose_card(
            card_id="retry-page", title="Retry Page", page_type="ConceptPage",
            content_json={"definition": "Retryable."}, summary="Retryable.",
            source_level="primary", source_urls=[], related_topics=[],
        )
        runtime = AgentRunStore(db_path=db_path)
        run = runtime.create_run(run_type="paper_ingestion", approval_mode="manual")
        for state in (
            "EXTRACTING", "DISTILLING", "VERIFYING",
            "COMPILING_PROPOSAL", "AWAITING_APPROVAL",
        ):
            runtime.transition(str(run["id"]), state)
        approval = runtime.create_approval(
            run_id=str(run["id"]), revision_id=str(proposal["revision_id"]),
            page_id="retry-page", title="Retry Page",
        )

        class FakeChunkIndex:
            def __init__(self) -> None:
                self.cards: list[str] = []

            def reindex_card(self, *, card_id: str, **kwargs):
                self.cards.append(card_id)

        chunks = FakeChunkIndex()
        monkeypatch.setattr(agent_runs_api, "get_chunk_index", lambda: chunks)
        original_effects = WikiRevisionManager.apply_post_commit_effects
        attempts = {"count": 0}

        def flaky_effects(self, revision_id: str):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise OSError("simulated metadata write outage")
            return original_effects(self, revision_id)

        monkeypatch.setattr(WikiRevisionManager, "apply_post_commit_effects", flaky_effects)
        with pytest.raises(HTTPException) as failed:
            agent_runs_api.approve_revision(
                str(approval["id"]), ApprovalDecisionPayload(reason="approve"), wiki=wiki
            )
        assert failed.value.status_code == 503
        assert wiki.get_card("retry-page") is not None
        assert runtime.get_approval(str(approval["id"]))["status"] == "commit_failed"
        assert "simulated metadata" in runtime.get_approval(str(approval["id"]))["commit_error"]
        assert runtime.get_run(str(run["id"]))["current_state"] == "COMMIT_FAILED"
        assert runtime.list_approvals(status="action_required", run_id=str(run["id"]))

        retried = agent_runs_api.retry_approval_commit(
            str(approval["id"]), RetryPayload(reason="retry"), wiki=wiki
        )
        assert retried["ok"] is True
        assert runtime.get_approval(str(approval["id"]))["status"] == "approved"
        assert runtime.get_run(str(run["id"]))["current_state"] == "COMPLETED"
        assert chunks.cards == ["retry-page"]


def test_expired_worker_lease_becomes_recoverable_and_restarts_from_source() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        runtime = AgentRunStore(db_path=str(Path(tmp) / "lease.db"))
        run = runtime.create_run(
            run_type="paper_ingestion", source_uri="origin/paper.pdf",
            context={"job_id": "job-lease", "pdf_path": "origin/paper.pdf"},
        )
        run_id = str(run["id"])
        runtime.transition(run_id, "EXTRACTING")
        assert runtime.acquire_lease(run_id, "worker-a", ttl_seconds=90)
        assert not any(item["id"] == run_id for item in runtime.list_recoverable_runs())
        with runtime._connect() as conn:
            conn.execute(
                "UPDATE agent_runs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                (run_id,),
            )
            conn.commit()
        assert any(item["id"] == run_id for item in runtime.list_recoverable_runs())
        restarted = runtime.restart_run(run_id, reason="test crash recovery")
        assert restarted["current_state"] == "QUEUED"
        assert restarted["attempt"] == 1
        assert restarted["resume_from_state"] == "EXTRACTING"
        assert restarted["context"]["resume_from_state"] == "EXTRACTING"


def test_whole_docling_table_is_resolvable_numeric_evidence() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store = PaperWikiPipelineStore(db_path=str(Path(tmp) / "table-evidence.db"))
        packet = SourcePacket(
            source_id="source-table", title="Minerva", source_hash="hash-table",
            parser_used="docling-remote", blocks=[{"text": "Table 3 results"}],
            elements=[], docling_json={"name": "DoclingDocument"},
            tables=[SourceTable(
                table_id="table-3", element_id="table-element-3", page=8,
                caption="Table 3: majority voting uses k = 256 samples for MATH.",
                headers=[["Model", "MATH"]],
                rows=[["Minerva 540B, maj1@k", "50 . 3%"]],
                markdown=(
                    "| Model | MATH |\n| --- | --- |\n"
                    "| Minerva 540B, maj1@k | 50 . 3% |"
                ),
                bbox={"l": 10, "t": 100, "r": 200, "b": 20},
            )],
        )
        store.upsert_source_packet(packet)
        matches = store.find_evidence(
            "source-table", text="Table 3: Minerva 540B, maj1@k: 50.3%", limit=1
        )
        assert matches[0]["id"] == "table-3"
        result = EvidenceVerifier(store).verify_claim(
            statement="Minerva 540B 在 MATH 上通过多数投票达到 50.3%。",
            evidence_excerpt="Table 3: Minerva 540B, maj1@k: 50.3%",
            evidence_ids=["table-3"], structured_required=True,
        )
        assert result.result == "supported"
        assert result.evidence[0]["kind"] == "table"
        assert result.evidence[0]["page"] == 8


def test_pipeline_result_false_is_rejected_not_completed(monkeypatch) -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db_path = str(Path(tmp) / "rejected-result.db")
        jobs = IngestionJobStore(db_path=db_path)
        runtime = AgentRunStore(db_path=db_path)
        pdf = Path(tmp) / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        job = jobs.create_job(source_type="paper_pdf", source_uri=str(pdf))
        run = runtime.create_run(
            run_type="paper_ingestion", source_uri=str(pdf), approval_mode="auto",
            ingestion_job_id=str(job["id"]),
            context={"job_id": job["id"], "pdf_path": str(pdf), "pipeline": "four_agent"},
        )

        def fake_pipeline(**kwargs):
            callback = kwargs["stage_callback"]
            for state in ("EXTRACTING", "DISTILLING", "VERIFYING", "COMPILING_PROPOSAL"):
                callback(state, {})
            return {
                "ok": False, "source_packet_id": "source-rejected",
                "paper_card_id": "", "proposals": [], "review_rejections": ["unsupported"],
            }

        monkeypatch.setattr(papers_api, "_run_pdf_pipeline", fake_pipeline)
        papers_api._run_ingestion_job(
            db_path=db_path, job_id=str(job["id"]), pdf_path=str(pdf),
            source_url="", pipeline="four_agent", run_id=str(run["id"]),
            approval_mode="auto", run_maintenance=False,
        )
        assert runtime.get_run(str(run["id"]))["current_state"] == "REJECTED"
        assert jobs.get_job(str(job["id"]))["status"] == "rejected"


def test_risk_policy_only_escalates_exceptional_proposals() -> None:
    clean = {
        "review_status": "proposed",
        "parent_revision_id": "",
        "verification": {
            "claims": [{"statement": "Evidence-backed", "relation": "supports"}],
            "checks": [{"result": "supported", "semantic_result": "entailed"}],
        },
    }
    assert papers_api._proposal_risk_reasons(clean) == []
    assert papers_api._proposal_risk_reasons(
        {**clean, "parent_revision_id": "previous-revision"}
    ) == []
    assert "changes_claim_history" in papers_api._proposal_risk_reasons({
        **clean,
        "verification": {
            "claims": [{"statement": "Contradicts old claim", "relation": "challenges"}],
            "affected_claims": [{"claim_id": "new-claim", "action": "challenge_claim", "compared_claim_id": "old-claim"}],
            "checks": [{"result": "supported", "semantic_result": "entailed"}],
        },
    })
    assert "verifier_not_clean" in papers_api._proposal_risk_reasons({
        **clean,
        "verification": {
            "claims": [{"statement": "Uncertain", "relation": "supports"}],
            "checks": [{"result": "unsupported", "semantic_result": "insufficient"}],
        },
    })


def test_identical_pdf_is_reported_as_already_ingested(monkeypatch) -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        db_path = str(root / "dedupe.db")
        pdf = root / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4\nidentical-pdf\n")
        packet = SourcePacket(
            source_id="source-existing",
            title="Existing paper",
            source_hash=papers_api._file_sha256(pdf),
            parser_used="docling",
        )
        PaperWikiPipelineStore(db_path=db_path).upsert_source_packet(packet)
        result = papers_api._existing_pdf_ingestion(pdf, db_path)
        assert result is not None
        assert result["already_exists"] is True
        assert result["source_packet_id"] == "source-existing"
        assert result["title"] == "Existing paper"
        monkeypatch.setattr(wiki_api, "PAPER_REPO_ROOT", root)
        monkeypatch.setattr(wiki_api, "PAPER_UPLOAD_DIR", root / "uploads")
        response = wiki_api.create_wiki_ingestion_job(
            file=UploadFile(filename="paper.pdf", file=io.BytesIO(pdf.read_bytes())),
            local_path="",
            source_url="",
            pipeline="four_agent",
            approval_mode="risk",
            store=WikiStore(db_path=db_path),
        )
        assert response["already_exists"] is True
        assert IngestionJobStore(db_path=db_path).list_jobs() == []


def test_conflict_pair_identifies_incoming_and_existing_sources() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db_path = str(Path(tmp) / "conflict.db")
        pipeline = PaperWikiPipelineStore(db_path=db_path)
        pipeline.upsert_source_packet(SourcePacket(source_id="paper-new", title="New paper"))
        pipeline.upsert_source_packet(SourcePacket(source_id="paper-old", title="Existing paper"))
        claims = [
            {
                "id": "claim-old", "statement": "The method requires output tokens.",
                "source_packet_ids": ["paper-old"],
            },
            {
                "id": "claim-new", "statement": "The method does not require output tokens.",
                "source_packet_ids": ["paper-new"], "action": "challenge_claim",
                "conflicts_with_claim_id": "claim-old", "comparison_reason": "opposite polarity",
                "confidence": 0.92,
            },
        ]
        pairs = agent_runs_api._conflict_pairs(claims, pipeline)
        assert len(pairs) == 1
        assert pairs[0]["incoming"]["sources"][0]["title"] == "New paper"
        assert pairs[0]["existing"]["sources"][0]["title"] == "Existing paper"
        assert pairs[0]["incoming"]["statement"] != pairs[0]["existing"]["statement"]

"""Regression: incomplete semantic comparisons must never become auto commits."""
import json

import pytest

from backend.api import agent_runs as approvals_api, papers as papers_api
from system.agent_runtime import AgentRunStore
from system.wiki.claim_relation_resolver import ClaimRelationResolver
from system.wiki.ingestion_jobs import IngestionJobStore
from system.wiki.paper_pipeline.models import CandidateClaim
from system.wiki.paper_pipeline.store import PaperWikiPipelineStore
from system.wiki.wiki_store import WikiStore


def candidate():
    return CandidateClaim(claim="System X uses a shared cache.", evidence="Shared cache", subject="System X")


def existing(count=1):
    return [{"id": f"old-{i}", "statement": f"System X cache observation {i}",
             "subject": "System X", "_retrieval_routes": ["claim_vector"]} for i in range(count)]


def test_incomplete_relation_output_retries_then_requires_review():
    class Model:
        calls = 0
        def invoke(self, prompt, **kwargs):
            self.calls += 1
            return '{"decisions": []}'
    model = Model()
    result = ClaimRelationResolver(model).resolve(page_title="System X", incoming_claims=[candidate()], existing_claims=existing())[0]
    assert model.calls == 2
    assert result.requires_review
    assert result.relation == "uncertain"
    assert result.resolution_status == "model_failed"


def test_retrieval_failure_is_not_treated_as_empty_knowledge_base():
    class Retriever:
        def retrieve(self, **kwargs):
            raise RuntimeError("index unavailable")
    result = ClaimRelationResolver(retriever=Retriever()).resolve(page_title="System X", incoming_claims=[candidate()], existing_claims=[])[0]
    assert result.requires_review
    assert result.resolution_status == "retrieval_failed"


def test_no_model_cannot_auto_approve_unresolved_relation():
    result = ClaimRelationResolver().resolve(page_title="System X", incoming_claims=[candidate()], existing_claims=existing())[0]
    assert result.requires_review
    assert result.resolution_status == "model_unavailable"


def test_successful_pair_does_not_hide_a_failed_comparison(monkeypatch):
    monkeypatch.setenv("PAPERWIKI_RELATION_BATCH_PAIRS", "1")
    class Model:
        def invoke(self, prompt, **kwargs):
            batch_text = prompt.split("Comparison batch:\n", 1)[1].split("\nPrevious response", 1)[0]
            pair = json.loads(batch_text)["pairs"][0]
            if pair["existing_claim_id"] == "old-1":
                raise RuntimeError("comparison failed")
            return json.dumps({"decisions": [{**pair, "relation": "equivalent", "confidence": 1,
                "same_entity": True, "same_aspect": True, "scope_overlap": True}]})
    result = ClaimRelationResolver(Model()).resolve(page_title="System X", incoming_claims=[candidate()], existing_claims=existing(2))[0]
    assert result.requires_review
    assert result.existing_claim_id == "old-1"
    assert result.resolution_status == "model_failed"


def test_comparison_batches_preserve_every_candidate(monkeypatch):
    monkeypatch.setenv("PAPERWIKI_RELATION_BATCH_PAIRS", "2")
    class Model:
        pairs = []
        def invoke(self, prompt, **kwargs):
            batch = json.loads(prompt.split("Comparison batch:\n", 1)[1])
            self.pairs.extend(batch["pairs"])
            return json.dumps({"decisions": [{**pair, "relation": "complements", "confidence": .9,
                "same_entity": True, "same_aspect": False, "scope_overlap": True} for pair in batch["pairs"]]})
    model = Model()
    results = ClaimRelationResolver(model).resolve(page_title="System X", incoming_claims=[candidate()], existing_claims=existing(5))
    assert len(model.pairs) == 5
    assert {pair["existing_claim_id"] for pair in model.pairs} == {f"old-{i}" for i in range(5)}
    assert not results[0].requires_review


@pytest.mark.parametrize("approval_mode", ["auto", "risk", "manual"])
def test_unresolved_addition_waits_for_approval_and_is_visible(tmp_path, monkeypatch, approval_mode):
    db_path = str(tmp_path / "approval.db")
    pipeline = PaperWikiPipelineStore(db_path=db_path)
    revision_id = pipeline.create_revision(
        page_id="new-page", parent_revision_id="", patch="", before_markdown="", full_markdown="",
        reason="test incomplete relation", source_ids=[], verification={
            "claims": [{"id": "new-claim", "statement": "Supported by source"}],
            "checks": [{"claim_id": "new-claim", "result": "supported", "semantic_result": "entailed"}],
            "affected_claims": [{"claim_id": "new-claim", "action": "add_claim", "requires_review": True,
                "relation_decision": {"relation": "uncertain", "resolution_status": "model_failed", "requires_review": True}}],
        },
    )
    jobs = IngestionJobStore(db_path=db_path)
    runtime = AgentRunStore(db_path=db_path)
    job = jobs.create_job(source_type="paper_pdf", source_uri="test.pdf")
    run = runtime.create_run(run_type="paper_ingestion", approval_mode=approval_mode, ingestion_job_id=job["id"])

    def compile_proposal(**kwargs):
        for state in ("EXTRACTING", "DISTILLING", "VERIFYING", "COMPILING_PROPOSAL"):
            kwargs["stage_callback"](state, {})
        return {"ok": True, "proposals": [{"revision_id": revision_id, "card_id": "new-page", "title": "Test"}]}

    monkeypatch.setattr(papers_api, "_run_pdf_pipeline", compile_proposal)
    papers_api._run_ingestion_job(db_path=db_path, job_id=job["id"], pdf_path="test.pdf",
        source_url="", pipeline="four_agent", run_id=run["id"], approval_mode=approval_mode, run_maintenance=False)
    assert runtime.get_run(run["id"])["current_state"] == "AWAITING_APPROVAL"
    assert jobs.get_job(job["id"])["status"] == "waiting"
    assert pipeline.get_revision(revision_id)["review_status"] == "proposed"
    items = approvals_api.list_approvals(status="action_required", run_id=run["id"], limit=10, wiki=WikiStore(db_path=db_path))["items"]
    assert len(items) == 1
    assert not items[0]["conflict_pairs"]
    assert "claim_relation_model_failed" in items[0]["decision_context"]["risk_reasons"]


def test_transport_failure_stops_remaining_batches_and_later_pages(monkeypatch):
    import requests
    monkeypatch.setenv("PAPERWIKI_RELATION_BATCH_PAIRS", "1")
    class Model:
        calls = 0
        def invoke(self, prompt, **kwargs):
            self.calls += 1
            assert kwargs["max_attempts"] == 1
            raise requests.exceptions.ReadTimeout("provider unavailable")
    model = Model()
    resolver = ClaimRelationResolver(model)
    for _ in range(2):
        result = resolver.resolve(page_title="System X", incoming_claims=[candidate()], existing_claims=existing(3))
        assert result[0].requires_review
        assert result[0].resolution_status == "model_failed"
    assert model.calls == 1

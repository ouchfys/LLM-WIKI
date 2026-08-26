from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from system.document.docling_evidence import normalize_docling_document
from system.storage import object_storage as object_storage_module
from system.storage.object_storage import ObjectStorage
from system.wiki.evidence_verifier import EvidenceVerifier
from system.wiki.hierarchical_compiler import HierarchicalWikiCompiler
from system.wiki.claim_relation_resolver import ClaimRelationResolver
from system.wiki.markdown_reindexer import MarkdownWikiReindexer
from system.wiki.markdown_parser import content_json_from_sections, parse_markdown_card
from system.wiki.markdown_vault import MarkdownVault
from system.wiki.paper_pipeline.models import (
    CandidateClaim,
    DistilledCandidate,
    SourceElement,
    SourcePacket,
    SourceTable,
)
from system.wiki.paper_pipeline.store import PaperWikiPipelineStore
from system.wiki.revision import RevisionRejectedError, WikiRevisionManager
from system.wiki.table_qa import ReadOnlyTableEngine, TableQuestionAnswerer, TableResolver
from system.wiki.wiki_store import WikiStore


DOCLING_FIXTURE = {
    "texts": [
        {
            "self_ref": "#/texts/0",
            "label": "section_header",
            "text": "Experiments",
            "level": 1,
            "prov": [{"page_no": 4, "bbox": {"l": 20, "t": 30, "r": 300, "b": 50}}],
        },
        {
            "self_ref": "#/texts/1",
            "label": "text",
            "text": "Model A reaches 91.2 on GSM8K.",
            "prov": [{"page_no": 4, "bbox": {"l": 20, "t": 60, "r": 400, "b": 90}}],
        },
        {
            "self_ref": "#/texts/2",
            "label": "caption",
            "text": "Table 2: Main results",
            "prov": [{"page_no": 4, "bbox": {"l": 20, "t": 100, "r": 300, "b": 120}}],
        },
    ],
    "tables": [
        {
            "self_ref": "#/tables/0",
            "label": "table",
            "captions": [{"$ref": "#/texts/2"}],
            "prov": [{"page_no": 4, "bbox": {"l": 20, "t": 130, "r": 500, "b": 300}}],
            "data": {
                "num_rows": 2,
                "num_cols": 2,
                "table_cells": [
                    {"start_row_offset_idx": 0, "end_row_offset_idx": 1, "start_col_offset_idx": 0, "end_col_offset_idx": 1, "text": "Model", "column_header": True},
                    {"start_row_offset_idx": 0, "end_row_offset_idx": 1, "start_col_offset_idx": 1, "end_col_offset_idx": 2, "text": "GSM8K", "column_header": True},
                    {"start_row_offset_idx": 1, "end_row_offset_idx": 2, "start_col_offset_idx": 0, "end_col_offset_idx": 1, "text": "Model A", "row_header": True},
                    {"start_row_offset_idx": 1, "end_row_offset_idx": 2, "start_col_offset_idx": 1, "end_col_offset_idx": 2, "text": "91.2"},
                ],
            },
        }
    ],
}


class FakeSemanticLLM:
    def invoke(self, prompt: str, **kwargs) -> str:
        if "not improve" in prompt:
            return '{"label":"contradicted","score":0.98,"reason":"source states an improvement"}'
        return '{"label":"entailed","score":0.96,"reason":"claim follows from the persisted source span"}'


class CapturingSemanticLLM(FakeSemanticLLM):
    def __init__(self) -> None:
        self.prompt = ""

    def invoke(self, prompt: str, **kwargs) -> str:
        self.prompt = prompt
        return super().invoke(prompt, **kwargs)


def _storage() -> None:
    storage = ObjectStorage()
    storage.backend = "local"
    storage.root_prefix = ""
    object_storage_module._STORAGE = storage


def _packet(source_id: str = "packet-1") -> SourcePacket:
    elements, tables = normalize_docling_document(DOCLING_FIXTURE, source_key="fixture-hash")
    return SourcePacket(
        source_id=source_id,
        title="Evidence Paper",
        source_hash="fixture-hash",
        parser_used="docling-local",
        docling_json=DOCLING_FIXTURE,
        elements=[SourceElement(**item) for item in elements],
        tables=[SourceTable(**item) for item in tables],
    )


def test_docling_evidence_keeps_table_cells_page_bbox_and_headers() -> None:
    packet = _packet()
    assert packet.elements[1].page == 4
    assert packet.elements[1].bbox["l"] == 20.0
    assert packet.tables[0].caption == "Table 2: Main results"
    assert packet.tables[0].headers == [["Model", "GSM8K"]]
    assert packet.tables[0].rows == [["Model A", "91.2"]]
    assert "| Model | GSM8K |" in packet.tables[0].markdown
    assert packet.tables[0].cells[-1].cell_id.startswith("ev-")


def test_store_and_verifier_reread_persisted_source_and_reject_wrong_number() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store = PaperWikiPipelineStore(db_path=str(Path(tmp) / "evidence.db"))
        packet = _packet()
        store.upsert_source_packet(packet)
        paragraph = next(item for item in packet.elements if "91.2" in item.text)
        verifier = EvidenceVerifier(store)

        supported = verifier.verify_claim(
            statement="Model A reaches 91.2 on GSM8K.",
            evidence_excerpt="Model A reaches 91.2 on GSM8K.",
            evidence_ids=[paragraph.element_id],
            structured_required=True,
        )
        rejected = verifier.verify_claim(
            statement="Model A reaches 99.9 on GSM8K.",
            evidence_excerpt="Model A reaches 91.2 on GSM8K.",
            evidence_ids=[paragraph.element_id],
            structured_required=True,
        )
        assert supported.result == "supported"
        assert supported.evidence[0]["page"] == 4
        assert rejected.result == "unsupported"
        assert "99.9" in rejected.reason

        semantic = EvidenceVerifier(store, llm=FakeSemanticLLM()).verify_claim(
            statement="Model A reaches 91.2 on GSM8K.",
            evidence_excerpt="Model A reaches 91.2 on GSM8K.",
            evidence_ids=[paragraph.element_id],
            structured_required=True,
        )
        contradicted = EvidenceVerifier(store, llm=FakeSemanticLLM()).verify_claim(
            statement="Model A does not improve on GSM8K.",
            evidence_excerpt="Model A reaches 91.2 on GSM8K.",
            evidence_ids=[paragraph.element_id],
            structured_required=True,
        )
        assert semantic.result == "supported" and semantic.semantic_result == "entailed"
        assert contradicted.result == "unsupported" and contradicted.semantic_result == "contradicted"

        with sqlite3.connect(store.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM document_tables").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM document_table_cells").fetchone()[0] == 4


def test_source_hash_upsert_keeps_relational_source_id_authoritative() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store = PaperWikiPipelineStore(db_path=str(Path(tmp) / "source-upsert.db"))
        original = _packet("stable-source")
        store.upsert_source_packet(original)
        replacement = _packet("fresh-random-id")
        replacement.title = "Reparsed Evidence Paper"
        store.upsert_source_packet(replacement)
        restored = store.get_source_packet("stable-source")
        assert replacement.source_id == "stable-source"
        assert restored is not None
        assert restored.source_id == "stable-source"
        assert restored.title == "Reparsed Evidence Paper"
        assert restored.docling_json.get("persisted_source_document_id")


def test_verifier_keeps_long_docling_tail_and_normalizes_number_words() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store = PaperWikiPipelineStore(db_path=str(Path(tmp) / "long-evidence.db"))
        long_text = ("architecture context " * 50) + "The model trained for eight days. TAIL_EVIDENCE"
        packet = SourcePacket(
            source_id="long-packet",
            title="Long Evidence",
            source_hash="long-hash",
            parser_used="docling-local",
            docling_json={"texts": []},
            elements=[SourceElement(
                element_id="long-element",
                text=long_text,
                label="text",
                page=1,
            )],
        )
        store.upsert_source_packet(packet)
        llm = CapturingSemanticLLM()
        result = EvidenceVerifier(store, llm=llm).verify_claim(
            statement="The model trained for 8 days.",
            evidence_excerpt="The model trained for eight days.",
            evidence_ids=["long-element"],
            structured_required=True,
        )
        assert result.result == "supported"
        assert "TAIL_EVIDENCE" in llm.prompt


def test_verifier_rebinds_hallucinated_or_wrong_evidence_ids() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store = PaperWikiPipelineStore(db_path=str(Path(tmp) / "rebind.db"))
        packet = _packet()
        store.upsert_source_packet(packet)
        wrong = next(item for item in packet.elements if item.text == "Experiments")
        right = next(item for item in packet.elements if "91.2" in item.text)
        candidate = DistilledCandidate(
            id="rebind-candidate",
            source_packet_id=packet.source_id,
            candidate_type="paper_page",
            page_type="PaperPage",
            title="Evidence Paper",
            summary="A sufficiently long paper summary for validation.",
            claims=[CandidateClaim(
                claim="Model A reaches 91.2 on GSM8K.",
                evidence="Model A reaches 91.2 on GSM8K.",
                section_id="experiments",
                evidence_ids=[wrong.element_id, "ev-hallucinated"],
            )],
        )
        result = EvidenceVerifier(store, llm=FakeSemanticLLM()).bind_and_verify_candidate(candidate)[0]
        assert result.result == "supported"
        assert right.element_id in candidate.claims[0].evidence_ids
        assert "ev-hallucinated" not in candidate.claims[0].evidence_ids


def test_cross_language_evidence_requires_semantic_verifier() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store = PaperWikiPipelineStore(db_path=str(Path(tmp) / "cross-language.db"))
        packet = _packet()
        store.upsert_source_packet(packet)
        paragraph = next(item for item in packet.elements if "91.2" in item.text)
        kwargs = dict(
            statement="模型A在该数学推理基准测试上的最终评估结果为91.2分。",
            evidence_excerpt="模型A在该数学推理基准测试上的最终评估结果为91.2分。",
            evidence_ids=[paragraph.element_id],
            structured_required=True,
        )
        assert EvidenceVerifier(store).verify_claim(**kwargs).result == "unsupported"
        semantic = EvidenceVerifier(store, llm=FakeSemanticLLM()).verify_claim(**kwargs)
        assert semantic.result == "supported"
        assert semantic.semantic_result == "entailed"
        persisted = EvidenceVerifier(store).verify_claim_payload({
            "id": "translated-claim",
            "statement": kwargs["statement"],
            "evidence_excerpt": kwargs["evidence_excerpt"],
            "evidence_ids": kwargs["evidence_ids"],
            "source_packet_ids": [packet.source_id],
            "semantic_verification_required": True,
            "verifier_result": "entailed",
            "verifier_reason": "verified before compilation",
            "entailment_score": 0.96,
        })
        assert persisted.result == "supported"
        assert persisted.semantic_result == "entailed"


def test_hierarchical_compiler_strengthens_existing_claim_instead_of_duplicating() -> None:
    packet = _packet()
    paragraph = next(item for item in packet.elements if "91.2" in item.text)
    candidate = DistilledCandidate(
        id="candidate-1",
        source_packet_id=packet.source_id,
        candidate_type="concept_card",
        page_type="ConceptPage",
        title="GSM8K result",
        claims=[CandidateClaim(
            claim="Model A reaches 91.2 on GSM8K.",
            evidence="Model A reaches 91.2 on GSM8K.",
            evidence_ids=[paragraph.element_id],
        )],
    )
    existing = [{
        "id": "claim-old",
        "statement": "Model A reaches 91.2 on GSM8K.",
        "status": "supported",
        "evidence_ids": [],
        "source_packet_ids": ["older-packet"],
        "confidence": 0.8,
    }]
    decisions = ClaimRelationResolver().resolve(
        page_title=candidate.title,
        incoming_claims=candidate.claims,
        existing_claims=existing,
    )
    result = HierarchicalWikiCompiler().compile_claims(
        content_json={"definition": "Benchmark result."},
        existing_claims=existing,
        candidate=candidate,
        packet=packet,
        relation_decisions=decisions,
    )
    assert len(result.content_json["claims"]) == 1
    assert result.affected_claims[0]["action"] == "strengthen_claim"
    assert paragraph.element_id in result.content_json["claims"][0]["evidence_ids"]


def test_hierarchical_compiler_records_both_sides_of_cross_source_conflict() -> None:
    packet = _packet()
    paragraph = packet.elements[0]
    candidate = DistilledCandidate(
        id="candidate-conflict",
        source_packet_id=packet.source_id,
        candidate_type="concept_card",
        page_type="ConceptPage",
        title="Difficulty estimation",
        claims=[CandidateClaim(
            claim="Model A does require output tokens for difficulty estimation.",
            evidence=paragraph.text,
            evidence_ids=[paragraph.element_id],
        )],
    )
    existing = [{
        "id": "claim-existing",
        "statement": "Model A does not require output tokens for difficulty estimation.",
        "status": "supported",
        "evidence_ids": ["older-evidence"],
        "source_packet_ids": ["older-paper"],
        "confidence": 0.9,
    }]
    decisions = ClaimRelationResolver().resolve(
        page_title=candidate.title,
        incoming_claims=candidate.claims,
        existing_claims=existing,
    )
    result = HierarchicalWikiCompiler().compile_claims(
        content_json={}, existing_claims=existing, candidate=candidate, packet=packet,
        relation_decisions=decisions,
    )
    change = result.affected_claims[0]
    incoming = next(item for item in result.content_json["claims"] if item["id"] != "claim-existing")
    assert change["action"] == "challenge_claim"
    assert change["compared_claim_id"] == "claim-existing"
    assert incoming["conflicts_with_claim_id"] == "claim-existing"
    assert incoming["source_packet_ids"] == [packet.source_id]


def test_claim_relation_resolver_does_not_conflict_across_different_scopes() -> None:
    incoming = CandidateClaim(
        claim="Model A reaches 91.2 accuracy on GSM8K.",
        evidence="Model A reaches 91.2 accuracy on GSM8K.",
        subject="Model A",
        aspect="benchmark accuracy",
        predicate="equals",
        value="91.2",
        scope={"dataset": "GSM8K"},
    )
    existing = [{
        "id": "claim-math",
        "statement": "Model A reaches 48.2 accuracy on MATH.",
        "subject": "Model A",
        "aspect": "benchmark accuracy",
        "predicate": "equals",
        "value": "48.2",
        "scope": {"dataset": "MATH"},
    }]
    decision = ClaimRelationResolver().resolve(
        page_title="Model A", incoming_claims=[incoming], existing_claims=existing,
    )[0]
    assert decision.relation == "complements"
    assert decision.scope_overlap is False
    assert decision.requires_review is False


def test_recompile_replaces_claims_from_previous_parser_for_same_source() -> None:
    packet = _packet()
    paragraph = next(item for item in packet.elements if "91.2" in item.text)
    candidate = DistilledCandidate(
        id="candidate-recompiled",
        source_packet_id=packet.source_id,
        candidate_type="paper_page",
        page_type="PaperPage",
        title="Evidence Paper",
        claims=[CandidateClaim(
            claim="Model A reaches 91.2 on GSM8K.",
            evidence="Model A reaches 91.2 on GSM8K.",
            evidence_ids=[paragraph.element_id],
            verifier_result="entailed",
            verifier_reason="verified after Docling reparse",
            entailment_score=0.99,
        )],
    )
    old = [{
        "id": "fallback-claim",
        "statement": "Old fallback parser claim.",
        "evidence_ids": ["fallback-evidence-no-longer-exists"],
        "source_packet_ids": [packet.source_id],
    }]
    result = HierarchicalWikiCompiler().compile_claims(
        content_json={}, existing_claims=old, candidate=candidate, packet=packet,
    )
    assert len(result.content_json["claims"]) == 1
    assert result.content_json["claims"][0]["id"] != "fallback-claim"
    assert result.content_json["claims"][0]["evidence_ids"] == [paragraph.element_id]


def test_revision_commit_diff_and_rollback_restore_previous_markdown() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        _storage()
        root = Path(tmp)
        db_path = str(root / "revision.db")
        wiki = WikiStore(db_path=db_path)
        store = PaperWikiPipelineStore(db_path=db_path)
        packet = _packet()
        store.upsert_source_packet(packet)
        paragraph = next(item for item in packet.elements if "91.2" in item.text)
        vault = MarkdownVault(vault_dir=str(root / "wiki"))
        reindexer = MarkdownWikiReindexer(db_path=db_path)
        manager = WikiRevisionManager(store=store, vault=vault, reindexer=reindexer)

        base = manager.commit_card(
            card_id="page-1", title="Benchmark", page_type="ConceptPage",
            content_json={"definition": "Initial definition."}, summary="Initial summary.",
            source_level="primary", source_urls=[], related_topics=[], reason="seed",
        )
        existing = wiki.get_card("page-1")
        claim = {
            "id": "claim-result",
            "statement": "Model A reaches 91.2 on GSM8K.",
            "status": "supported",
            "relation": "supports",
            "evidence_ids": [paragraph.element_id],
            "evidence_excerpt": "Model A reaches 91.2 on GSM8K.",
            "source_packet_ids": [packet.source_id],
            "confidence": 1.0,
        }
        update = manager.commit_card(
            card_id="page-1", title="Benchmark", page_type="ConceptPage",
            content_json={"definition": "Updated definition.", "claims": [claim], "affected_claims": [{"claim_id": "claim-result"}]},
            summary="Updated summary.", source_level="primary", source_urls=[], related_topics=[],
            existing_card=existing, reason="verified result",
        )
        assert "-Initial summary." in update["patch"]
        assert "+Updated summary." in update["patch"]
        assert wiki.get_card("page-1")["summary"] == "Updated summary."

        rolled_back = manager.rollback(update["revision_id"], reason="test rollback")
        assert rolled_back["rolled_back_revision_id"] == update["revision_id"]
        assert wiki.get_card("page-1")["summary"] == "Initial summary."
        assert store.get_revision(update["revision_id"])["review_status"] == "rolled_back"
        assert store.list_page_claims("page-1")[0]["status"] == "removed"
        assert base["revision_id"]


def test_markdown_summary_cannot_inject_a_new_section_heading() -> None:
    markdown = MarkdownVault().render_card(
        card_id="summary-card",
        title="Summary Card",
        page_type="PaperPage",
        summary="## Paper Title\n\nAbstract text remains in the summary.",
        content_json={},
        source_level="primary",
        source_urls=[],
        related_topics=[],
    )
    parsed = parse_markdown_card(markdown)
    assert parsed.summary == "Paper Title Abstract text remains in the summary."
    assert "Paper Title" not in parsed.sections


def test_markdown_hides_runtime_audit_fields_but_reindexes_them_losslessly() -> None:
    claim = {
        "id": "claim-readable",
        "statement": "GRPO does not require a critic model.",
        "status": "supported",
        "evidence_ids": ["ev-1"],
        "scope": {"version": "original"},
    }
    markdown = MarkdownVault().render_card(
        card_id="grpo", title="GRPO", page_type="ConceptPage",
        summary="A policy optimization method.",
        content_json={
            "definition": "GRPO is a policy optimization method.",
            "claims": [claim],
            "merge_history": [{"action": "update_existing"}],
            "affected_claims": [{"claim_id": "claim-readable", "action": "add_claim"}],
            "compiler": {"name": "hierarchical-claim-compiler"},
        },
        source_level="primary", source_urls=[], related_topics=[],
    )
    assert "## Knowledge Claims" not in markdown
    assert "GRPO does not require a critic model." in markdown  # hidden maintenance snapshot
    assert "`claim:" not in markdown
    assert "## Merge History" not in markdown
    assert "<!-- wiki-system " in markdown
    parsed_content = content_json_from_sections(parse_markdown_card(markdown))
    assert parsed_content["claims"][0]["scope"] == {"version": "original"}
    assert parsed_content["merge_history"][0]["action"] == "update_existing"
    assert parsed_content["compiler"]["name"] == "hierarchical-claim-compiler"


def test_revision_rejects_unsupported_claim_without_overwriting_page() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        _storage()
        root = Path(tmp)
        db_path = str(root / "reject.db")
        wiki = WikiStore(db_path=db_path)
        store = PaperWikiPipelineStore(db_path=db_path)
        packet = _packet()
        store.upsert_source_packet(packet)
        paragraph = next(item for item in packet.elements if "91.2" in item.text)
        manager = WikiRevisionManager(
            store=store,
            vault=MarkdownVault(vault_dir=str(root / "wiki")),
            reindexer=MarkdownWikiReindexer(db_path=db_path),
        )
        manager.commit_card(
            card_id="page-2", title="Reject", page_type="ConceptPage",
            content_json={"definition": "Safe."}, summary="Safe summary.",
            source_level="primary", source_urls=[], related_topics=[],
        )
        existing = wiki.get_card("page-2")
        bad_claim = {
            "id": "bad", "statement": "Model A reaches 99.9 on GSM8K.",
            "status": "supported", "relation": "supports",
            "evidence_ids": [paragraph.element_id],
            "evidence_excerpt": "Model A reaches 91.2 on GSM8K.",
            "source_packet_ids": [packet.source_id],
        }
        try:
            manager.commit_card(
                card_id="page-2", title="Reject", page_type="ConceptPage",
                content_json={"definition": "Unsafe.", "claims": [bad_claim], "affected_claims": [{"claim_id": "bad"}]},
                summary="Unsafe summary.", source_level="primary", source_urls=[], related_topics=[],
                existing_card=existing,
            )
            assert False, "unsupported revision should fail"
        except RevisionRejectedError:
            pass
        assert wiki.get_card("page-2")["summary"] == "Safe summary."
        assert store.list_revisions("page-2")[0]["review_status"] == "rejected"


def test_table_resolver_and_readonly_duckdb_cross_paper_query() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store = PaperWikiPipelineStore(db_path=str(Path(tmp) / "tables.db"))
        first = _packet("paper-a")
        second = _packet("paper-b")
        second.title = "Second Evidence Paper"
        second.source_hash = "fixture-hash-b"
        # IDs must be unique across source documents.
        elements, tables = normalize_docling_document(DOCLING_FIXTURE, source_key="fixture-hash-b")
        second.elements = [SourceElement(**item) for item in elements]
        second.tables = [SourceTable(**item) for item in tables]
        store.upsert_source_packet(first)
        store.upsert_source_packet(second)

        resolver = TableResolver(store)
        resolved = resolver.resolve("GSM8K Model A", limit=5)
        assert len(resolved) == 2
        rows = resolver.evidence_rows([item.table_id for item in resolved])
        query = """SELECT source_title, max(numeric_value) AS best
                   FROM evidence_cells
                   WHERE column_label='GSM8K' AND numeric_value IS NOT NULL
                   GROUP BY source_title ORDER BY source_title"""
        result = ReadOnlyTableEngine().execute(query, rows)
        assert len(result) == 2
        assert all(item["best"] == 91.2 for item in result)
        try:
            ReadOnlyTableEngine().execute("SELECT * FROM read_csv_auto('secret.csv')", rows)
            assert False, "external access must be blocked"
        except ValueError:
            pass

        answer = TableQuestionAnswerer(store).answer("GSM8K Model A", limit=5)
        assert answer["tables"] and answer["rows"] and answer["citations"]

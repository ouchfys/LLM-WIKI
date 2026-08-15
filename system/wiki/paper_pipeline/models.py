from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SourceSection(BaseModel):
    section_id: str
    heading: str = ""
    text: str = ""
    page_start: int = 0
    page_end: int = 0
    evidence_ids: list[str] = Field(default_factory=list)


class SourceElement(BaseModel):
    """Replayable unit extracted from a DoclingDocument."""

    element_id: str
    element_type: str = "text"
    text: str = ""
    caption: str = ""
    page: int = 0
    bbox: dict[str, float] = Field(default_factory=dict)
    heading_path: list[str] = Field(default_factory=list)
    parent_id: str = ""
    reading_order: int = 0
    docling_ref: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class TableCellEvidence(BaseModel):
    cell_id: str
    table_id: str
    row_index: int = 0
    column_index: int = 0
    row_span: int = 1
    column_span: int = 1
    text: str = ""
    row_header: bool = False
    column_header: bool = False
    page: int = 0
    bbox: dict[str, float] = Field(default_factory=dict)


class SourceTable(BaseModel):
    table_id: str
    element_id: str
    caption: str = ""
    section_path: list[str] = Field(default_factory=list)
    page: int = 0
    bbox: dict[str, float] = Field(default_factory=dict)
    headers: list[list[str]] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    markdown: str = ""
    docling_ref: str = ""
    cells: list[TableCellEvidence] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourcePacket(BaseModel):
    source_id: str
    source_type: str = "paper_pdf"
    title: str
    abstract: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_urls: list[str] = Field(default_factory=list)
    raw_source_path: str = ""
    pdf_storage_uri: str = ""
    parser_used: str = ""
    source_hash: str = ""
    sections: list[SourceSection] = Field(default_factory=list)
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    docling_json: dict[str, Any] = Field(default_factory=dict)
    elements: list[SourceElement] = Field(default_factory=list)
    figures: list[dict[str, Any]] = Field(default_factory=list)
    tables: list[SourceTable] = Field(default_factory=list)


class CandidateClaim(BaseModel):
    claim: str
    evidence: str
    # Source-local semantics.  Distillation describes what the paper says;
    # cross-source relations are decided later by ClaimRelationResolver while
    # merging into a concrete Wiki page.
    subject: str = ""
    aspect: str = ""
    predicate: str = ""
    value: str = ""
    scope: dict[str, str] = Field(default_factory=dict)
    qualifiers: list[str] = Field(default_factory=list)
    section_id: str = ""
    page_start: int = 0
    evidence_ids: list[str] = Field(default_factory=list)
    # Kept only so previously persisted candidates remain readable.  New
    # distillation prompts do not ask the model to classify Wiki relations.
    relation: Literal["supports", "challenges", "supersedes"] = "supports"
    confidence: float = 1.0
    verifier_result: str = ""
    verifier_reason: str = ""
    entailment_score: float = 0.0


class ClaimRelationDecision(BaseModel):
    incoming_index: int
    existing_claim_id: str = ""
    relation: Literal[
        "new", "equivalent", "supports", "complements", "contradicts",
        "supersedes", "unrelated", "uncertain",
    ] = "new"
    confidence: float = 0.0
    reason: str = ""
    same_entity: bool = True
    same_aspect: bool = False
    scope_overlap: bool = True
    requires_review: bool = False
    comparison_subject: str = ""
    comparison_aspect: str = ""
    comparison_scope: dict[str, str] = Field(default_factory=dict)


class DistilledCandidate(BaseModel):
    id: str = ""
    source_packet_id: str = ""
    candidate_type: Literal["paper_page", "concept_card", "method_card"]
    page_type: Literal["PaperPage", "ConceptPage", "MethodPage"]
    title: str
    aliases: list[str] = Field(default_factory=list)
    summary: str = ""
    content_json: dict[str, Any] = Field(default_factory=dict)
    claims: list[CandidateClaim] = Field(default_factory=list)
    related_topics: list[str] = Field(default_factory=list)
    source_level: str = "primary"


class ReviewReport(BaseModel):
    id: str = ""
    candidate_id: str = ""
    status: Literal["approved", "needs_revision", "rejected"]
    schema_errors: list[str] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    evidence_quality: str = "unknown"
    duplicate_candidates: list[dict[str, Any]] = Field(default_factory=list)
    merge_recommendation: dict[str, Any] = Field(default_factory=dict)
    claim_verifications: list[dict[str, Any]] = Field(default_factory=list)


class MergePlan(BaseModel):
    action: Literal["create_new", "update_existing", "link_only", "skip_duplicate", "needs_human_review"]
    target_card_id: str = ""
    field_updates: dict[str, Any] = Field(default_factory=dict)
    aliases_to_add: list[str] = Field(default_factory=list)
    links_to_add: list[dict[str, Any]] = Field(default_factory=list)
    reason: str = ""
    confidence: float = 0.0


class MergeResult(BaseModel):
    paper_card_id: str = ""
    created_cards: list[dict[str, Any]] = Field(default_factory=list)
    updated_cards: list[dict[str, Any]] = Field(default_factory=list)
    linked_cards: list[dict[str, Any]] = Field(default_factory=list)
    review_rejections: list[dict[str, Any]] = Field(default_factory=list)
    merge_audit: list[dict[str, Any]] = Field(default_factory=list)
    proposals: list[dict[str, Any]] = Field(default_factory=list)


class PaperPipelineResult(BaseModel):
    ok: bool = True
    pipeline: str = "wiki_compile"
    source_packet_id: str = ""
    paper_id: str = ""
    paper_card_id: str = ""
    wiki_card_id: str = ""
    blocks: int = 0
    parser: str = ""
    raw_source_path: str = ""
    pdf_storage_uri: str = ""
    created_cards: list[dict[str, Any]] = Field(default_factory=list)
    updated_cards: list[dict[str, Any]] = Field(default_factory=list)
    linked_cards: list[dict[str, Any]] = Field(default_factory=list)
    review_rejections: list[dict[str, Any]] = Field(default_factory=list)
    merge_audit: list[dict[str, Any]] = Field(default_factory=list)
    proposals: list[dict[str, Any]] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)

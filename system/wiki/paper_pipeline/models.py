from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SourceSection(BaseModel):
    section_id: str
    heading: str = ""
    text: str = ""
    page_start: int = 0
    page_end: int = 0


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
    figures: list[dict[str, Any]] = Field(default_factory=list)
    tables: list[dict[str, Any]] = Field(default_factory=list)


class CandidateClaim(BaseModel):
    claim: str
    evidence: str
    section_id: str = ""
    page_start: int = 0


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


class PaperPipelineResult(BaseModel):
    ok: bool = True
    pipeline: str = "four_agent"
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
    timings: dict[str, float] = Field(default_factory=dict)

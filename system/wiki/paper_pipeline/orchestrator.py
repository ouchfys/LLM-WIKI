from __future__ import annotations

import time
from pathlib import Path

from system.paper_index.store import PaperIndexStore
from system.wiki.chunk_index import WikiChunkIndex
from system.wiki.paper_pipeline.distiller import PaperDistiller
from system.wiki.paper_pipeline.extractor import extract_paper_source
from system.wiki.paper_pipeline.merger import PaperMergeAgent
from system.wiki.paper_pipeline.models import PaperPipelineResult
from system.wiki.paper_pipeline.reviewer import PaperReviewAgent
from system.wiki.paper_pipeline.store import PaperWikiPipelineStore
from system.wiki.wiki_store import WikiStore


def run_paper_pipeline(
    pdf_path: Path,
    source_url: str,
    paper_store: PaperIndexStore,
    wiki_store: WikiStore,
    chunk_index: WikiChunkIndex,
    llm=None,
    review_llm=None,
    merge_llm=None,
    approval_mode: str = "auto",
    stage_callback=None,
) -> PaperPipelineResult:
    timings: dict[str, float] = {}
    pipeline_store = PaperWikiPipelineStore(db_path=wiki_store.db_path)

    start = time.perf_counter()
    if stage_callback:
        stage_callback("EXTRACTING", {"pdf_path": str(pdf_path)})
    packet = extract_paper_source(pdf_path=pdf_path, source_url=source_url, store=pipeline_store)
    timings["extract_seconds"] = round(time.perf_counter() - start, 2)

    paper_id = paper_store.upsert_paper(
        title=packet.title,
        source_url=source_url,
        pdf_path=str(pdf_path),
        summary=packet.abstract,
        metadata=packet.metadata,
    )
    paper_store.replace_blocks(paper_id, packet.blocks)

    start = time.perf_counter()
    if stage_callback:
        stage_callback("DISTILLING", {"source_packet_id": packet.source_id})
    candidates = PaperDistiller(llm=llm, store=pipeline_store).distill(packet)
    timings["distill_seconds"] = round(time.perf_counter() - start, 2)

    start = time.perf_counter()
    if stage_callback:
        stage_callback("VERIFYING", {"candidate_count": len(candidates)})
    reviewer = PaperReviewAgent(pipeline_store=pipeline_store, wiki_store=wiki_store, llm=review_llm)
    reports = {candidate.id: reviewer.review(candidate) for candidate in candidates}
    timings["review_seconds"] = round(time.perf_counter() - start, 2)
    timings["review_llm_calls"] = float(reviewer.llm_calls)

    start = time.perf_counter()
    if stage_callback:
        stage_callback("COMPILING_PROPOSAL", {"approved_candidates": sum(report.status == "approved" for report in reports.values())})
    merger = PaperMergeAgent(
        pipeline_store=pipeline_store, wiki_store=wiki_store, llm=merge_llm,
        approval_mode=approval_mode,
    )
    merge_result = merger.merge(
        packet=packet,
        candidates=candidates,
        reports=reports,
    )
    timings["merge_seconds"] = round(time.perf_counter() - start, 2)
    timings["merge_llm_calls"] = float(merger.llm_calls)

    if approval_mode == "auto":
        changed_card_ids = [merge_result.paper_card_id]
        changed_card_ids.extend(item["id"] for item in merge_result.created_cards)
        changed_card_ids.extend(item["id"] for item in merge_result.updated_cards)
        for card_id in dict.fromkeys(card_id for card_id in changed_card_ids if card_id):
            card = wiki_store.get_card(card_id)
            if not card:
                continue
            chunk_index.reindex_card(
                card_id=card_id,
                raw_source_path=packet.raw_source_path if card.get("page_type") == "PaperPage" else "",
                markdown_path=card.get("markdown_path", ""),
                source_kind="paper_pdf",
            )

    return PaperPipelineResult(
        ok=bool(merge_result.paper_card_id),
        source_packet_id=packet.source_id,
        paper_id=paper_id,
        paper_card_id=merge_result.paper_card_id,
        wiki_card_id=merge_result.paper_card_id,
        blocks=len(packet.blocks),
        parser=packet.parser_used,
        raw_source_path=packet.raw_source_path,
        pdf_storage_uri=packet.pdf_storage_uri,
        created_cards=merge_result.created_cards,
        updated_cards=merge_result.updated_cards,
        linked_cards=merge_result.linked_cards,
        review_rejections=merge_result.review_rejections,
        merge_audit=merge_result.merge_audit,
        proposals=merge_result.proposals,
        timings=timings,
    )

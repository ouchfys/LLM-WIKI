import json
from types import SimpleNamespace

import pytest

from system.agent_runtime import AgentRunStore, TraceRecorder
from system.core.deepseek_client import OutputTruncatedError
from system.document.models import ParsedDocument
from system.wiki.paper_pipeline.distiller import PaperDistillationError, PaperDistiller, paper_coverage_issues
from system.wiki.paper_pipeline.extractor import _abstract_section, _parse_pdf, extract_paper_source
from system.wiki.paper_pipeline.models import SourceElement, SourcePacket
from system.wiki.paper_pipeline.reviewer import PaperReviewAgent


def complete_response():
    return {"paper_page": {
        "candidate_type": "paper_page", "page_type": "PaperPage", "title": "Example paper",
        "summary": "本文介绍一种基于真实执行记录维护研究状态并核验论文证据的方法。",
        "content_json": {
            "schema_version": "paper-wiki-v2", "paper_type": "theory",
            "reading_guide": (
                "### 为什么需要它\n\n研究任务可能跨越多次会话。只保存一段总结，无法确认哪些步骤真的执行过，"
                "也无法直接检查答案是否得到论文原文支持。\n\n"
                "### 核心办法\n\n系统把任务要求和每次工具运行的真实结果分开记录。"
                "可以把它想成实验记录本：写下计划之后，还要记录实际做了什么和看到了什么。"
                "这只是帮助理解的例子，并非论文实验。\n\n"
                "### 证据与边界\n\n回答问题时再回到原文核验重要结论；"
                "执行记录只能证明动作发生过，不能单独证明最后的解释正确。"
            ),
            "research_problem": "研究任务需要在跨会话与上下文压缩后保留已经完成的工作，并准确定位支持结论的原始证据。" * 2,
            "motivation": "现有方案可能把模型声称完成的任务误认为真实完成，因此需要将执行记录与自然语言计划分别保存。" * 2,
            "method_overview": "该方法首先记录任务的输入与完成条件，然后在每个工具执行后保存真实结果，再对论文结论进行原文回读与证据核验，最后根据已经完成的工作继续研究。" * 3,
            "contributions": ["保存真实执行记录", "核验原文证据"],
            "key_takeaways": ["执行可追溯", "摘要不是证据", "失败保留状态"],
        },
        "claims": [{"claim": "The method records execution.", "evidence": "The method records execution.", "section_id": "method"}],
    }}


class ScriptedLLM:
    model = "deepseek-v4-flash"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def invoke(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def packet():
    return SourcePacket(source_id="source-1", title="Example paper", abstract="The method records execution.")


@pytest.mark.parametrize("failure", [
    '{"paper_page": {"title": "unfinished"',
    OutputTruncatedError(max_tokens=65536, output_tokens=65536),
    {"paper_page": {"title": "wrong schema"}},
])
def test_invalid_paper_response_repairs_once_instead_of_publishing_abstract(failure, monkeypatch, tmp_path):
    monkeypatch.setenv("PAPERWIKI_PAPER_OUTPUT_TOKENS", "65536")
    monkeypatch.setenv("PAPERWIKI_PAPER_RETRY_OUTPUT_TOKENS", "131072")
    llm = ScriptedLLM([failure, complete_response(), {"knowledge_cards": []}])
    runtime = AgentRunStore(db_path=str(tmp_path / "trace.db"))
    run = runtime.create_run(run_type="paper_ingestion")
    with TraceRecorder(runtime, run["id"]).bind():
        candidates = PaperDistiller(llm=llm).distill(packet())
    assert candidates[0].content_json["compile_status"] == "llm_refined"
    assert [call["max_tokens"] for call in llm.calls[:2]] == [65536, 131072]
    assert "previous response could not be used" in llm.calls[1]["prompt"]
    retry = next(event for event in runtime.list_events(run["id"]) if event["event_type"] == "paper.distillation.retry")
    assert retry["error"]


def test_repeated_invalid_json_fails_job_without_inserting_fallback_candidate(tmp_path):
    llm = ScriptedLLM(['{"paper_page":', '{"paper_page":'])
    inserted = []
    store = SimpleNamespace(insert_candidate=inserted.append)
    runtime = AgentRunStore(db_path=str(tmp_path / "trace.db"))
    run = runtime.create_run(run_type="paper_ingestion")
    with TraceRecorder(runtime, run["id"]).bind(), pytest.raises(PaperDistillationError, match="after 2 attempts"):
        PaperDistiller(llm=llm, store=store).distill(packet())
    assert len(llm.calls) == 2
    assert inserted == []
    failed = next(event for event in runtime.list_events(run["id"]) if event["event_type"] == "paper.distillation.failed")
    assert "invalid JSON" in failed["error"]


def test_distillation_failure_is_persisted_and_requires_explicit_retry(monkeypatch, tmp_path):
    from backend.api import papers as papers_api
    from system.wiki.ingestion_jobs import IngestionJobStore

    db_path = str(tmp_path / "failed-job.db")
    jobs = IngestionJobStore(db_path=db_path)
    runtime = AgentRunStore(db_path=db_path)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    job = jobs.create_job(source_type="paper_pdf", source_uri=str(pdf))
    run = runtime.create_run(
        run_type="paper_ingestion", source_uri=str(pdf), ingestion_job_id=job["id"],
        context={"job_id": job["id"], "pdf_path": str(pdf), "pipeline": "wiki_compile"},
    )
    llm = ScriptedLLM(['{"paper_page":', '{"paper_page":'])

    def failing_pipeline(**kwargs):
        kwargs["stage_callback"]("EXTRACTING", {"pdf_path": str(pdf)})
        kwargs["stage_callback"]("DISTILLING", {"source_packet_id": "source-1"})
        return PaperDistiller(llm=llm).distill(packet())

    monkeypatch.setattr(papers_api, "_run_pdf_pipeline", failing_pipeline)
    papers_api._run_ingestion_job(
        db_path=db_path, job_id=job["id"], pdf_path=str(pdf), source_url="",
        pipeline="wiki_compile", run_id=run["id"], run_maintenance=False,
    )
    assert jobs.get_job(job["id"])["status"] == "failed"
    assert "after 2 attempts" in jobs.get_job(job["id"])["error"]
    assert runtime.get_run(run["id"])["current_state"] == "FAILED"
    assert runtime.list_recoverable_runs() == []
    assert pdf.exists()
    assert runtime.restart_run(run["id"], reason="explicit test retry")["current_state"] == "QUEUED"


def test_configured_output_budget_above_default_is_not_silently_capped(monkeypatch):
    monkeypatch.setenv("PAPERWIKI_PAPER_OUTPUT_TOKENS", "131072")
    monkeypatch.setenv("PAPERWIKI_PAPER_RETRY_OUTPUT_TOKENS", "262144")
    llm = ScriptedLLM([complete_response(), {"knowledge_cards": []}])
    PaperDistiller(llm=llm).distill(packet())
    assert llm.calls[0]["max_tokens"] == 131072


def test_extraction_only_and_legacy_candidates_cannot_bypass_quality_gate():
    source = packet()
    candidate = PaperDistiller().distill(source)[0]
    reports, statuses = [], []
    store = SimpleNamespace(get_source_packet=lambda _: source, insert_review_report=reports.append,
                            update_candidate_status=lambda *args: statuses.append(args))
    reviewer = PaperReviewAgent(store, object(), llm=object())
    reviewer.evidence_verifier = SimpleNamespace(bind_and_verify_candidate=lambda _: pytest.fail("invalid page must not incur verification calls"))
    report = reviewer.review(candidate)
    assert report.status == "needs_revision"
    assert report.merge_recommendation["action"] == "needs_human_review"
    assert report.evidence_quality == "not_checked"
    assert paper_coverage_issues(candidate)
    candidate.content_json["schema_version"] = "paper-wiki-v2"
    assert paper_coverage_issues(candidate)


@pytest.mark.parametrize("heading", ["## Abstract\n", "Abstract: ", "**Abstract**: ", "摘要："])
def test_inline_or_heading_abstract_excludes_authors_and_following_sections(heading):
    abstract = "The actual research abstract explains its contribution. " * 25
    text = "# Example paper\n\n" + "Author Name " * 100 + "\n\n" + heading + abstract + "\n\n## 1 Introduction\nINTRODUCTION_ONLY"
    selected = _abstract_section(text)
    assert selected == abstract.strip()
    assert "Author Name" not in selected
    assert "INTRODUCTION_ONLY" not in selected
    assert len(selected) > 600


def test_mineru_summary_comes_from_actual_abstract_after_long_author_list(monkeypatch, tmp_path):
    abstract = "Recursive self-improvement combines data, harness and model updates."
    markdown = "# MetaRSI\n\n" + "Author Name " * 100 + "\n\nAbstract: " + abstract + "\n\nFigure 1: Overview\n\n## 1 Introduction\nOther content."
    parsed = ParsedDocument(title="MetaRSI", text=markdown[:3000], markdown=markdown, parser="mineru-vlm")
    monkeypatch.setattr("system.wiki.paper_pipeline.extractor.PaperParserRouter", lambda: SimpleNamespace(parse=lambda *args, **kwargs: parsed))
    result = _parse_pdf(tmp_path / "paper.pdf")
    assert result["summary"] == abstract
    assert result["metadata"]["abstract_source"] == "markdown_abstract"


def test_missing_abstract_does_not_relabel_title_or_authors_as_abstract(monkeypatch, tmp_path):
    parsed = ParsedDocument(title="Paper", text="Authors Alice and Bob", markdown="# Paper\nAlice and Bob\n## Introduction\nBody", parser="mineru-vlm")
    monkeypatch.setattr("system.wiki.paper_pipeline.extractor.PaperParserRouter", lambda: SimpleNamespace(parse=lambda *args, **kwargs: parsed))
    assert _parse_pdf(tmp_path / "paper.pdf")["summary"] == ""


def test_old_extraction_cache_with_bad_abstract_is_not_reused(monkeypatch, tmp_path):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"PDF fixture")
    source = packet()
    source.parser_used = "mineru-vlm"
    source.elements = [SourceElement(element_id="ev-1", text="source")]
    source.blocks = [{"text": "source"}]
    source.metadata["extraction_schema_version"] = "paper-source-v5"
    store = SimpleNamespace(find_source_packet_by_hash=lambda _: source)
    def reparse(*args, **kwargs):
        raise RuntimeError("reparse_requested")
    monkeypatch.setattr("system.wiki.paper_pipeline.extractor._parse_pdf", reparse)
    with pytest.raises(RuntimeError, match="reparse_requested"):
        extract_paper_source(pdf, store=store)
    source.metadata["extraction_schema_version"] = "paper-source-v6"
    assert extract_paper_source(pdf, store=store) is source

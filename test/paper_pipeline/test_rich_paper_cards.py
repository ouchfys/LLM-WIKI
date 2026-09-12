from __future__ import annotations

import json

from system.document.evidence import matrix_to_markdown
from system.wiki.markdown_vault import MarkdownVault
from system.wiki.paper_pipeline.distiller import PaperDistiller, build_source_context, paper_coverage_issues
from system.wiki.paper_pipeline.models import DistilledCandidate, SourcePacket, SourceSection, SourceTable


def _table() -> SourceTable:
    headers = [["Model", "Throughput", "Accuracy"]]
    rows = [["Baseline", "10", "72.1"], ["Ours", "18", "73.0"]]
    return SourceTable(
        table_id="tbl-main",
        element_id="ev-table",
        caption="Table 2: Main serving results",
        section_path=["Experiments", "Main Results"],
        page=8,
        headers=headers,
        rows=rows,
        markdown=matrix_to_markdown(headers, rows),
    )


def _candidate() -> DistilledCandidate:
    return DistilledCandidate(
        candidate_type="paper_page",
        page_type="PaperPage",
        title="A Complete Paper",
        summary="一篇用于验证详细论文页编译和原始表格保真的完整测试论文。",
        content_json={
            "schema_version": "paper-wiki-v2",
            "paper_type": "system",
            "research_problem": "研究问题需要解释现有系统瓶颈、影响范围与为什么已有路径不足。" * 3,
            "motivation": "动机来自部署成本、吞吐量和精度之间长期存在的工程矛盾。" * 3,
            "contributions": ["提出完整方法", "验证系统收益"],
            "method_overview": "方法首先识别瓶颈，再联合设计算法与内核，最后在统一设置中验证端到端效果。" * 5,
            "method_components": [
                {"name": "算法", "purpose": "降低开销", "mechanism": "联合量化"},
                {"name": "内核", "purpose": "提高吞吐", "mechanism": "融合执行"},
            ],
            "execution_flow": ["准备输入", "执行方法", "收集指标"],
            "experiment_setup": [{"name": "Datasets", "details": "Benchmark A"}],
            "key_results": [{"finding": "吞吐提高"}, {"finding": "精度保持"}],
            "selected_table_ids": ["tbl-main"],
            "figure_notes": [],
            "limitations": "论文未明确报告。",
            "comparison_to_prior_work": "与已有工作相比，该方法把算法选择和系统实现放入同一优化目标。" * 3,
            "key_takeaways": ["结论一", "结论二", "结论三"],
        },
        claims=[],
    )


def test_full_source_context_keeps_late_sections(monkeypatch):
    monkeypatch.setenv("PAPERWIKI_PAPER_INPUT_TOKENS", "320000")
    sections = [
        SourceSection(section_id=f"s{i}", heading=f"Section {i}", text=(f"section-{i}-evidence " * 80))
        for i in range(12)
    ]
    packet = SourcePacket(source_id="packet", title="Long Paper", abstract="Abstract", sections=sections)
    context = build_source_context(packet)
    assert "section-0-evidence" in context
    assert "section-11-evidence" in context
    assert "section_id=s11" in context


def test_exact_table_markdown_is_attached_and_rendered_without_model_retyping():
    packet = SourcePacket(source_id="packet", title="Paper", tables=[_table()])
    candidate = _candidate()
    PaperDistiller()._attach_source_artifacts(candidate, packet)
    table_markdown = _table().markdown
    assert candidate.content_json["key_tables"][0]["markdown"] == table_markdown

    rendered = "\n".join(MarkdownVault._render_content_sections("PaperPage", candidate.content_json))
    assert "## Key Tables" in rendered
    assert "### Table 2: Main serving results" in rendered
    assert table_markdown in rendered
    assert "'markdown':" not in rendered


def test_v2_coverage_gate_rejects_shallow_paper_but_accepts_complete_card():
    packet = SourcePacket(source_id="packet", title="Paper", tables=[_table()])
    shallow = DistilledCandidate(
        candidate_type="paper_page",
        page_type="PaperPage",
        title="Shallow",
        summary="This summary is long enough for the base schema check.",
        content_json={"schema_version": "paper-wiki-v2", "research_problem": "too short"},
    )
    assert paper_coverage_issues(shallow, packet)

    complete = _candidate()
    PaperDistiller()._attach_source_artifacts(complete, packet)
    assert paper_coverage_issues(complete, packet) == []


def test_paper_page_and_topic_compilation_use_separate_model_calls():
    content = _candidate().content_json
    content["paper_type"] = "theory"
    response = {
        "paper_page": {
            "candidate_type": "paper_page",
            "page_type": "PaperPage",
            "title": "A Complete Paper",
            "aliases": [],
            "summary": "一篇完整覆盖研究问题、方法与结果的测试论文页面。",
            "content_json": content,
            "claims": [{
                "claim": "该方法联合优化算法与执行过程。",
                "evidence": "The method jointly optimizes the algorithm and execution process.",
                "section_id": "method",
                "evidence_ids": [],
            }],
            "related_topics": [],
            "source_level": "primary",
        }
    }

    class FakeLLM:
        model = "deepseek-v4-flash"

        def __init__(self):
            self.prompts = []

        def invoke(self, prompt, **_kwargs):
            self.prompts.append(prompt)
            return json.dumps(response if len(self.prompts) == 1 else {"knowledge_cards": []}, ensure_ascii=False)

    llm = FakeLLM()
    packet = SourcePacket(
        source_id="packet",
        title="A Complete Paper",
        sections=[SourceSection(section_id="method", heading="Method", text="method evidence " * 200)],
    )
    candidates = PaperDistiller(llm=llm).distill(packet)
    assert len(candidates) == 1
    assert len(llm.prompts) == 2
    assert "detailed, self-contained PaperWiki page" in llm.prompts[0]
    assert "reusable TopicPage candidates" in llm.prompts[1]

from __future__ import annotations

import json

from system.document.evidence import matrix_to_markdown
from system.wiki.markdown_vault import MarkdownVault, normalize_markdown_table
from system.wiki.paper_pipeline.distiller import PaperDistiller, build_source_context, paper_coverage_issues
from system.wiki.paper_pipeline.models import DistilledCandidate, SourcePacket, SourceSection, SourceTable
from system.wiki.wiki_builder import sanitize_wiki_text


def _table() -> SourceTable:
    headers = [["Model", "Throughput", "Accuracy"]]
    rows = [["Baseline", "10", "72.1"], ["Ours", "18", "73.0"]]
    return SourceTable(
        table_id="tbl-main",
        element_id="ev-table",
        caption="Table 2: Main serving results",
        section_path=["Experiments", "Main Results"],
        page=8,
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
            "reading_guide": (
                "### 为什么需要它\n\n原有系统运行研究任务时要同时顾及速度和结果质量。"
                "只追求更快可能让答案变差，只追求精度又可能让使用成本过高。\n\n"
                "### 核心办法\n\n作者先找出耗时的步骤，再让算法和执行程序一起改进。"
                "可以把它理解为同时改菜谱和厨房流程：菜谱决定做什么，流程决定多久能做好。"
                "这个比喻只用于解释思路，不是论文中的实验。\n\n"
                "### 证据与边界\n\n论文在同一组任务中比较了速度和质量，具体结果仍需"
                "结合测试条件解读，不能直接推广到所有系统。"
            ),
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
            "key_results": [{"finding": "吞吐提高", "table_id": "tbl-main"}, {"finding": "精度保持"}],
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
    assert "## 关键表格" in rendered
    assert "### 表 2" in rendered
    assert table_markdown in rendered
    assert "'markdown':" not in rendered


def test_table_is_optional_and_never_autoselected_from_source():
    packet = SourcePacket(source_id="packet", title="Paper", tables=[_table()])
    candidate = _candidate()
    candidate.content_json["selected_table_ids"] = []
    PaperDistiller()._attach_source_artifacts(candidate, packet)
    assert candidate.content_json["key_tables"] == []
    assert paper_coverage_issues(candidate, packet) == []


def test_table_without_headline_result_link_is_not_attached():
    packet = SourcePacket(source_id="packet", title="Paper", tables=[_table()])
    candidate = _candidate()
    candidate.content_json["key_results"][0].pop("table_id")
    PaperDistiller()._attach_source_artifacts(candidate, packet)
    assert candidate.content_json["key_tables"] == []


def test_reader_guide_is_required_even_when_technical_sections_are_complete():
    packet = SourcePacket(source_id="packet", title="Paper", tables=[_table()])
    candidate = _candidate()
    candidate.content_json.pop("reading_guide")
    PaperDistiller()._attach_source_artifacts(candidate, packet)
    assert any("reading_guide" in issue for issue in paper_coverage_issues(candidate, packet))


def test_table_renderer_repairs_pipe_only_rows_and_missing_leading_group_cells():
    malformed = """| Device | System | Throughput |
|
| --- | --- | --- |
| A100 | Baseline | 100 |
| Ours | 180 |
"""

    normalized = normalize_markdown_table(malformed)

    assert "\n|\n" not in normalized
    assert normalized.count("| --- | --- | --- |") == 1
    assert "|  | Ours | 180 |" in normalized


def test_figure_reader_projection_does_not_publish_raw_nearby_source_text():
    rendered = "\n".join(MarkdownVault._render_figure_notes([{
        "caption": "Figure 1",
        "description": "中文图表解读。",
        "trend": "吞吐量随批量增加。",
        "source_text": "A very long raw English paragraph copied from the parser.",
    }]))

    assert "## 图表解读" in rendered
    assert "中文图表解读" in rendered
    assert "raw English paragraph" not in rendered


def test_legacy_string_artifacts_are_cleaned_when_card_is_rewritten():
    rendered = "\n".join(MarkdownVault._render_content_sections("PaperPage", {
        "key_tables": """### Table 4. A very long original caption\n\n*来源位置：Results / page 9*\n\n| Device | Value |\n|\n| --- | --- |\n| A100 | 42 |""",
        "figure_notes": """### Figure 2. A very long original caption\n\n中文图表解读。\n\n- **论文正文说明**：raw English parser context""",
    }))

    assert "### 表 4" in rendered
    assert "| Device | Value |" in rendered
    assert "\n|\n" not in rendered
    assert "### 图 2" in rendered
    assert "中文图表解读" in rendered
    assert "raw English parser context" not in rendered


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


def test_v2_research_problem_gate_accepts_substantive_cjk_explanation():
    packet = SourcePacket(source_id="packet", title="Paper", tables=[_table()])
    candidate = _candidate()
    candidate.content_json["research_problem"] = (
        "现有深度研究系统依赖人工轨迹或静态检索，难以在真实网页环境中学习多轮规划、"
        "证据获取与答案生成之间的协同策略，因此需要可扩展的端到端强化学习方法。"
    )
    PaperDistiller()._attach_source_artifacts(candidate, packet)

    issues = paper_coverage_issues(candidate, packet)

    assert not any(issue.startswith("research_problem") for issue in issues)


def test_v2_research_problem_gate_counts_cjk_information_density():
    packet = SourcePacket(source_id="packet", title="Paper", tables=[_table()])
    candidate = _candidate()
    candidate.content_json["research_problem"] = (
        "如何有效利用稠密的逐轮奖励结构，在GRPO和PPO等RL算法中实现细粒度的信用分配，"
        "以提升多轮LLM智能体的推理能力。"
    )
    PaperDistiller()._attach_source_artifacts(candidate, packet)

    issues = paper_coverage_issues(candidate, packet)

    assert not any(issue.startswith("research_problem") for issue in issues)


def test_distiller_recovers_missing_summary_from_structured_paper_content():
    content = _candidate().content_json

    candidate = PaperDistiller()._candidate_from_payload({
        "candidate_type": "paper_page",
        "page_type": "PaperPage",
        "title": "EvolveSearch",
        "summary": "",
        "content_json": content,
        "claims": [],
    })

    assert candidate is not None
    assert len(candidate.summary) >= 20
    assert content["research_problem"] in candidate.summary


def test_sanitizer_keeps_long_cjk_prose_but_drops_ascii_blob_noise():
    prose = "该方法通过多轮工具交互、细粒度奖励和状态恢复机制提升长时程搜索智能体的可靠性。" * 12

    assert sanitize_wiki_text(prose) == prose
    assert sanitize_wiki_text("A" * 300) == ""


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


def test_source_table_placeholder_headers_are_removed_without_losing_values():
    from system.wiki.markdown_vault import normalize_markdown_table
    table = "| column_1 | column_2 |\n| --- | --- |\n| Method | Score |\n| A | 93.57 |"
    fixed = normalize_markdown_table(table)
    assert fixed.startswith("| Method | Score |")
    assert "column_" not in fixed
    assert "93.57" in fixed


def test_reading_guide_survives_canonical_markdown_roundtrip(tmp_path):
    from system.wiki.markdown_vault import MarkdownVault
    from system.wiki.markdown_parser import parse_markdown_card, content_json_from_sections
    guide = "### 核心办法\n\n先保存经验，再按需读取。\n\n$$B_k=\\max(0,W-S-G-R)$$"
    vault = MarkdownVault(vault_dir=str(tmp_path))
    text = vault.render_card(card_id="test-guide", title="Test", page_type="PaperPage",
        summary="Short summary", content_json={"reading_guide": guide},
        source_level="primary", source_urls=[], related_topics=[])
    assert content_json_from_sections(parse_markdown_card(text))["reading_guide"] == guide

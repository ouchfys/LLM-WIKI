"""Citation identities survive long pages, batch reordering and real budgets."""
import re

from system.conversation.context_budget import ContextBudget, ContextPolicy, TokenCounter
from system.wiki.wiki_chat import WikiChatService


class ByteCounter(TokenCounter):
    def __init__(self):
        self.mode = "test_bytes"
        self.tokenizer = self.encoding = None


def chat(window=800_000):
    budget = ContextBudget(ContextPolicy(window=window), ByteCounter())
    service = WikiChatService(object(), wiki_resolver=object(), context_budget=budget)
    service._wiki_catalog_context = lambda: ""
    service._task_plan_context = lambda: ""
    service._project_context = lambda query: ""
    return service


def papers(count=24, size=20_000):
    return [{
        "id": f"paper-{i}", "title": f"Paper {i}",
        "markdown_path": f"papers/{i}.md",
        "_full_text": f"BEGIN-PAPER-{i}\n\n" + "x" * size + f"\n\nEND-PAPER-{i}",
    } for i in range(1, count + 1)]


def opened(cards, result_id):
    return {"tool": "wiki_open", "status": "done", "result_id": result_id,
            "items": [{"card_id": c["id"], "title": c["title"], "content": c["_full_text"]}
                      for c in cards]}


def test_all_24_long_papers_keep_global_citation_map_and_single_complete_body():
    service = chat()
    cards = papers()
    observations = [opened(cards[:8], 1), opened(cards[8:], 2), opened(list(reversed(cards)), 3)]
    prompt = service._build_prompt("Explain the mechanism", cards, [], tool_observations=observations)

    for i in range(1, 25):
        assert f'[{i}] {{"card_id": "paper-{i}", "title": "Paper {i}"' in prompt
        assert prompt.count(f"BEGIN-PAPER-{i}\n") == 1
        assert prompt.count(f"END-PAPER-{i}\n") == 1 or prompt.endswith(f"END-PAPER-{i}")
    assert "[Opened Card " not in prompt
    assert "END-PAPER-24" in prompt
    assert len(prompt) > 12000
    service.context_budget.check_request(prompt, output_tokens=4096)


def test_actual_context_pressure_preserves_every_identity_and_shares_body_budget():
    service = chat(window=32_768)
    cards = papers(size=40_000)
    prompt = service._build_prompt("Compare all sources", cards, [], tool_observations=[opened(cards, 1)])

    for i in range(1, 25):
        assert f'[{i}] {{"card_id": "paper-{i}"' in prompt
        assert f"BEGIN-PAPER-{i}\n" in prompt
    assert "内容因上下文预算省略" in prompt
    assert "END-PAPER-24" not in prompt
    service.context_budget.check_request(prompt, output_tokens=4096)


def test_batch_order_does_not_reassign_response_citations():
    service = chat()
    cards = papers(3, size=100)
    observations = [opened([cards[2], cards[0]], 1), opened([cards[1], cards[2]], 2)]
    text = service._answer_observation_context(observations, citation_numbers={c["id"]: i for i, c in enumerate(cards, 1)})

    assert "Wiki source [3]: card_id=paper-3" in text
    assert "Wiki source [1]: card_id=paper-1" in text
    assert "Wiki source [2]: card_id=paper-2" in text
    assert text.count("Wiki source [3]") == 1
    assert "BEGIN-PAPER" not in text
    assert "[Opened Card " not in text


def test_recovered_wiki_result_is_not_copied_again_but_other_recovery_is_kept():
    service = chat()
    cards = papers(1, size=100)
    observations = [opened(cards, 7), {"tool": "read_tool_result", "items": [
        {"tool": "wiki_open", "result_id": 7, "offset": 0, "content": "DUPLICATE-WIKI-BODY"},
        {"tool": "evidence_lookup", "result_id": 8, "offset": 0, "content": "ORIGINAL-EVIDENCE-QUOTE"},
    ]}]
    prompt = service._build_prompt("Explain", cards, [], tool_observations=observations)

    assert "DUPLICATE-WIKI-BODY" not in prompt
    assert "ORIGINAL-EVIDENCE-QUOTE" in prompt
    assert prompt.count("BEGIN-PAPER-1\n") == 1


def test_unmapped_receipt_never_invents_a_numeric_citation():
    service = chat()
    text = service._answer_observation_context([opened(papers(1, 50), 1)], citation_numbers={})
    assert "No numbered source" in text
    assert not re.search(r"Wiki source \[\d+\]", text)


def test_compact_card_context_prioritizes_reader_explanation_over_raw_table():
    body = chat()._compact_content({
        "key_tables": [{"markdown": "| Model | Score |\n| --- | --- |\n| A | 90 |"}],
        "reading_guide": "先解释这项研究要解决什么问题，以及分数如何理解。",
        "limitations": "测试只覆盖一种设置。",
    })
    assert body.startswith("reading_guide: 先解释")
    assert "limitations: 测试只覆盖一种设置" in body


def test_repository_answer_context_hides_internal_claim_payload():
    card = {
        "id": "repo-1", "page_type": "ConceptPage", "title": "记忆实现",
        "content_json": {"repository_research": {"repository": "a/b", "commit": "a" * 40}},
        "_full_text": "# 记忆实现\n\n## 结论\n\n代码读取会话记忆。\n\n"
                      '<!-- wiki-system {"claims":[{"evidence_excerpt":"INTERNAL-CODE"}]} -->\n\n'
                      '<small class="repository-snapshot">版本：aaaaaaaaaaaa</small>',
    }
    prompt = chat()._build_prompt("怎么读取记忆？", [card], [])
    assert "代码读取会话记忆" in prompt
    assert "版本：aaaaaaaaaaaa" in prompt
    assert "INTERNAL-CODE" not in prompt


def test_answer_scope_does_not_force_keyword_outlines_over_user_constraints():
    service = chat()
    cases = [
        "Jev 是怎么工作的？不要讨论消融，只用两句话说明。",
        "DeepSeek Harness 的记忆架构是怎么设计的？不要举例。",
        "只说明上下文压缩后如何使用结果，给出具体提示词和一个例子。",
        "Jev 和规则控制有什么区别，是否值得引入？先列对比表。",
    ]
    for query in cases:
        prompt = service._build_prompt(query, [], [])
        assert f"用户问题：{query}" in prompt
        assert service._answer_scope_policy() in prompt
        assert "用户明确的详略与格式要求优先" in prompt
        assert "本轮回答重心：" not in prompt
        assert "先用一个具体例子" not in prompt


def test_design_choice_prompt_keeps_attribution_proportional():
    prompt = chat()._build_prompt("我们需不需要引入 Jev？", [], [])
    assert "not independent corroboration" in prompt
    assert "Missing ablations alone do not establish" in prompt
    assert "only when relevant to the user's question" in prompt


def test_answer_failure_shows_reader_guide_and_states_synthesis_failed():
    answer = chat()._fallback_answer([{
        "id": "paper-1", "title": "论文一", "summary": "满是缩写的旧摘要",
        "content_json": {"reading_guide": "### 核心办法\n\n先记录真实执行，再核验结论。"},
    }])
    assert "未能生成可靠的综合回答" in answer
    assert "先记录真实执行" in answer
    assert "满是缩写的旧摘要" not in answer


def test_large_operational_receipts_do_not_evict_papers_or_source_quotes():
    service = chat(window=32_768)
    cards = papers(size=40_000)
    observations = [
        {"tool": "local_shell", "items": [{"content": "Operational output. " * 40_000}]},
        opened(cards, 1),
        {"tool": "evidence_lookup", "items": [{
            "source_packet_id": "packet-1", "page": 4,
            "text": "Original evidence " * 25 + "IMPORTANT-QUOTE-END",
        }]},
    ]
    prompt = service._build_prompt("Explain with evidence", cards, [], tool_observations=observations)

    assert "IMPORTANT-QUOTE-END" in prompt  # No old 260-character clipping.
    assert '"source_packet_id": "packet-1"' in prompt
    for i in range(1, 25):
        assert f"BEGIN-PAPER-{i}\n" in prompt
    service.context_budget.check_request(prompt, output_tokens=4096)

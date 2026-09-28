from system.wiki.wiki_chat import AgentToolCall, ToolCallPlan, WikiChatService


def test_ordinary_turn_has_no_search_or_open_quota():
    calls = [
        AgentToolCall("wiki_search", {"query": "QServe KIVI"}, "first query"),
        AgentToolCall("wiki_search", {"query": "KIVI asymmetric quantization"}, "rephrased query"),
        AgentToolCall("wiki_open", {"card_ids": ["a"]}, "open result"),
        AgentToolCall("wiki_open", {"card_ids": ["b"]}, "open another result"),
        AgentToolCall("evidence_lookup", {"query": "claim"}, "verify claim"),
    ]

    accepted = WikiChatService._apply_turn_tool_budget(calls, [], long_research=False)

    assert accepted == calls


def test_call_counts_do_not_block_subsequent_queries_in_either_mode():
    calls = [AgentToolCall("wiki_search", {"query": "rephrased"}, "try again")]
    executed = [ToolCallPlan("wiki_search", "original", "first query")]

    assert WikiChatService._apply_turn_tool_budget(calls, executed, long_research=False) == calls
    assert WikiChatService._apply_turn_tool_budget(calls, executed, long_research=True) == calls

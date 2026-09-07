import json
from types import SimpleNamespace

import pytest

from system.memory.context_budget import ContextBudget, ContextPolicy, ContextBudgetExceeded, TokenCounter
from system.memory.context_compaction import auto_compact, summarize_records
from system.memory.session_store import SessionStore
from system.wiki.wiki_chat import WikiChatService, AgentToolObservation


class ByteCounter(TokenCounter):
    def __init__(self):
        self.mode = "test_bytes"
        self.tokenizer = self.encoding = None


class SummaryLLM:
    def __init__(self, callback=None):
        self.prompts = []
        self.callback = callback

    def invoke(self, prompt, **kwargs):
        self.prompts.append(prompt)
        if self.callback:
            return self.callback()
        return "用户要求保留明确约束、来源与待办；继续处理当前问题。"


def small_budget():
    return ContextBudget(ContextPolicy(window=8000, output_reserve=1200, safety=256,
                        history=1200, planning_history=800, summary=256,
                        compact_trigger=1800, compact_keep=600), ByteCounter())


def populated(tmp_path):
    store = SessionStore(str(tmp_path / "history.db"))
    sid = store.create_session(settings={"unrelated": "preserve"})
    for i in range(6):
        store.save_message(sid, "user", f"Question-{i}: " + "abc def " * 40)
        store.save_message(sid, "assistant", f"Answer-{i}: " + "xyz uvw " * 40)
    return store, sid


def test_short_turns_are_not_limited_to_two_three_or_six(tmp_path):
    store = SessionStore(str(tmp_path / "history.db"))
    sid = store.create_session()
    for i in range(12):
        store.save_message(sid, "user", f"Question {i}")
        store.save_message(sid, "assistant", f"Answer {i}")
    service = WikiChatService(object(), wiki_resolver=object(), session_store=store)
    history = service._load_history(sid)
    assert len(history) == 12
    answer = service._build_prompt("当前问题", [], history)
    planner = service._tool_loop_prompt("当前问题", "query", history, [], 0, 4)
    native = service._native_tool_messages("当前问题", "query", history, [], 0, 4)
    for prompt in [answer, planner, json.dumps(native, ensure_ascii=False)]:
        assert "Question 0" in prompt and "Question 11" in prompt


def test_large_turn_and_partial_remaining_budget_keep_newest():
    budget = small_budget()
    history = [("old", "old"), ("new", "long log " * 2000)]
    text = budget.history_text(history, 500)
    assert "new" in text and "old" not in text and "省略" in text
    assert budget.counter.count(text) <= 500
    prompt = budget.compose("current " * 600, [("history", lambda cap: budget.history_text(history, cap), 1200)])
    assert "new" in prompt
    budget.check_request(prompt, output_tokens=1200)


def test_all_prompt_envelopes_include_tools_and_reserve_output():
    service = WikiChatService(object(), wiki_resolver=object(), context_budget=ContextBudget(ContextPolicy(window=32768, history=24000, planning_history=24000)))
    history = [("提问 " * 2000, "长日志 " * 5000)] * 10
    observations = [AgentToolObservation("wiki_open", "query", "ok", "资料 " * 20000)]
    cards = [{"id": "paper", "title": "QServe", "_full_text": "原文 " * 30000}]
    prompt = service._build_prompt("CURRENT-MUST-REMAIN", cards, history)
    native = service._native_tool_messages("CURRENT-MUST-REMAIN", "query", history, observations, 0, 4)
    fallback = service._tool_loop_prompt("CURRENT-MUST-REMAIN", "query", history, observations, 0, 4)
    budget = service.context_budget
    budget.check_request(prompt, output_tokens=1200)
    budget.check_request(fallback, output_tokens=900)
    budget.check_request(native, output_tokens=600, tools=service._native_tool_specs())
    for value in [prompt, fallback, json.dumps(native, ensure_ascii=False)]:
        assert "CURRENT-MUST-REMAIN" in value
    with pytest.raises(ContextBudgetExceeded):
        budget.check_request(native, output_tokens=600, tools=[{"schema": "巨大定义 " * 100000}])


def test_oversized_current_question_fails_without_silent_truncation():
    budget = small_budget()
    with pytest.raises(ContextBudgetExceeded):
        budget.compose("完整问题" * 5000, [])


def test_auto_compaction_keeps_raw_records_and_atomic_pair_boundary(tmp_path):
    store, sid = populated(tmp_path)
    before = store.get_messages(sid, 500)
    result = auto_compact(store, sid, SummaryLLM(), small_budget())
    assert result["status"] == "compacted"
    assert store.get_messages(sid, 500) == before
    settings = store.get_session_settings(sid)
    assert settings["unrelated"] == "preserve"
    assert settings["compacted_through_message_id"] == before[-3]["id"]
    history = store.get_history(sid, None)
    assert history[0][0] == "[SYSTEM_CONTEXT_SUMMARY]"
    assert history[-1][0].startswith("Question-5")
    assert auto_compact(store, sid, SummaryLLM(), small_budget())["status"] == "not_needed"


def test_failed_auto_summary_does_not_advance_coverage(tmp_path):
    store, sid = populated(tmp_path)
    result = auto_compact(store, sid, SummaryLLM(lambda: ""), small_budget())
    assert result["status"] == "failed"
    assert "compacted_through_message_id" not in store.get_session_settings(sid)
    assert len(store.get_history(sid, None)) == 6


def test_deletion_during_compaction_cannot_resurrect_session(tmp_path):
    store, sid = populated(tmp_path)
    def deleted():
        store.delete_session(sid)
        return "摘要"
    assert auto_compact(store, sid, SummaryLLM(deleted), small_budget())["status"] == "stale"
    assert store.get_session(sid) is None


def test_stale_compaction_cannot_overwrite_newer_summary(tmp_path):
    store, sid = populated(tmp_path)
    assert store.commit_context_summary(sid, "new", 2, 0, "")
    assert not store.commit_context_summary(sid, "stale", 4, 0, "")
    assert store.get_session_settings(sid)["context_summary"] == "new"


def test_chunked_summary_reads_tail_instead_of_marking_unseen_text_compacted():
    llm = SummaryLLM()
    records = [{"id": 1, "role": "user", "content": "START " + "abcd " * 9000 + " END-OF-LONG-MESSAGE"}]
    budget = small_budget()
    summary = summarize_records(records, "", budget, llm)
    assert summary and len(llm.prompts) > 1
    assert "END-OF-LONG-MESSAGE" in llm.prompts[-1]
    for prompt in llm.prompts:
        budget.check_request(prompt, output_tokens=1200)
    limited = SummaryLLM()
    with pytest.raises(ContextBudgetExceeded):
        summarize_records(records, "", budget, limited, max_chunks=4)
    assert not limited.prompts


def test_context_loading_and_manual_compaction_cover_more_than_500_records(tmp_path, monkeypatch):
    from backend.api import wiki
    store = SessionStore(str(tmp_path / "history.db"))
    sid = store.create_session()
    for i in range(260):
        store.save_message(sid, "user", f"QUESTION-{i}")
        store.save_message(sid, "assistant", f"ANSWER-{i}")
    assert len(store.get_context_messages(sid)) == 520
    llm = SummaryLLM()
    monkeypatch.setattr(wiki, "get_chat_llm", lambda: llm)
    result = wiki.compact_chat_session(sid, wiki.SessionCompactPayload(keep_recent_turns=2, use_llm=True),
                                       session_store=store, wiki_store=SimpleNamespace(db_path=store.db_path))
    assert result["compacted_message_count"] == 516
    combined = " ".join(llm.prompts)
    assert "QUESTION-0" in combined and "ANSWER-257" in combined
    assert "QUESTION-259" not in combined


def test_unicode_clipping_and_token_counter_metadata():
    counter = TokenCounter()
    for budget in [0, 32, 64, 150]:
        text = counter.clip("中文🙂代码const x=42; " * 1000, budget)
        assert counter.count(text) <= budget
        assert "�" not in text
    assert counter.mode in {"cl100k_estimate_1.25x", "utf8_byte_upper_estimate"}


def test_stream_auto_compaction_is_visible_and_persisted_in_trace(tmp_path):
    from system.wiki.wiki_chat import WikiToolPlan
    store, sid = populated(tmp_path)

    class Service(WikiChatService):
        def _run_tool_loop(self, *args, **kwargs):
            return {"plan": WikiToolPlan(), "cards": [], "web_results": [], "resources": [], "trace": {"tool_observations": []}}

        def _save_turn(self, *args, **kwargs):
            pass

    class LLM(SummaryLLM):
        def stream_invoke(self, prompt, **kwargs):
            yield "answer"

    service = Service(object(), wiki_resolver=object(), session_store=store, llm=LLM(), context_budget=small_budget())
    events = list(service.chat_stream("当前问题", sid))
    compact = [e for e in events if e.get("tool") == "context_compact"]
    assert [e["status"] for e in compact] == ["running", "done"]
    traces = [e["trace"] for e in events if e["type"] == "agent_trace"]
    assert any(item["status"] == "compacted" for item in traces[-1]["context_budget"]["compactions"])
    assert events[-1]["type"] == "done"


def test_configured_tokenizer_uses_local_file(tmp_path):
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace
    tokenizer = Tokenizer(WordLevel({"[UNK]": 0, "hello": 1, "world": 2}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = Whitespace()
    path = tmp_path / "tokenizer.json"
    tokenizer.save(str(path))
    counter = TokenCounter(str(path))
    assert counter.mode == "configured_tokenizer"
    assert counter.count("hello world") == 2

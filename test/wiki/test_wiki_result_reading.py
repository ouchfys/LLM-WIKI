"""Regression: a large two-paper comparison must not lose its second paper."""
import json

from system.conversation.session_store import SessionStore
from system.conversation.tool_reading import sections
from system.wiki.wiki_chat import AgentToolObservation
from test_citation_context import chat
from test_context_budget import populated, small_budget, SummaryLLM


def example():
    return {"query": "Compare MAGMA with Jev-Mem", "status": "done", "items": [
        {"card_id": "jev", "title": "Jev-Mem", "content": "# Method\n" + "甲" * 12000 + "\nJEV-END"},
        {"card_id": "magma", "title": "MAGMA", "content": "# Method\n" + "乙" * 12000 + "\nMAGMA-END\n## Experiments\n" + "E" * 6000},
    ]}


def test_both_controller_paths_get_complete_large_cards():
    service = chat()
    payload = example()
    observation = AgentToolObservation("wiki_open", payload["query"], "done", items=payload["items"], result_id=57)
    for text in [service._observation_context([observation]),
                 service._native_tool_messages("Compare", "Compare", [], [observation], 0, 4)[1]["content"],
                 service._tool_loop_prompt("Compare", "Compare", [], [observation], 0, 4)]:
        assert "JEV-END" in text and "MAGMA-END" in text
        assert "超过单条工具结果内联上限" not in text


def test_body_title_lookup_and_exact_section_paging(tmp_path):
    store = SessionStore(str(tmp_path / "test.db"))
    sid = store.create_session()
    rid = store.save_tool_result(sid, "wiki_open", {}, example())
    page = store.read_tool_result(sid, rid, query="MAGMA")[0]
    assert page["card_id"] == "magma" and "乙" in page["content"] and "甲" not in page["content"]
    first = store.read_tool_result(sid, rid, card_id="magma", section="Experiments")[0]
    second = store.read_tool_result(sid, rid, card_id="magma", section="s2", offset=first["next_offset"])[0]
    assert first["end_offset"] == second["offset"] and second["next_offset"] is None
    assert first["content"] + second["content"] == "## Experiments\n" + "E" * 6000
    assert first["content_hash"] == second["content_hash"]
    assert store.read_tool_result(sid, rid, card_id="missing") == []
    assert store.read_tool_result(store.create_session(), rid, card_id="magma") == []
    assert all(item["view"] == "directory" for item in store.read_tool_result(sid, rid))
    assert store.read_tool_result(sid, rid, card_id="magma", query="NOT-IN-BODY")[0]["match_found"] is False


def test_pressure_cursor_reconstructs_omitted_text_without_rereading_prefix(tmp_path):
    service = chat()
    payload = example()
    original = payload["items"][0]["content"]
    payload["items"] = payload["items"][:1]
    store = SessionStore(str(tmp_path / "test.db"))
    sid = store.create_session()
    rid = store.save_tool_result(sid, "wiki_open", {}, payload)
    observation = AgentToolObservation("wiki_open", "", "done", items=payload["items"], result_id=rid)
    text = service._observation_context([observation], budget=3000)
    assert service.context_budget.counter.count(text) <= 3000
    cursor_line = next(line for line in text.splitlines() if '"end_offset"' in line)
    cursor = json.loads(cursor_line)["next_offset"]
    assert original[:cursor] in text and cursor > 0
    rebuilt = original[:cursor]
    while cursor is not None:
        page = store.read_tool_result(sid, rid, card_id="jev", offset=cursor)[0]
        rebuilt += page["content"]
        cursor = page["next_offset"]
    assert rebuilt == original


def test_compact_preserves_stored_body_and_runtime_rebuilds_read_cursor(tmp_path):
    from system.conversation.context_compaction import auto_compact
    store, sid = populated(tmp_path)
    rid = store.save_tool_result(sid, "wiki_open", {}, example())
    page = store.read_tool_result(sid, rid, card_id="magma", section="s2")[0]
    recovery = {"tool": "read_tool_result", "status": "done", "items": [page]}
    service = chat()
    before = service._execution_state([recovery])
    assert auto_compact(store, sid, SummaryLLM(), small_budget())["status"] == "compacted"
    reopened = SessionStore(store.db_path)
    next_page = reopened.read_tool_result(sid, rid, card_id="magma", section="s2", offset=page["next_offset"])[0]
    state = service._execution_state([json.loads(json.dumps(recovery))])
    assert state == before
    assert state["recovered_pages"][0]["last_next_offset"] == next_page["offset"]
    assert next_page["content"] == "E" * (6000 - (4000 - len("## Experiments\n")))


def test_paging_does_not_inherit_user_query_and_pressure_keeps_callable():
    service = chat()
    for normalize, raw in [
        (service._normalize_native_tool_calls, {"tool_calls": [{"function": {"name": "read_tool_result", "arguments": '{"result_id": 57, "offset": 4000}'}}]}),
        (service._normalize_agent_tool_calls, {"tool_calls": [{"name": "read_tool_result", "arguments": {"result_id": 57, "offset": 4000}}]}),
    ]:
        assert normalize(raw, "MAGMA", 4)[0].arguments["query"] == ""
    budget = small_budget()
    with budget.on_pressure(lambda _: None):
        text = budget.compose("question", [("Previous observations", lambda cap: "actual-body " * min(cap // 12, 400), 6000)])
    assert "actual-body" in text and "<function" not in text


def test_section_index_ignores_code_headings():
    assert [item["title"] for item in sections("# Real\n```python\n# Fake\n```\n## Detail\ntext")] == ["Real", "Detail"]


def test_recovered_page_does_not_spill_again_and_targeted_tail_survives_final_pressure():
    from test_citation_context import papers, opened
    service = chat()
    page = {"tool": "wiki_open", "result_id": 57, "view": "page", "card_id": "magma",
            "offset": 90000, "end_offset": 114000, "next_offset": None, "content": "字" * 23990 + "RECOVER-END"}
    obs = AgentToolObservation("read_tool_result", "", "done", items=[page], result_id=58)
    assert "RECOVER-END" in service._observation_context([obs])
    assert "RECOVER-END" in service._answer_observation_context([service._observation_payload(obs)])
    service = chat(window=32768)
    cards = papers(2, size=60000)
    observations = [opened(cards, 57), {"tool": "read_tool_result", "status": "done", "items": [
        {**page, "card_id": "paper-1", "content": "ESSENTIAL-EXPERIMENT-TAIL", "offset": 59000,
         "end_offset": 59025}]}]
    prompt = service._build_prompt("Compare experiments", cards, [], tool_observations=observations)
    assert "ESSENTIAL-EXPERIMENT-TAIL" in prompt
    assert "END-PAPER-1" not in prompt

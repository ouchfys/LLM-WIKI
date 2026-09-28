"""Regressions from the MAGMA / Jev-Mem comparison; no provider calls."""
from types import SimpleNamespace

import pytest

from system.agent_runtime.control import RunControl
from system.conversation.session_store import SessionStore
from system.wiki.wiki_chat import AgentToolCall, AgentToolObservation, WikiChatService


def evidence(text="Original passage", element="ev-1"):
    return {"source_packet_id": "source-1", "element_id": element, "text": text}


def test_paraphrased_queries_with_same_evidence_stop_and_keep_full_results(monkeypatch):
    service = WikiChatService(object(), wiki_resolver=object(), llm=object())
    monkeypatch.setattr(service, "_next_agent_tool_calls", lambda **kw: [
        AgentToolCall("evidence_lookup", {"query": f"different wording {kw['step_index']}"})])
    monkeypatch.setattr(service, "_execute_agent_tool_call_impl", lambda call, *args: AgentToolObservation(
        call.name, call.arguments["query"], "done", "retrieved", [evidence()]))
    result = service._run_tool_loop("Compare two papers", "", [], max_steps=15)
    observations = result["trace"]["tool_observations"]
    assert result["trace"]["stop_reason"] == "evidence_no_progress"
    assert sum(item["tool"] == "evidence_lookup" for item in observations) == 4
    assert service._answer_observation_context(observations).count("Original passage") == 1
    assert sum(len(item["items"]) for item in observations if item["tool"] == "evidence_lookup") == 4
    assert any(item["tool"] == "runtime_evidence_feedback" for item in observations)


def test_repeated_evidence_stops_even_with_shell_checks_between_queries(monkeypatch):
    service = WikiChatService(object(), wiki_resolver=object(), llm=object())

    def next_call(**kwargs):
        step = kwargs["step_index"]
        if step % 2:
            return [AgentToolCall("local_shell", {"command": f"read source section {step}"})]
        return [AgentToolCall("evidence_lookup", {"query": f"Jev definition {step}"})]

    def execute(call, *args):
        if call.name == "local_shell":
            return AgentToolObservation(call.name, "", "done", "source inspected", [{"stdout": "same definition"}])
        return AgentToolObservation(call.name, call.arguments["query"], "done", "retrieved", [evidence()])

    monkeypatch.setattr(service, "_next_agent_tool_calls", next_call)
    monkeypatch.setattr(service, "_execute_agent_tool_call_impl", execute)
    result = service._run_tool_loop("What does Jev do?", "", [], max_steps=15)
    observations = result["trace"]["tool_observations"]
    assert result["trace"]["stop_reason"] == "evidence_no_progress"
    assert sum(item["tool"] == "evidence_lookup" for item in observations) == 4
    assert sum(item["tool"] == "local_shell" for item in observations) == 3


def test_changed_evidence_and_source_identity_are_not_deduplicated():
    service = WikiChatService(object(), wiki_resolver=object())
    observations = [AgentToolObservation("evidence_lookup", "q", "done", items=[item]) for item in (
        evidence(), evidence("Corrected passage"), {**evidence(), "source_packet_id": "other-source"})]
    assert len(service._evidence_keys(observations)) == 3
    assert len(service._context_observations(observations)) == 3


def test_full_evidence_is_recoverable_and_has_original_source_path():
    body = "x" * 2000 + " TABLE_DISAGREEMENT_AT_END"

    class Store:
        def list_card_links(self, card_id):
            return {"sources": [{"source_packet_id": "source-1"}]}

        def get_source_packet(self, source_id):
            return SimpleNamespace(raw_source_path="sources/paper_pdf/paper.md")

        def find_evidence(self, source_id, **kwargs):
            return [{"id": "ev-1", "text": body, "heading_path": ["Title", "Experiments"]}]

    service = WikiChatService(SimpleNamespace(get_card=lambda cid: None),
                              wiki_resolver=object(), evidence_store=Store())
    result = service._lookup_evidence(AgentToolCall("evidence_lookup", {"card_ids": ["paper"]}), "q", 6)
    assert result.items[0]["text"] == body
    assert result.items[0]["source_path"] == "sources/paper_pdf/paper.md"
    assert "not full-document coverage" in result.summary


def test_retired_memory_call_does_not_update_new_session(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPERWIKI_TASKS_ROOT", str(tmp_path / "tasks"))
    memory_root = str(tmp_path / "memory")
    sessions = SessionStore(str(tmp_path / "sessions.db"), memory_root=memory_root)
    sid = sessions.create_session()
    service = WikiChatService(object(), wiki_resolver=object(), session_store=sessions, llm=object())
    notes = "# Project Memory\n\n## Progress\n已完成 Paper A 与 Paper B 的比较；资料见 Wiki。\n"
    calls = []

    def controller(**kwargs):
        observations = kwargs["observations"]
        calls.append([o.tool for o in observations])
        if len(calls) == 1:
            return [AgentToolCall("project_memory_update", {"content": notes})]
        return []

    monkeypatch.setattr(service, "_next_agent_tool_calls", controller)
    control = RunControl(None, "", "Compare Paper A with Paper B")
    control.session_id = sid
    with control.bind():
        result = service._run_tool_loop(control.message, "", [], max_steps=8)
    assert [c.name for c in result["plan"].tools] == ["project_memory_update"]
    assert len(calls) == 2  # Write once, then the model ends the turn.
    assert not any("runtime_memory_review" in observed for observed in calls)
    assert result["trace"]["stop_reason"] == "model_finished"
    reopened = SessionStore(str(tmp_path / "sessions.db"), memory_root=memory_root)
    next_sid = reopened.create_session()
    assert "已完成 Paper A 与 Paper B" not in reopened.render_project_context(next_sid)
    rejected = next(o for o in result["trace"]["tool_observations"] if o["tool"] == "project_memory_update")
    assert rejected["status"] == "error"
    assert "not registered" in rejected["summary"]
    assert reopened.get_all_preferences() == {}


@pytest.mark.parametrize("message", ["你好", "解释已有比较结论，不需要保存新的经验"])
def test_no_memory_change_finishes_without_extra_model_decision(tmp_path, monkeypatch, message):
    monkeypatch.setenv("PAPERWIKI_TASKS_ROOT", str(tmp_path / "tasks"))
    sessions = SessionStore(str(tmp_path / "sessions.db"), memory_root=str(tmp_path / "memory"))
    sid = sessions.create_session()
    sessions.write_project_memory(sid, "# Project Memory\n\n已确认的旧约定保持有效。\n")
    service = WikiChatService(object(), wiki_resolver=object(), session_store=sessions, llm=object())
    calls = []

    def controller(**kwargs):
        calls.append([observation.tool for observation in kwargs["observations"]])
        return []

    monkeypatch.setattr(service, "_next_agent_tool_calls", controller)
    control = RunControl(None, "", message)
    control.session_id = sid
    before = sessions.render_project_context(sid)
    with control.bind():
        result = service._run_tool_loop(control.message, "", [], max_steps=8)
    assert len(calls) == 1
    assert not any("runtime_memory_review" in observed for observed in calls)
    assert result["plan"].tools == []
    assert result["trace"]["stop_reason"] == "model_finished"
    assert before == sessions.render_project_context(sid)

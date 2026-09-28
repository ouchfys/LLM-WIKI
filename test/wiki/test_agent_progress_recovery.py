"""Behavioral checks for long turns, recoverable observations and public trace."""
import json
import time
from types import SimpleNamespace

from system.agent_runtime import AgentRunStore
from system.agent_runtime.control import RunControl
from system.conversation.session_store import SessionStore
from system.wiki.wiki_chat import AgentToolCall, AgentToolObservation, WikiChatService, WikiToolPlan


def service():
    return WikiChatService(wiki_store=object(), wiki_resolver=object(), llm=object())


def test_default_loop_can_finish_more_than_twelve_real_steps(monkeypatch):
    chat = service()
    calls = [[AgentToolCall("local_shell", {"command": str(i)})] for i in range(14)] + [[]]
    monkeypatch.delenv("PAPERWIKI_MAX_TOOL_STEPS", raising=False)
    monkeypatch.setattr(chat, "_next_agent_tool_calls", lambda **kwargs: calls.pop(0))
    monkeypatch.setattr(chat, "_execute_agent_tool_call", lambda call, *args:
                        AgentToolObservation(call.name, "", "done", "executed"))
    result = chat._run_tool_loop("read several sources", "", [])
    assert len(result["plan"].tools) == 14
    assert result["trace"]["stop_reason"] == "model_finished"


def test_failed_call_can_retry_but_success_is_not_reexecuted(monkeypatch):
    chat = service()
    call = AgentToolCall("local_shell", {"command": "fetch"})
    planned = [[call], [call], [call], []]
    outcomes = ["error", "done"]
    observed_feedback = []

    def plan(**kwargs):
        observed_feedback.extend(item.tool for item in kwargs["observations"])
        return planned.pop(0)

    monkeypatch.setattr(chat, "_next_agent_tool_calls", plan)
    monkeypatch.setattr(chat, "_execute_agent_tool_call", lambda call, *args:
                        AgentToolObservation(call.name, "", outcomes.pop(0), "result"))
    result = chat._run_tool_loop("read source", "", [])
    assert len(result["plan"].tools) == 2
    assert "runtime_call_feedback" in observed_feedback
    assert result["trace"]["stop_reason"] == "model_finished"


def test_loop_budget_is_reported_as_unfinished(monkeypatch):
    chat = service()
    monkeypatch.setattr(chat, "_next_agent_tool_calls", lambda **kwargs:
                        [AgentToolCall("local_shell", {"command": str(kwargs["step_index"])})])
    monkeypatch.setattr(chat, "_execute_agent_tool_call", lambda call, *args:
                        AgentToolObservation(call.name, "", "done", "executed"))
    events = []
    result = chat._run_tool_loop("work", "", [], max_steps=2, event_callback=events.append)
    assert result["trace"]["stop_reason"] == "step_budget_exhausted"
    assert result["trace"]["tool_observations"][-1]["tool"] == "runtime_stop"
    assert events[-1]["phase"] == "blocked"


def test_historical_active_plan_does_not_gate_a_new_question(monkeypatch):
    chat = service()
    monkeypatch.setattr(chat, "_task_plan_context", lambda: "active_task_id: a\n# Done When\nread sources")
    rounds = []

    def plan(**kwargs):
        rounds.append([item.tool for item in kwargs["observations"]])
        return []

    monkeypatch.setattr(chat, "_next_agent_tool_calls", plan)
    chat._run_tool_loop("research", "", [])
    assert rounds == [[]]


def test_latest_job_state_and_opened_ids_survive_old_observations():
    observations = [
        AgentToolObservation("arxiv_import_paper", "", "done", items=[{"job_id": "job-1", "status": "queued"}]),
        AgentToolObservation("arxiv_ingestion_status", "", "done", items=[{"id": "job-1", "status": "completed", "paper_card_id": "p1"}]),
        AgentToolObservation("wiki_open", "", "done", items=[{"card_id": "p1"}]),
    ]
    state = WikiChatService._execution_state(observations)
    assert state["latest_ingestion_jobs"]["job-1"]["status"] == "completed"
    assert state["opened_card_ids"] == ["p1"]
    assert "does not prove" in state["note"]


def test_public_progress_uses_only_explicit_content(monkeypatch):
    chat = service()
    chat.llm = SimpleNamespace(tool_call=lambda **kwargs: None)
    monkeypatch.setattr(chat, "_native_tool_messages", lambda **kwargs: [])
    monkeypatch.setattr(chat, "_call_llm_tools", lambda **kwargs: {
        "content": "正在读取已入库的论文。", "reasoning_content": "PRIVATE_REASONING",
        "tool_calls": [{"function": {"name": "wiki_open", "arguments": '{"card_ids": ["p1"]}'}}],
    })
    control = RunControl(None, "", "")
    with control.bind():
        chat._next_native_tool_calls("read", "", [], [], 0, 6)
    assert control.loop_state["pending_progress"] == "正在读取已入库的论文。"
    assert "PRIVATE_REASONING" not in json.dumps(control.loop_state)


def test_full_card_is_saved_but_public_trace_is_bounded(tmp_path):
    chat = service()
    body = "# A paper\n\n" + ("Complete scientific content. " * 700) + "TAIL_EVIDENCE"
    path = tmp_path / "paper.md"
    path.write_text(body, encoding="utf-8")
    chat.wiki_store = SimpleNamespace(vault=SimpleNamespace(resolve_markdown_path=lambda value: path))
    card = {"id": "p1", "title": "paper", "markdown_path": str(path)}
    card["_full_text"] = chat._read_card_markdown(card)
    assert "TAIL_EVIDENCE" in card["_full_text"]
    item = chat._trace_card(card)
    assert "TAIL_EVIDENCE" in item["content"]
    observation = {"tool": "wiki_open", "status": "done", "items": [item], "result_id": 1}
    assert "TAIL_EVIDENCE" in chat._answer_observation_context([observation])
    public = chat._public_trace({"tool_observations": [observation], "retrieved_cards": [item]})
    assert "TAIL_EVIDENCE" not in json.dumps(public)
    assert public["tool_observations"][0]["result_id"] == 1


def test_independent_tool_durations_are_measured_inside_each_worker(monkeypatch):
    chat = service()
    def execute(call, *args):
        time.sleep(call.arguments["delay"])
        return AgentToolObservation(call.name, "", "done", "read")
    monkeypatch.setattr(chat, "_execute_agent_tool_call_impl", execute)
    observations = chat._execute_tool_batch([
        AgentToolCall("wiki_open", {"delay": 0.01}),
        AgentToolCall("wiki_open", {"delay": 0.12}),
    ], [], [], [], 6)
    assert observations[1].duration_ms > observations[0].duration_ms + 50


def test_failed_turn_retains_public_timeline_for_history(tmp_path):
    chat = service()
    sessions = SessionStore(str(tmp_path / "sessions.db"))
    sid = sessions.create_session()
    chat.session_store = sessions
    chat.runtime = AgentRunStore(db_path=str(tmp_path / "runtime.db"))
    run_id, _ = chat._start_chat_runtime("research", sid, 6)
    control = RunControl(chat.runtime, run_id, "research")
    chat._record_public_event(control.loop_state, {
        "type": "progress", "text": "正在读取论文。", "reasoning_content": "PRIVATE",
    }, run_id)
    chat._record_public_event(control.loop_state, {
        "type": "tool_status", "event_id": "tool:1", "tool": "wiki_open", "status": "running",
    }, run_id)
    chat._persist_failed_turn(control, sid, RuntimeError("connection failed"))
    messages = sessions.get_messages(sid)
    metadata = messages[-1]["metadata"]
    assert metadata["failed"] is True
    assert metadata["trace"]["timeline"][-1]["status"] == "error"
    assert "PRIVATE" not in json.dumps(metadata)
    assert any(event["event_type"] == "chat.public_event" for event in chat.runtime.list_events(run_id))


def test_completed_stream_history_replays_public_events_and_final_runtime(tmp_path, monkeypatch):
    chat = service()
    chat.llm = None
    chat.session_store = SessionStore(str(tmp_path / "sessions.db"))
    sid = chat.session_store.create_session()
    chat.runtime = AgentRunStore(db_path=str(tmp_path / "runtime.db"))

    def run_loop(*args, event_callback, **kwargs):
        event_callback({"type": "progress", "text": "正在核对原始证据。"})
        event = {"type": "tool_status", "event_id": "tool:1", "tool": "wiki_open"}
        event_callback({**event, "status": "running"})
        event_callback({**event, "status": "done", "result_id": 4})
        return {"plan": WikiToolPlan(), "cards": [], "web_results": [], "resources": [],
                "trace": {"tool_observations": [{"tool": "wiki_open", "result_id": 4,
                    "items": [{"card_id": "p1", "content": "LARGE_BODY" * 3000}]}]}}

    monkeypatch.setattr(chat, "_run_tool_loop", run_loop)
    monkeypatch.setattr(chat, "_answer", lambda *args, **kwargs: "已完成核对。")
    events = list(chat.chat_stream("核对论文", sid))
    trace = chat.session_store.get_messages(sid)[-1]["metadata"]["trace"]
    assert [event["type"] for event in trace["timeline"]] == ["progress", "tool_status", "tool_status"]
    assert trace["runtime"]["current_state"] == "COMPLETED"
    assert trace["tool_observations"][0]["result_id"] == 4
    assert "LARGE_BODY" not in json.dumps(trace)
    assert events[-1]["type"] == "done"

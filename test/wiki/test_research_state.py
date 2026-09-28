"""Semantic decisions are stubbed; provenance, stopping and recovery are real."""
import json
import re
from types import SimpleNamespace

import pytest

from system.agent_runtime import AgentRunStore
from system.agent_runtime.control import RunControl
from system.conversation.session_store import SessionStore
from system.wiki.research_state import ResearchState, evidence_sources
from system.wiki.wiki_chat import AgentToolCall, AgentToolObservation, WikiChatService


BODY = "Jev-Mem 是一种智能体记忆架构。Jev 控制证据选择、记忆操作和计算预算。"


def observation(body=BODY, tool="wiki_open", version="v1"):
    return AgentToolObservation(tool, "", "done", "opened", [
        {"card_id": "paper", "content": body, "read_version": version, "title": "来源"}])


def question(qid="q1", text="它是什么，有何用途？", source=None, quote=BODY, **extra):
    return {"id": qid, "question": text, "answer": quote if source else "",
            "coverage": "covered" if source else "missing",
            "support": [{"source_id": source, "quote": quote}] if source else [],
            "against": [], "resolution": "", "resolution_evidence": [], **extra}


def assessment(questions, decision="continue"):
    return {"decision": decision, "questions": questions}


def test_host_validates_quotes_and_freezes_general_question_contract():
    sources = evidence_sources([observation()])
    sid = next(iter(sources))
    state = ResearchState("它是什么，有何用途？")
    false_ref = assessment([question(source=sid, quote="这段话并没有出现")])
    with pytest.raises(ValueError, match="Quote"):
        state.apply_assessment(false_ref, sources)
    assert not state.data["contract_set"]
    assert state.apply_assessment(assessment([question(source=sid)]), sources) == "evidence_sufficient"
    with pytest.raises(ValueError, match="established"):
        state.apply_assessment(assessment([question(text="顺便研究起源", source=sid)]), sources)
    with pytest.raises(ValueError):
        state.apply_assessment(assessment([]), sources)


def test_only_real_bodies_are_evidence_and_versions_invalidate_old_quotes():
    obs = observation()
    metadata = AgentToolObservation("wiki_search", "", "done", "definition", [{"title": "A", "summary": BODY}])
    plan = observation(tool="task_plan_write")
    sources = evidence_sources([obs, metadata, plan])
    assert len(sources) == 1
    state = ResearchState("用途")
    state.apply_assessment(assessment([question(source=next(iter(sources)))]), sources)
    changed = evidence_sources([obs, observation("新版本未说明用途。", version="v2")])
    state.refresh_sources(changed)
    assert state.unresolved() == ["q1"]
    assert len(changed) == 1
    assert state.data["questions"][0]["support"] == []


def test_opposing_evidence_cannot_be_silently_dropped_and_resolution_is_grounded():
    sources = evidence_sources([observation(BODY + "相反的实验发现控制器没有改善召回。")])
    sid = next(iter(sources))
    q = question(source=sid, against=[{"source_id": sid, "quote": "控制器没有改善召回"}])
    state = ResearchState("控制器的作用")
    assert state.apply_assessment(assessment([q]), sources) == ""
    assert state.data["questions"][0]["status"] == "conflict"
    dropped = question(source=sid)
    state.apply_assessment(assessment([dropped]), sources)
    assert state.data["questions"][0]["status"] == "conflict"
    state.finish("evidence_unresolved")
    assert state.data["status"] == "conflicted"
    bad_resolution = {**q, "resolution": "条件不同", "resolution_evidence": [{"source_id": sid, "quote": "不存在的条件"}]}
    with pytest.raises(ValueError, match="Quote"):
        state.apply_assessment(assessment([bad_resolution]), sources)


def test_new_keywords_and_new_irrelevant_sources_do_not_reset_progress():
    state = ResearchState("比较甲和乙", "research")
    obs = [observation()]
    for index in range(4):
        obs.append(observation(f"第 {index} 个无关结果", tool="local_shell"))
        sources = evidence_sources(obs)
        sid = next(iter(sources))
        reason = state.apply_assessment(assessment([
            question("q1", "甲的方法", source=sid), question("q2", "乙的方法")]), sources)
    assert reason == "no_information_gain"
    assert state.data["no_gain_rounds"] == 3
    state.finish(reason)
    assert state.data["status"] == "insufficient"


def test_research_action_must_bind_to_missing_question_and_metadata_does_not_bypass_dedupe():
    state = ResearchState("比较甲乙", "research")
    sources = evidence_sources([observation()])
    state.apply_assessment(assessment([question(source=next(iter(sources))), question("q2", "乙")]), sources)
    call = AgentToolCall("local_shell", {"command": "read paper", "gap_id": "q1", "query_purpose": "再核实"})
    assert not state.allow_action(call)[0]
    call.arguments["gap_id"] = "q2"
    assert state.allow_action(call)[0]
    service = WikiChatService(object(), wiki_resolver=object())
    signature = service._tool_signature(call)
    call.arguments.update(gap_id="another", query_purpose="换个说法")
    assert service._tool_signature(call) == signature


def test_refining_a_missing_question_is_allowed_but_does_not_count_as_evidence_gain():
    state = ResearchState("比较甲乙的条件", "research")
    state.apply_assessment(assessment([question()]), {})
    refined = question("q2", "乙实验的基线条件", parent_id="q1", necessity="需要同条件比较")
    state.apply_assessment(assessment([question(), refined]), {})
    assert state.unresolved() == ["q1", "q2"]
    assert state.data["no_gain_rounds"] == 1
    with pytest.raises(ValueError, match="parent_id"):
        state.apply_assessment(assessment([question(), refined, question("q3", "题外话")]), {})


def test_partial_coverage_is_not_marked_complete():
    sources = evidence_sources([observation()])
    state = ResearchState("复杂问题")
    state.apply_assessment(assessment([question(source=next(iter(sources)), coverage="partial")]), sources)
    assert state.unresolved() == ["q1"]
    assert state.data["questions"][0]["status"] == "partial"


def service_with_tools(monkeypatch, mode="chat", body=BODY):
    service = WikiChatService(object(), wiki_resolver=object(), llm=SimpleNamespace(invoke=lambda *a, **k: ""))
    control = RunControl(None, "", "解释这套方法及控制器用途")
    control.loop_state["research_mode"] = mode == "research"
    calls = []

    def propose(**kwargs):
        calls.append(kwargs["step_index"])
        return [AgentToolCall("local_shell", {"command": f"read-{len(calls)}", "gap_id": "q1", "query_purpose": "确认用途"})]

    monkeypatch.setattr(service, "_next_agent_tool_calls", propose)
    monkeypatch.setattr(service, "_execute_agent_tool_call_impl", lambda call, *args:
                        observation(body, tool=call.name))
    return service, control, calls


@pytest.mark.parametrize("prompt", ["jev-mem是什么，这里面的jev是用来干嘛的", "比较甲、乙的方法，说明适用条件", "用中文解释控制器如何工作"])
def test_answerable_request_finishes_when_main_model_returns_no_tools(monkeypatch, prompt):
    service, control, decisions = service_with_tools(monkeypatch)
    def decide(**kwargs):
        decisions.append(kwargs["step_index"])
        return [] if kwargs["observations"] else [AgentToolCall("local_shell", {"command": "read definition"})]
    monkeypatch.setattr(service, "_next_agent_tool_calls", decide)
    monkeypatch.setattr(service, "_assess_research_state", lambda *a: pytest.fail("Unexpected assessment"))
    with control.bind():
        result = service._run_tool_loop(prompt, prompt, [], max_steps=10)
    assert decisions == [0, 1]
    assert result["trace"]["stop_reason"] == "model_finished"
    assert "research_state" not in result["trace"]


def test_partial_answer_is_bounded_by_total_steps_not_semantic_no_gain(monkeypatch):
    service, control, decisions = service_with_tools(monkeypatch)
    monkeypatch.setattr(service, "_assess_research_state", lambda state, sources, *args:
        assessment([question(source=next(iter(sources))), question("q2", "未找到的实现细节")]))
    # Normal chat binds a call to the remaining question even if it has no explicit gap id.
    def propose(**kwargs):
        decisions.append(kwargs["step_index"])
        return [AgentToolCall("local_shell", {"command": f"read-{len(decisions)}"})]
    monkeypatch.setattr(service, "_next_agent_tool_calls", propose)
    with control.bind():
        result = service._run_tool_loop(control.message, "", [], max_steps=15)
    assert len(decisions) == 15
    assert result["trace"]["stop_reason"] == "step_budget_exhausted"
    assert "research_state" not in result["trace"]


def test_source_check_requested_by_main_model_is_executed(monkeypatch):
    service, control, _ = service_with_tools(monkeypatch)
    executed = []
    monkeypatch.setattr(service, "_next_agent_tool_calls", lambda **kwargs: [
        AgentToolCall("wiki_open", {"card_ids": ["paper"]}),
        AgentToolCall("evidence_lookup", {"query": "再次核实定义"})])
    monkeypatch.setattr(service, "_execute_agent_tool_call_impl", lambda call, *args:
                        (executed.append(call.name) or observation(tool=call.name)))
    monkeypatch.setattr(service, "_assess_research_state", lambda state, sources, *args:
                        assessment([question(source=next(iter(sources)))]))
    with control.bind():
        result = service._run_tool_loop(control.message, "", [], max_steps=5)
    assert executed == ["wiki_open", "evidence_lookup"]
    assert result["trace"]["stop_reason"] == "step_budget_exhausted"


def test_pending_polling_does_not_consume_evidence_progress_rounds(monkeypatch):
    service, control, _ = service_with_tools(monkeypatch)
    state = ResearchState("等待入库并阅读论文", "research")
    state.apply_assessment(assessment([question()]), {})
    monkeypatch.setattr(service, "_assess_research_state", lambda *args: pytest.fail("Polling is not research"))
    observations = []
    for _ in range(5):
        observations.append(AgentToolObservation("arxiv_ingestion_status", "", "done", "running",
                            [{"job_id": "job1", "status": "running"}]))
        assert service._check_research_progress(state, observations, []) == ""
    assert state.data["no_gain_rounds"] == 0


@pytest.mark.parametrize("stream", [False, True])
def test_api_passes_explicit_research_mode(stream):
    import asyncio
    from backend.api.wiki import WikiChatPayload, chat_with_wiki
    from system.wiki.wiki_chat import WikiChatResult
    captured = []
    class Service:
        def chat(self, message, **kwargs):
            captured.append(kwargs)
            return WikiChatResult(answer="done")
        def chat_stream(self, message, **kwargs):
            captured.append(kwargs)
            yield {"type": "done"}
    response = chat_with_wiki(WikiChatPayload(message="问题", research_mode=True, stream=stream), Service())
    if stream:
        async def consume():
            return [chunk async for chunk in response.body_iterator]
        asyncio.run(consume())
    assert captured == [{"session_id": "", "research_mode": True}]


def test_invalid_legacy_assessor_cannot_interrupt_main_model(monkeypatch):
    service, control, decisions = service_with_tools(monkeypatch)
    evaluations = []
    def invalid(*args):
        evaluations.append(1)
        return {"decision": "answer", "questions": []}
    monkeypatch.setattr(service, "_assess_research_state", invalid)
    with control.bind():
        result = service._run_tool_loop(control.message, "", [], max_steps=15)
    assert evaluations == []
    assert len(decisions) == 15
    assert result["trace"]["stop_reason"] == "step_budget_exhausted"


def test_research_mode_uses_total_budget_without_initial_semantic_gate(monkeypatch):
    service, control, decisions = service_with_tools(monkeypatch, mode="research")
    monkeypatch.setattr(service, "_assess_research_state", lambda *args: {})
    with control.bind():
        result = service._run_tool_loop(control.message, "", [], max_steps=15)
    assert len(decisions) == 15
    assert len(result["plan"].tools) == 15
    assert result["trace"]["stop_reason"] == "step_budget_exhausted"


def test_errors_and_navigation_are_never_opposing_source_evidence():
    rows = [AgentToolObservation("repository", "", "error", "403", [{"kind": "operational_error", "content": "403 forbidden"}]),
            AgentToolObservation("repository", "", "done", "paths", [{"kind": "navigation", "content": "memory.py"}]),
            AgentToolObservation("local_shell", "", "done", "ok", [{"stdout": "ERR: 远程服务器返回错误: (403) 已禁止。", "exit_code": 0}])]
    assert evidence_sources(rows) == {}


def test_partial_question_can_gain_new_grounded_facts():
    state = ResearchState("研究记忆系统")
    rows = []
    for index in range(6):
        rows.append(observation(f"新增实现细节 {index}：读取、写入或压缩的原始代码。", tool="repository"))
        sources = evidence_sources(rows)
        sid = list(sources)[-1]
        q = question(source=sid, quote=sources[sid]["body"], coverage="partial")
        assert state.apply_assessment(assessment([q]), sources) == ""
    assert state.data["no_gain_rounds"] == 0
    assert state.unresolved() == ["q1"]
    for _ in range(2):
        reason = state.apply_assessment(assessment([q]), sources)
    assert reason == "no_information_gain"


def test_span_ids_resolve_to_original_text_and_actions_require_receipts():
    sources = evidence_sources([observation()])
    sid = next(iter(sources))
    span = sources[sid]["spans"][0]
    q = question(source=sid)
    q["support"] = [{"source_id": sid, "span_id": span["span_id"]}]
    state = ResearchState("定义")
    assert state.apply_assessment(assessment([q]), sources) == "evidence_sufficient"
    assert state.data["questions"][0]["support"][0]["quote"] == BODY
    q["kind"] = "action"
    with pytest.raises(ValueError, match="execution receipt"):
        ResearchState("写入").apply_assessment(assessment([q]), sources)


def test_legacy_assessor_errors_do_not_run_at_read_boundaries(monkeypatch):
    service, control, decisions = service_with_tools(monkeypatch)
    attempts = []
    def assess(state, sources, *args):
        attempts.append(1)
        if len(attempts) <= 2:
            raise ValueError("Quote is absent")
        return assessment([question(source=next(iter(sources)))])
    monkeypatch.setattr(service, "_assess_research_state", assess)
    with control.bind():
        result = service._run_tool_loop(control.message, "", [], max_steps=10)
    assert len(decisions) == 10
    assert attempts == []
    assert result["trace"]["stop_reason"] == "step_budget_exhausted"


def test_open_and_checkout_are_bounded_navigation_progress(monkeypatch):
    service, control, _ = service_with_tools(monkeypatch)
    state = ResearchState("研究仓库")
    observations = []
    for item in [{"kind": "navigation", "snapshot_id": "abc", "url": "https://github.com/a/b"},
                 {"kind": "navigation", "snapshot_id": "abc", "checkout_path": "cache/abc"}]:
        observations.append(AgentToolObservation("repository", "", "done", "navigation", [item]))
        assert service._check_research_progress(state, observations, []) == ""
    assert state.data["no_gain_rounds"] == 0
    for _ in range(2):
        observations.append(observations[-1])
        reason = service._check_research_progress(state, observations, [])
    assert reason == "no_information_gain"


def test_budget_is_enforced_within_a_parallel_batch_without_assessment(monkeypatch):
    service, control, decisions = service_with_tools(monkeypatch)
    state = ResearchState(control.message)
    state.data["budget"]["max_calls"] = 1
    control.loop_state["research_state"] = state.report()
    executed = []
    monkeypatch.setattr(service, "_next_agent_tool_calls", lambda **kwargs: [
        AgentToolCall("wiki_open", {"card_ids": [str(i)]}) for i in range(8)])
    monkeypatch.setattr(service, "_execute_agent_tool_call_impl", lambda call, *args:
                        (executed.append(call) or observation()))
    monkeypatch.setattr(service, "_assess_research_state", lambda *args: assessment([question()]))
    with control.bind():
        result = service._run_tool_loop(control.message, "", [], max_steps=1)
    assert len(executed) == 1
    assert result["trace"]["tool_budget"]["used_calls"] == 1
    assert result["trace"]["stop_reason"] == "step_budget_exhausted"


def test_finalizer_gets_validated_quotes_and_uncertainty_with_no_tools(monkeypatch):
    service = WikiChatService(object(), wiki_resolver=object(), llm=SimpleNamespace(invoke=lambda *a, **k: ""))
    state = ResearchState("问题")
    sources = evidence_sources([observation()])
    state.apply_assessment(assessment([question(source=next(iter(sources))), question("q2", "未知事项")]), sources)
    state.finish("budget_exhausted")
    prompts = []
    monkeypatch.setattr(service, "_invoke_llm", lambda prompt, **kwargs: prompts.append((prompt, kwargs)) or "结论")
    monkeypatch.setattr(service, "_call_llm_tools", lambda **kwargs: pytest.fail("Finalizer exposed tools"))
    assert service._answer("问题", [], [], [], [], research_state=state.report()) == "结论"
    assert BODY in prompts[0][0] and "budget_exhausted" in prompts[0][0]
    assert "tool-free stage" in prompts[0][0]
    assert "tools" not in prompts[0][1]


def test_native_controller_finishes_without_secondary_model_assessment(monkeypatch):
    prompts, tool_decisions = [], []
    class Model:
        def tool_call(self, **kwargs):
            tool_decisions.append(kwargs)
            if len(tool_decisions) > 1:
                return {"content": "Ready to answer"}
            return {"tool_calls": [{"type": "function", "function": {
                "name": "wiki_open", "arguments": json.dumps({"card_ids": ["paper"]})}}]}

        def invoke(self, prompt, **kwargs):
            prompts.append(prompt)
            assert "Original user request:\n用通俗的话解释它的用途" in prompt
            assert "You have no tools" in prompt
            sid = re.search(r'"source_id": "(src_[a-f0-9]+)"', prompt)[1]
            return json.dumps(assessment([question(source=sid)]), ensure_ascii=False)

    service = WikiChatService(object(), wiki_resolver=object(), llm=Model())
    monkeypatch.setattr(service, "_execute_agent_tool_call_impl", lambda call, *args: observation())
    result = service._run_tool_loop("用通俗的话解释它的用途", "Jev use", [], max_steps=10)
    assert len(tool_decisions) == 2 and prompts == []
    assert result["trace"]["stop_reason"] == "model_finished"


def test_recovery_restores_mode_contract_sources_attempts_and_budget(tmp_path):
    sessions = SessionStore(str(tmp_path / "state.db"))
    sid = sessions.create_session()
    runtime = AgentRunStore(sessions.db_path)
    service = WikiChatService(object(), wiki_resolver=object(), session_store=sessions, runtime=runtime)
    control, _ = service._prepare_chat("研究问题", sid, 6, research_mode=True)
    state = ResearchState(control.message, "research")
    sources = evidence_sources([observation()])
    state.apply_assessment(assessment([question(source=next(iter(sources))), question("q2", "未知")]), sources)
    call = AgentToolCall("evidence_lookup", {"query": "原始研究", "gap_id": "q2", "query_purpose": "核对未知"})
    state.record_attempt(call)
    try:
        with control.bind():
            runtime.save_chat_cursor(control.run_id, control.message, 2, research_state=state.report())
        runtime.request_pause(control.run_id)
    finally:
        control.lease.stop()
    resumed, _ = service._prepare_chat("", sid, 6, resume_run_id=control.run_id, research_mode=False)
    try:
        assert resumed.loop_state["research_mode"] is True
        restored = resumed.loop_state["research_state"]
        assert restored == state.report()
        assert restored["budget"]["used_calls"] == 1
        assert resumed.loop_state["planner_steps"] == 2
        changed = ResearchState("补充新要求", "research", restored)
        assert changed.data["budget"]["used_calls"] == 1
        assert not changed.data["contract_set"]
    finally:
        resumed.lease.stop()

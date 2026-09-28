"""Regressions from the four-repository trace: raw feedback and bounded continuation."""
from contextlib import closing
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from system.agent_runtime import AgentRunStore
from system.agent_runtime.control import RunControl
from system.agent_runtime.repositories import RepositoryReader, RepositoryError
from system.conversation.session_store import SessionStore
from system.search.web_search import WebSearchTool
from system.wiki.wiki_chat import WikiChatService, AgentToolCall, AgentToolObservation


def bare_service(**kwargs):
    return WikiChatService(object(), wiki_resolver=object(), **kwargs)


@pytest.mark.parametrize("raw", ['{"title":"bad "quote""}', '[]', 'null', '', 12])
def test_bad_arguments_return_real_error_without_executing(raw, monkeypatch):
    service = bare_service()
    calls = service._normalize_native_tool_calls({"tool_calls": [
        {"id": "bad-call", "function": {"name": "wiki_write", "arguments": raw}}]}, "research", 6)
    calls = service._apply_query_tool_policy(calls, message="research", effective_query="",
                                             observations=[], limit=6)
    monkeypatch.setattr(service, "_execute_agent_tool_call_impl", lambda *a: pytest.fail("Malformed call executed"))
    obs = service._execute_agent_tool_call(calls[0], [], [], [], 6)
    assert obs.call_id == "bad-call"
    assert obs.items[0]["raw_arguments"] == raw
    assert obs.items[0]["executed"] is False
    assert obs.items[0]["error_code"] == "invalid_tool_arguments"
    assert obs.summary == calls[0].argument_error


def test_parse_failure_is_a_tool_reply_and_model_can_correct_it(monkeypatch):
    requests = []
    malformed = '{"command":"echo "bad""}'

    class Model:
        def tool_call(self, messages, **kwargs):
            requests.append(deepcopy(messages))
            if len(requests) > 2:
                return {"content": ""}
            args = malformed if len(requests) == 1 else json.dumps({"command": "echo fixed"})
            return {"tool_calls": [{"id": f"c{len(requests)}", "function": {
                "name": "local_shell", "arguments": args}}]}

    service = bare_service(llm=Model())
    executed = []
    original = {"stdout": "partial\n中文\n", "stderr": 'fatal: "actual error"\r\n',
                "exit_code": 7, "cwd": "E:/project", "shell": "powershell", "timed_out": False}
    service.local_shell = SimpleNamespace(run=lambda command, **kw: executed.append(command) or original)
    monkeypatch.setattr(service, "_native_tool_messages", lambda **kw: [{"role": "user", "content": "test"}])
    control = RunControl(None, "", "test")
    with control.bind():
        result = service._run_tool_loop("test", "", [], max_steps=5)
    assert executed == ["echo fixed"]
    error = next(m for m in requests[1] if m.get("tool_call_id") == "c1")
    assert "Expecting ',' delimiter" in json.loads(error["content"])["summary"]
    shell = next(m for m in requests[2] if m.get("tool_call_id") == "c2")
    assert json.loads(shell["content"])["items"][0] == original
    assert result["trace"]["tool_budget"]["used_calls"] == 1


def test_fallback_json_error_returns_original_output_and_continues(monkeypatch):
    broken = '{"tool_calls": [not JSON]}'
    replies = iter([broken, '{"tool_calls": [{"name":"local_shell","arguments":{"command":"echo fixed"}}]}', '{"finish":true}'])
    prompts, executed = [], []
    class Model:
        def invoke(self, prompt, **kw):
            prompts.append(prompt)
            return next(replies)
    service = bare_service(llm=Model())
    service.local_shell = SimpleNamespace(run=lambda command, **kw: executed.append(command) or {"exit_code": 0, "stdout": "fixed"})
    control = RunControl(None, "", "test")
    with control.bind():
        result = service._run_tool_loop("test", "", [], max_steps=5)
    errors = [o for o in control.loop_state["observations"] if o.tool == "runtime_tool_parse_error"]
    assert errors[0].items[0]["raw_output"] == broken
    assert "not JSON" in prompts[1]
    assert executed == ["echo fixed"]
    assert result["trace"]["stop_reason"] == "model_finished"


def test_web_search_preserves_model_query_and_failure(monkeypatch):
    query = ('DeepSeek Harness ' + 'official source ' * 20).strip()
    seen = []

    def search(value, **kw):
        seen.append(value)
        raise RuntimeError("HTTP 503 from search provider")

    service = bare_service(web_search=SimpleNamespace(available=True, search=search))
    calls = service._apply_query_tool_policy([AgentToolCall("web_search", {"query": query})],
        message="research", effective_query="", observations=[], limit=6)
    obs = service._execute_agent_tool_call(calls[0], [], [], [], 6)
    assert seen == [query]
    assert obs.status == "error" and obs.summary == "HTTP 503 from search provider"


def test_web_search_network_error_is_not_empty_results(monkeypatch):
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **kw: (_ for _ in ()).throw(requests.ConnectionError("offline")))
    with pytest.raises(requests.ConnectionError, match="offline"):
        WebSearchTool().search("project source", raise_errors=True)


def test_search_202_is_returned_with_original_response_not_zero_results(monkeypatch):
    import requests
    response = requests.Response()
    response.status_code, response.url = 202, "https://html.duckduckgo.com/html/"
    response._content = b'<html>Request pending: actual response body</html>'
    monkeypatch.setattr(requests, "get", lambda *a, **kw: response)
    service = bare_service(web_search=WebSearchTool())
    obs = service._execute_agent_tool_call(AgentToolCall("web_search", {"query": "project source"}), [], [], [], 6)
    assert obs.status == "error"
    assert obs.items[0]["status_code"] == 202
    assert obs.items[0]["response_body"] == response.text


def test_git_error_keeps_both_streams_and_exit_code(tmp_path, monkeypatch):
    import subprocess
    stdout, stderr = b'partial output\n', b'first error\n' + b'x' * 2500 + b'\nlast error\n'
    class Process:
        returncode = 128
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def communicate(self, **kw): return stdout, stderr
        def poll(self): return 128
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: Process())
    with pytest.raises(RepositoryError) as failure:
        RepositoryReader(tmp_path)._git(["ls-remote", "https://github.com/owner/repo.git", "HEAD"])
    receipt = failure.value.receipt()
    assert receipt["stdout"] == stdout.decode() and receipt["stderr"] == stderr.decode()
    assert receipt["exit_code"] == 128


def test_fetched_passages_are_not_silently_cut_before_persistence():
    body = "source paragraph " * 500
    item = SimpleNamespace(passages=[{"text": body}], text_excerpt=body, error="e" * 700)
    payload = WikiChatService._trace_web_result(item)
    assert payload["passages"][0]["text"] == body
    assert payload["text_excerpt"] == body and payload["error"] == item.error


def test_failed_fetch_does_not_silently_visit_another_url():
    seen = []
    def fetch(url, **kw):
        seen.append(url)
        return SimpleNamespace(title="", url=url, site="official.example", passages=[],
                               status="error", error="HTTP 404", fetched_at="", text_excerpt="")
    service = bare_service(web_fetch=SimpleNamespace(available=True, fetch=fetch))
    obs = service._execute_agent_tool_call(AgentToolCall("web_fetch", {"url": "https://official.example/source"}),
        [], [SimpleNamespace(url="https://unrelated.example/other")], [], 6)
    assert seen == ["https://official.example/source"]
    assert obs.status == "error" and "HTTP 404" in obs.summary


def test_github_404_keeps_response_body_and_other_repository_can_open(tmp_path):
    body = b'{"message":"Not Found","documentation_url":"https://docs.github.com/rest"}'
    sha = "a" * 40
    class Response:
        headers = {}
        def __init__(self, status, raw):
            self.status_code, self.raw = status, raw
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, size): yield self.raw
    class HTTP:
        def get(self, url, **kw):
            return Response(404, body) if "missing/agent" in url else Response(200, json.dumps({"sha": sha}).encode())
    reader = RepositoryReader(tmp_path, http=HTTP())
    with pytest.raises(RepositoryError) as failure:
        reader.run("open", repository="missing/agent")
    receipt = failure.value.receipt()
    assert receipt["response_body"] == body.decode()
    assert receipt["status_code"] == 404 and not receipt["response_truncated"]
    assert reader.run("open", repository="observed/agent")["commit"] == sha


def test_legacy_retrieval_limit_does_not_block_calls_or_mark_partial(monkeypatch):
    monkeypatch.setenv("PAPERWIKI_CHAT_RETRIEVAL_LIMIT", "1")
    requests = []
    class Model:
        def tool_call(self, messages, **kw):
            requests.append(deepcopy(messages))
            if len(requests) > 2:
                return {"content": ""}
            return {"tool_calls": [{"id": f"c{len(requests)}", "function": {"name": "local_shell",
                "arguments": json.dumps({"command": f"read project{len(requests)}"})}}]}
    service = bare_service(llm=Model())
    monkeypatch.setattr(service, "_native_tool_messages", lambda **kw: [{"role": "user", "content": "research"}])
    service.local_shell = SimpleNamespace(run=lambda *a, **kw: {"stdout": "source", "stderr": "", "exit_code": 0})
    control = RunControl(None, "", "research")
    with control.bind():
        result = service._run_tool_loop("research", "", [], max_steps=6)
    assert result["trace"]["stop_reason"] == "model_finished"
    assert result["trace"]["task_outcome"]["status"] == "model_finished"
    reply = json.loads(next(m for m in requests[-1] if m.get("tool_call_id") == "c2")["content"])
    assert reply["status"] == "done"
    assert result["trace"]["tool_budget"]["max_calls"] is None
    assert result["trace"]["tool_budget"]["used_calls"] == 2


def test_using_last_allowed_call_is_not_itself_partial():
    assert WikiChatService._task_outcome([], "model_finished")["status"] == "model_finished"


def test_normal_loop_ignores_old_step_and_call_quotas_on_resume(monkeypatch):
    from system.wiki.research_state import ResearchState
    monkeypatch.setenv("PAPERWIKI_MAX_TOOL_STEPS", "1")
    monkeypatch.setenv("PAPERWIKI_RESEARCH_RETRIEVAL_LIMIT", "1")
    events, requests = [], []
    class Model:
        def tool_call(self, messages, **kw):
            requests.append(kw)
            assert events[-1]["phase"] == "thinking"
            if len(requests) > 2:
                return {"content": ""}
            return {"tool_calls": [{"id": f"c{len(requests)}", "function": {"name": "local_shell",
                "arguments": json.dumps({"command": f"read file{len(requests)}"})}}]}
    service = bare_service(llm=Model())
    monkeypatch.setattr(service, "_native_tool_messages", lambda **kw: [{"role": "user", "content": "research"}])
    service.local_shell = SimpleNamespace(run=lambda *a, **kw: {"stdout": "source", "stderr": "", "exit_code": 0})
    control = RunControl(None, "", "research")
    saved = ResearchState("research", "research").data
    saved["budget"].update(max_calls=128, used_calls=128)
    control.loop_state.update(research_mode=True, research_state=saved, planner_steps=200, thinking_effort="max")
    with control.bind():
        result = service._run_tool_loop("research", "", [], event_callback=events.append)
    assert len(requests) == 3 and all(r["max_tokens"] is None for r in requests)
    assert result["trace"]["stop_reason"] == "model_finished"
    assert result["trace"]["tool_budget"]["used_calls"] == 130
    assert result["trace"]["tool_budget"]["max_calls"] is None
    plan = AgentToolObservation("task_plan_write", "", "done", items=[{"task_id": "p", "status": "completed"}])
    assert WikiChatService._task_outcome([plan], "budget_exhausted")["status"] == "model_finished"


@pytest.mark.parametrize("saved_cap", [64, None])
def test_partial_resume_retains_evidence_and_usage_without_quota_and_saves_new_answer(tmp_path, saved_cap):
    sessions = SessionStore(str(tmp_path / "session.db"))
    sid = sessions.create_session()
    card = {"id": "card-1", "current_revision_id": "rev-1", "title": "First project", "page_type": "TopicPage"}
    wiki = SimpleNamespace(get_card=lambda cid: dict(card))
    service = WikiChatService(wiki, wiki_resolver=object(), session_store=sessions, runtime=AgentRunStore(sessions.db_path))
    control, _ = service._prepare_chat("Research four projects", sid, 6)
    source = {"kind": "source", "span_id": "repo_real", "repository": "owner/first", "content": "original source"}
    receipt = {"card_id": "card-1", "revision_id": "rev-1", "repository": "owner/first", "verified_readback": True,
               "content": "Complete article including long-term memory", "title": "First project"}
    try:
        with control.bind():
            for name, item in [("repository", source), ("wiki_write", receipt)]:
                call = AgentToolCall(name, {"operation": "read"} if name == "repository" else {"title": "First project"})
                service._journal_calls([call])
                service.chat_recovery.begin(control.run_id, call.recovery_id)
                service._persist_tool_result(call, AgentToolObservation(name, "", "done", items=[item]))
            outcome = {"status": "partial", "stop_reason": "budget_exhausted", "committed_wiki": [receipt]}
            control.loop_state.update(task_outcome=outcome, research_state={"reason": "budget_exhausted"})
            service.runtime.save_chat_cursor(control.run_id, control.message, 64, stop_reason="budget_exhausted",
                task_outcome=outcome, research_state={"question": control.message, "mode": "chat",
                    "budget": {"max_calls": saved_cap, "used_calls": 64, "max_no_gain": 2}})
            sessions.save_chat_answer(sid, control.run_id, control.message, "First partial answer",
                                     {"trace": {"task_outcome": outcome}}, "Research")
            service._complete_chat_runtime(control.run_id, answer="First partial answer", cards=[card], web_results=[], resources=[])
    finally:
        control.lease.stop()
    assert service.chat_recovery.list_for_session(sid)[0]["task_outcome"]["status"] == "partial"
    resumed, _ = service._prepare_chat("Continue remaining projects", sid, 6, resume_run_id=control.run_id)
    try:
        assert resumed.loop_state["planner_steps"] == 0
        assert resumed.loop_state["research_state"]["budget"]["used_calls"] == 64
        assert resumed.loop_state["research_state"]["budget"]["max_calls"] is None
        assert resumed.loop_state["cards"][0]["_full_text"] == receipt["content"]
        assert any(source in obs.items for obs in resumed.loop_state["observations"])
        with resumed.bind():
            ids = sessions.save_chat_answer(sid, resumed.run_id, resumed.message, "Second answer", {}, "Research")
            assert sessions.save_chat_answer(sid, resumed.run_id, resumed.message, "No duplicate", {}, "Research") == ids
        assert [m["content"] for m in sessions.get_messages(sid) if m["role"] == "assistant"] == ["First partial answer", "Second answer"]
    finally:
        resumed.lease.stop()

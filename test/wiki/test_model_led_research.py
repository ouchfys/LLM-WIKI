"""Regressions for the 2026-09-27 repeated source reads and thinking transport."""
from copy import deepcopy
import json

import pytest

from system.agent_runtime.control import RunControl
from system.core.deepseek_client import DeepSeekChat
from system.core.thinking import thinking_options
from system.wiki.wiki_chat import AgentToolObservation, WikiChatService


class Model:
    model = "deepseek-v4-flash"

    def __init__(self, actions):
        self.actions = iter(actions)
        self.requests = []

    def tool_call(self, messages, **kwargs):
        self.requests.append(deepcopy(messages))
        action = next(self.actions, None)
        if action is None:
            return {"content": "", "reasoning_content": "opaque continuation state"}
        name, args = action
        return {"content": "正在读取相关实现。", "reasoning_content": "opaque continuation state",
                "tool_calls": [{"id": f"call_{len(self.requests)}", "type": "function",
                                "function": {"name": name, "arguments": json.dumps(args)}}]}

    def invoke(self, *args, **kwargs):
        pytest.fail("The tool loop must not call a separate evidence assessor")


def run_script(monkeypatch, actions, *, mode=False, max_steps=12):
    model = Model(actions)
    service = WikiChatService(object(), wiki_resolver=object(), llm=model)
    monkeypatch.setattr(service, "_native_tool_messages", lambda **k: [
        {"role": "system", "content": "Research from observed code."},
        {"role": "user", "content": "Research all four projects and write Wiki cards."}])
    monkeypatch.setattr(service, "_completion_context", lambda obs: "")
    monkeypatch.setattr(service, "_assess_research_state", lambda *a: pytest.fail("Unexpected assessment"))

    def execute(call, *args):
        if call.name == "local_shell":
            return AgentToolObservation(call.name, "", "done", "read succeeded", [
                {"stdout": "Token-budget compaction starts a new context window.", "exit_code": 0}])
        return AgentToolObservation(call.name, "", "done", "read Pi source", [
            {"kind": "source", "repository": "earendil-works/pi", "path": "compaction.ts",
             "content": "export function compact() {}", "commit": "a" * 40, "span_id": "pi-span"}])

    monkeypatch.setattr(service, "_execute_agent_tool_call_impl", execute)
    control = RunControl(None, "", "Research all four projects")
    control.loop_state.update(research_mode=mode, thinking_effort="high")
    with control.bind():
        result = service._run_tool_loop(control.message, "", [], max_steps=max_steps)
    return service, control, model, result


@pytest.mark.parametrize("mode", [False, True])
def test_repeated_codex_reads_allow_pi_research_and_keep_call_provenance(monkeypatch, mode):
    commands = [
        'Get-Content "codex-rs/core/src/compact_token_budget.rs" | Select-Object -First 160',
        'Get-Content -Raw "codex-rs/core/src/compact_token_budget.rs"',
        'Get-Content "codex-rs/core/src/compact_token_budget.rs" | Select-Object -First 120',
    ]
    service, control, model, result = run_script(monkeypatch,
        [("local_shell", {"command": cmd}) for cmd in commands] +
        [("repository", {"operation": "read", "snapshot_id": "pi", "path": "compaction.ts"})], mode=mode)
    assert result["trace"]["stop_reason"] == "model_finished"
    assert "research_state" not in result["trace"]
    assert len(result["plan"].tools) == 4
    messages = model.requests[-1]
    for index, cmd in enumerate(commands, 1):
        assistant = next(m for m in messages if m.get("tool_calls", [{}])[0].get("id") == f"call_{index}")
        assert json.loads(assistant["tool_calls"][0]["function"]["arguments"])["command"] == cmd
        assert assistant["reasoning_content"] == "opaque continuation state"
        response = next(m for m in messages if m.get("tool_call_id") == f"call_{index}")
        payload = json.loads(response["content"])
        assert payload["arguments"]["command"] == cmd
        assert "starts a new context" in payload["items"][0]["stdout"]
    assert "opaque continuation state" not in json.dumps(result["trace"])
    answer_context = service._answer_observation_context(result["trace"]["tool_observations"])
    assert "compact_token_budget.rs" in answer_context
    assert "runtime_call_feedback" in [o.tool for o in control.loop_state["observations"]]


def test_skipped_duplicates_have_protocol_replies_and_do_not_stop_other_projects(monkeypatch):
    repeated = ("local_shell", {"command": "read codex"})
    _, _, model, result = run_script(monkeypatch, [repeated] * 4 + [
        ("repository", {"operation": "read", "snapshot_id": "pi", "path": "compaction.ts"})])
    assert result["trace"]["stop_reason"] == "model_finished"
    assert [c.name for c in result["plan"].tools] == ["local_shell", "repository"]
    responses = [json.loads(m["content"]) for m in model.requests[-1] if m["role"] == "tool"]
    assert sum(r["status"] == "not_executed" for r in responses) == 3


def test_total_budget_still_stops_an_uncooperative_model(monkeypatch):
    _, _, _, result = run_script(monkeypatch, [("local_shell", {"command": "same"})] * 10, max_steps=5)
    assert result["trace"]["stop_reason"] == "step_budget_exhausted"


@pytest.mark.parametrize("effort", ["none", "low", "high", "max"])
def test_selected_effort_reaches_tool_text_and_stream_payloads(monkeypatch, effort):
    payloads = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}

        def iter_lines(self, **kwargs):
            yield 'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}'
            yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
            yield 'data: [DONE]'

        def close(self):
            pass

    def post(*args, **kwargs):
        payloads.append(deepcopy(kwargs["json"]))
        return Response()

    monkeypatch.setattr("system.core.deepseek_client.requests.post", post)
    client = DeepSeekChat(api_key="test", max_retries=1)
    control = RunControl(None, "", "test")
    control.loop_state["thinking_effort"] = effort
    with control.bind():
        client.tool_call([], tools=[])
        assert client.invoke("answer", **thinking_options()) == "ok"
        assert "".join(client.stream_invoke("answer", **thinking_options())) == "ok"
    for payload in payloads:
        assert payload["thinking"] == {"type": "disabled" if effort == "none" else "enabled"}
        if effort != "none":
            assert payload["reasoning_effort"] == effort
            assert "temperature" not in payload


def test_http_contract_rejects_unknown_effort():
    from backend.api.wiki import WikiChatPayload
    from pydantic import ValidationError
    assert WikiChatPayload(thinking_effort="max").thinking_effort == "max"
    with pytest.raises(ValidationError):
        WikiChatPayload(thinking_effort="unlimited")


@pytest.mark.parametrize("stream", [False, True])
def test_http_forwards_selected_effort(stream):
    import asyncio
    from backend.api.wiki import WikiChatPayload, chat_with_wiki
    from system.wiki.wiki_chat import WikiChatResult
    captured = []

    class Service:
        def chat(self, message, **kwargs):
            captured.append(kwargs)
            return WikiChatResult(answer="ok")

        def chat_stream(self, message, **kwargs):
            captured.append(kwargs)
            yield {"type": "done"}

    response = chat_with_wiki(WikiChatPayload(message="test", stream=stream, thinking_effort="low"), Service())
    if stream:
        async def consume():
            return [chunk async for chunk in response.body_iterator]
        asyncio.run(consume())
    assert captured == [{"session_id": "", "thinking_effort": "low"}]


def test_github_exhausted_quota_is_actionable_without_exposing_credentials(tmp_path, monkeypatch):
    from system.agent_runtime.repositories import RepositoryReader, RepositoryError
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    class Response:
        status_code = 403
        headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Limit": "60", "X-RateLimit-Reset": "12345"}
        def iter_content(self, size):
            yield b'{"message":"API rate limit exceeded"}'
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    class HTTP:
        def get(self, *args, **kwargs):
            return Response()

    reader = RepositoryReader(tmp_path, http=HTTP())
    with pytest.raises(RepositoryError) as caught:
        reader.run(operation="discover", query="agent")
    assert caught.value.code == "rate_limited"
    assert "anonymous" in str(caught.value)
    assert "Continue other projects" in str(caught.value)
    assert caught.value.retry_after == "12345"


def test_private_tool_transcript_and_effort_survive_restart(tmp_path, monkeypatch):
    from contextlib import closing
    from system.agent_runtime import AgentRunStore
    from system.conversation.session_store import SessionStore
    from system.wiki.wiki_chat import AgentToolCall

    sessions = SessionStore(str(tmp_path / "session.db"))
    sid = sessions.create_session()
    service = WikiChatService(object(), wiki_resolver=object(), session_store=sessions,
                             runtime=AgentRunStore(sessions.db_path), llm=object())
    monkeypatch.setattr(service, "_native_tool_messages", lambda **kw: [{"role": "user", "content": "Read a file"}])
    monkeypatch.setattr(service, "_execute_agent_tool_call_impl", lambda call, *a:
        AgentToolObservation("local_shell", "", "done", "file read", [{"stdout": "source body", "exit_code": 0}]))
    control, _ = service._prepare_chat("Read a file", sid, 6, effort="max")
    kwargs = dict(message=control.message, effective_query="", history=[], observations=[], step_index=0, limit=6)
    try:
        with control.bind():
            service._provider_tool_messages(**kwargs)
            service._remember_provider_reply({"content": "", "reasoning_content": "opaque private state",
                "tool_calls": [{"id": "call_read", "type": "function", "function": {
                    "name": "local_shell", "arguments": '{"command":"read path.rs"}'}}]})
            service._execute_agent_tool_call(AgentToolCall("local_shell", {"command": "read path.rs"},
                                                         model_call_id="call_read"), [], [], [], 6)
    finally:
        control.lease.stop()
    with closing(service.runtime._connect()) as conn:
        conn.execute("UPDATE agent_runs SET lease_expires_at='2000-01-01',lease_owner='dead-worker' WHERE id=?", (control.run_id,))
        conn.commit()
    resumed, _ = service._prepare_chat("", sid, 6, resume_run_id=control.run_id)
    try:
        assert resumed.loop_state["thinking_effort"] == "max"
        with resumed.bind():
            messages = service._provider_tool_messages(**kwargs)
        assistant = next(m for m in messages if any(
            call.get("id") == "call_read" for call in m.get("tool_calls", [])))
        assert assistant["reasoning_content"] == "opaque private state"
        reply = next(m for m in messages if m.get("tool_call_id") == "call_read")
        assert json.loads(reply["content"])["arguments"]["command"] == "read path.rs"
        assert "source body" in reply["content"]
        assert "opaque private state" not in json.dumps(service.runtime.get_run(control.run_id))
        assert "opaque private state" not in json.dumps(service.runtime.list_events(control.run_id))
    finally:
        resumed.lease.stop()

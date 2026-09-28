import pytest
import json

from system.agent_runtime import AgentRunStore, TraceRecorder
from system.core.llm_call import invoke_structured
from system.core.deepseek_client import OutputTruncatedError, DeepSeekChat


def test_official_client_uses_deepseek_thinking_contract(monkeypatch):
    monkeypatch.setenv("SILICONFLOW_API_KEY", "must-not-be-used")
    monkeypatch.setattr("system.core.deepseek_client.DEEPSEEK_API_KEY", "")
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        DeepSeekChat()
    client = DeepSeekChat(api_key="unit-test")
    payload = {}
    client._set_thinking(payload, False)
    assert payload == {"thinking": {"type": "disabled"}}
    assert client.base_url.startswith("https://api.deepseek.com/")


@pytest.mark.parametrize("finish_reason", ["stop", "length"])
def test_structured_response_records_finish_reason_and_rejects_truncation(tmp_path, monkeypatch, finish_reason):
    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "choices": [{"finish_reason": finish_reason, "message": {"content": '{"ok":true}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 12, "total_tokens": 22},
            }

    def post(*args, **kwargs):
        calls.append(kwargs)
        return Response()

    monkeypatch.setattr("system.core.deepseek_client.requests.post", post)
    client = DeepSeekChat(api_key="unit-test", model="fake", max_retries=3)
    store = AgentRunStore(db_path=str(tmp_path / "trace.db"))
    run = store.create_run(run_type="wiki_chat", approval_mode="auto")
    with TraceRecorder(store, run["id"]).bind():
        if finish_reason == "length":
            with pytest.raises(OutputTruncatedError) as caught:
                invoke_structured(client, "JSON please", max_tokens=12)
            assert caught.value.output_tokens == 12
            assert caught.value.max_tokens == 12
        else:
            assert invoke_structured(client, "JSON please", max_tokens=12) == '{"ok":true}'
    # Retrying the identical truncated request belongs to the caller, not HTTP retries.
    assert len(calls) == 1
    events = store.list_events(run["id"])
    terminal = [e for e in events if e["event_type"] in {"model.completed", "model.failed"}][-1]
    assert terminal["output"]["finish_reason"] == finish_reason
    assert terminal["usage"]["completion_tokens"] == 12
    assert terminal["event_type"] == ("model.failed" if finish_reason == "length" else "model.completed")


def test_truncated_tool_arguments_are_not_executed(monkeypatch):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"finish_reason": "length", "message": {"tool_calls": [{"function": {"arguments": '{"x":'}}]}}]}

    monkeypatch.setattr("system.core.deepseek_client.requests.post", lambda *a, **k: Response())
    with pytest.raises(OutputTruncatedError):
        DeepSeekChat(api_key="test", model="fake").tool_call([], tools=[], max_tokens=100)


def test_structured_call_can_disable_nested_transport_retries(monkeypatch):
    import requests
    calls = []
    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise requests.exceptions.ReadTimeout("timeout")
    monkeypatch.setattr("system.core.deepseek_client.requests.post", fail)
    client = DeepSeekChat(api_key="test", model="fake", max_retries=3)
    with pytest.raises(requests.exceptions.ReadTimeout):
        invoke_structured(client, "compare", max_tokens=100, max_attempts=1)
    assert len(calls) == 1
    assert client.max_retries == 3


@pytest.mark.parametrize("kind", ["tool", "answer", "stream"])
def test_main_model_omits_output_limit_and_accepts_streaming_response(monkeypatch, kind):
    requests = []
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "private continuation"}}]},
        {"choices": [{"delta": {"content": "Ready"}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-1",
            "function": {"name": "task_plan_write", "arguments": '{"steps":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0,
            "function": {"arguments": '[{"step":"Read","status":"pending"}]}'}}]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"completion_tokens": 20001, "completion_tokens_details": {"reasoning_tokens": 20000}}},
    ]
    class Response:
        headers = {"Content-Type": "text/event-stream"}
        closed = False
        def raise_for_status(self): pass
        def iter_lines(self, decode_unicode=True):
            yield from ("data: " + json.dumps(chunk) for chunk in chunks)
            yield "data: [DONE]"
        def close(self): self.closed = True
    response = Response()
    def post(*a, **kw):
        requests.append(kw)
        return response
    monkeypatch.setattr("system.core.deepseek_client.requests.post", post)
    client = DeepSeekChat(api_key="test", model="fake")
    if kind == "tool":
        result = client.tool_call([], [])
        assert result["tool_calls"][0]["id"] == "call-1"
        assert json.loads(result["tool_calls"][0]["function"]["arguments"])["steps"][0]["step"] == "Read"
        assert result["reasoning_content"] == "private continuation"
    elif kind == "answer":
        assert client.invoke("question") == "Ready"
    else:
        assert "".join(client.stream_invoke("question")) == "Ready"
    assert "max_tokens" not in requests[0]["json"]
    assert requests[0]["stream"] is True
    assert response.closed


@pytest.mark.parametrize("kind", ["tool", "answer", "stream"])
def test_provider_truncation_without_app_limit_is_still_reported(monkeypatch, kind):
    class Response:
        headers = {"Content-Type": "text/event-stream"}
        closed = False
        def raise_for_status(self): pass
        def iter_lines(self, decode_unicode=True):
            yield 'data: {"choices":[{"delta":{"reasoning_content":"private"},"finish_reason":"length"}]}'
            yield 'data: {"choices":[],"usage":{"completion_tokens":128000}}'
            yield 'data: [DONE]'
        def close(self): self.closed = True
    response = Response()
    monkeypatch.setattr("system.core.deepseek_client.requests.post", lambda *a, **kw: response)
    client = DeepSeekChat(api_key="test", model="fake")
    with pytest.raises(OutputTruncatedError) as caught:
        if kind == "tool":
            client.tool_call([], [])
        elif kind == "answer":
            client.invoke("question")
        else:
            list(client.stream_invoke("question"))
    assert caught.value.max_tokens is None
    assert caught.value.output_tokens == 128000
    assert response.closed

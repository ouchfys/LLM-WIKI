import json

import pytest
import requests

from scripts.run_research_smoke import StreamProtocolError, iter_sse_events, run_research_smoke


class Response:
    def __init__(self, *, body=None, events=(), error=None, content_type="text/event-stream"):
        self.body = body
        self.events = events
        self.error = error
        self.headers = {"Content-Type": content_type}
        self.closed = False

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        return self.body

    def iter_lines(self, **kwargs):
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            yield "data: " + json.dumps(event, ensure_ascii=False)
            yield ""

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class Client:
    def __init__(self, response):
        self.responses = [Response(body={"id": "session-1"}), response]
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses[len(self.calls) - 1]


def execute(tmp_path, events):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("研究这些论文。", encoding="utf-8-sig")
    output = tmp_path / "run"
    client = Client(Response(events=events))
    messages = []
    code = run_research_smoke(base_url="http://localhost:8000", prompt_file=prompt, output_dir=output, http=client, report=messages.append)
    return code, json.loads((output / "summary.json").read_text(encoding="utf-8")), output, client, messages


def test_completed_turn_preserves_trace_and_resets_answer_without_claiming_goal_success(tmp_path):
    code, summary, output, client, messages = execute(tmp_path, [
        {"type": "run_started", "run_id": "run-1"},
        {"type": "token", "text": "旧回答"},
        {"type": "answer_reset"},
        {"type": "progress", "text": "公开进度", "reasoning_content": "private provider reasoning"},
        {"type": "token", "text": "最终回答"},
        {"type": "agent_trace", "trace": {"stop_reason": "step_budget_exhausted", "runtime": {"tool_calls": 3}}},
        {"type": "done"},
    ])
    assert code == 0
    assert summary["turn_complete"] is True
    assert summary["stop_reason"] == "step_budget_exhausted"
    assert summary["research_goal_verified"] is None
    assert summary["run_id"] == "run-1"
    assert summary["session_url"] == "http://localhost:8000/?session=session-1"
    assert (output / "answer.md").read_text(encoding="utf-8") == "最终回答"
    assert "private provider reasoning" not in (output / "events.jsonl").read_text(encoding="utf-8")
    assert "最终回答" not in "\n".join(messages)
    assert len(client.calls) == 2
    assert client.calls[1][1]["json"]["message"] == "研究这些论文。"
    assert all(response.closed for response in client.responses)


@pytest.mark.parametrize("events,error_type", [
    ([{"type": "token", "text": "partial"}], "StreamProtocolError"),
    ([requests.ConnectionError("secret connection details")], "ConnectionError"),
])
def test_disconnection_saves_partial_run_and_never_resubmits(tmp_path, events, error_type):
    code, summary, output, client, messages = execute(tmp_path, events)
    assert code != 0
    assert not summary["turn_complete"]
    assert not summary["stream_done"]
    assert summary["errors"][0]["type"] == error_type
    assert len(client.calls) == 2
    assert "secret connection details" not in "\n".join(messages)


def test_error_followed_by_done_is_not_success(tmp_path):
    code, summary, *_ = execute(tmp_path, [{"type": "error", "message": "provider failed"}, {"type": "done"}])
    assert code == 2
    assert summary["stream_done"] is True
    assert summary["turn_complete"] is False
    assert summary["answer_status"] == "partial"


def test_cancelled_done_is_not_success(tmp_path):
    code, summary, *_ = execute(tmp_path, [{"type": "done", "cancelled": True}])
    assert code == 3
    assert summary["cancelled"] is True
    assert summary["turn_complete"] is False


def test_existing_run_is_not_overwritten_or_resubmitted(tmp_path):
    _, _, output, client, _ = execute(tmp_path, [{"type": "done"}])
    with pytest.raises(ValueError, match="never overwritten"):
        run_research_smoke(base_url="http://localhost:8000/api", prompt_file=tmp_path / "prompt.txt", output_dir=output, http=client)
    assert len(client.calls) == 2


def test_parser_handles_comment_and_multiline_events_and_rejects_partial_json():
    events = list(iter_sse_events([": heartbeat", "", 'data: {"type":"token",', 'data: "text":"中文"}', ""]))
    assert events == [{"type": "token", "text": "中文"}]
    with pytest.raises(StreamProtocolError, match="inside"):
        list(iter_sse_events(['data: {"type":"token"}']))
    with pytest.raises(StreamProtocolError, match="Invalid JSON"):
        list(iter_sse_events(["data: nope", ""]))

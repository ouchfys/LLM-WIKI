from __future__ import annotations

from system.agent_runtime import AgentRunStore, TraceRecorder
from system.agent_runtime.tracing import record_current_retry, set_current_span_usage


def test_runtime_aggregates_exact_usage_and_retry_events(tmp_path) -> None:
    runtime = AgentRunStore(db_path=str(tmp_path / "trace-metrics.db"))
    run = runtime.create_run(run_type="wiki_chat", approval_mode="auto")
    run_id = str(run["id"])
    runtime.transition(run_id, "CHAT_RUNNING")
    recorder = TraceRecorder(runtime, run_id)

    with recorder.bind():
        with recorder.span(
            "llm.invoke", kind="model", model="provider-model", tool_name="invoke",
        ) as span:
            record_current_retry(
                name="llm.invoke",
                model="provider-model",
                attempt=1,
                max_attempts=3,
                error="rate limited",
                delay_seconds=0.01,
            )
            set_current_span_usage({
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "total_tokens": 150,
            })
            span["output"] = {"attempts": 2, "response_chars": 20}

    runtime.transition(run_id, "COMPLETED")
    summary = runtime.summarize_trace(run_id)

    assert summary["model_calls"] == 1
    assert summary["retry_count"] == 1
    assert summary["token_usage"] == {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
        "estimated_calls": 0,
        "contains_estimates": False,
    }
    events = runtime.list_events(run_id)
    retry = next(item for item in events if item["event_type"] == "model.retry")
    completed = next(item for item in events if item["event_type"] == "model.completed")
    assert retry["parent_span_id"] == completed["span_id"]

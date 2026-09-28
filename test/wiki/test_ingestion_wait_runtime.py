"""Batch execution and asynchronous waiting use local fakes, never live services."""

import json
import threading
import time

import pytest

from system.agent_runtime.control import RunCancelled, RunControl
from system.wiki.wiki_chat import AgentToolCall, AgentToolObservation, WikiChatService


def service():
    return WikiChatService(object(), wiki_resolver=object(), llm=object())


def status(job_id, value="running", *, result_id=0):
    item = {"id": job_id, "status": value}
    if value == "done":
        item["paper_card_id"] = f"paper-{job_id}"
    return AgentToolObservation("arxiv_ingestion_status", job_id, "done", value, [item], result_id=result_id)


@pytest.mark.parametrize("native", [False, True])
def test_all_nine_valid_calls_survive_normalization_and_policy(native):
    chat = service()
    calls = [{"name": "arxiv", "arguments": {"action": "status", "job_id": f"job-{i}"}}
             for i in range(9)]
    calls.insert(4, {"name": "unadvertised", "arguments": {}})
    if native:
        payload = {"tool_calls": [{"function": {**call, "arguments": json.dumps(call["arguments"])}}
                                  for call in calls]}
        normalized = chat._normalize_native_tool_calls(payload, "papers", 6)
    else:
        normalized = chat._normalize_agent_tool_calls({"tool_calls": calls}, "papers", 6)
    imports = [AgentToolObservation("arxiv_import_paper", "", "done", items=[{"job_id": f"job-{i}"}])
               for i in range(9)]
    accepted = chat._apply_query_tool_policy(normalized, message="read papers", effective_query="papers",
                                            observations=imports, limit=6)
    assert [call.arguments["job_id"] for call in accepted] == [f"job-{i}" for i in range(9)]


def test_explicit_job_checks_keep_all_jobs_and_other_valid_calls():
    chat = service()
    jobs = [f"00000000-0000-4000-8000-{i:012d}" for i in range(9)]
    calls = chat._apply_query_tool_policy([AgentToolCall("task_plan_read")],
        message="arxiv_ingestion_status " + " ".join(jobs), effective_query="papers", observations=[], limit=6)
    assert [call.arguments["job_id"] for call in calls[:-1]] == jobs
    assert calls[-1].name == "task_plan_read"


def test_nine_independent_reads_all_execute_with_at_most_three_workers(monkeypatch):
    chat = service()
    lock = threading.Lock()
    active = maximum = 0

    def execute(call, *args):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(active, maximum)
        time.sleep(0.01)
        with lock:
            active -= 1
        return status(call.arguments["job_id"], "done")

    monkeypatch.setattr(chat, "_execute_agent_tool_call_impl", execute)
    calls = [AgentToolCall("arxiv", {"action": "status", "job_id": f"job-{i}"}) for i in range(9)]
    results = chat._execute_tool_batch(calls, [], [], [], 6)
    assert [item.query for item in results] == [f"job-{i}" for i in range(9)]
    assert 1 < maximum <= 3


def test_unchanged_polls_use_backoff_and_no_extra_model_steps(monkeypatch):
    chat = service()
    jobs = [f"job-{i}" for i in range(9)]
    model_views, polls, delays = [], [], []

    def plan(**kwargs):
        model_views.append(list(kwargs["observations"]))
        return [AgentToolCall("arxiv", {"action": "status", "job_id": job}) for job in jobs] if len(model_views) == 1 else []

    def execute(calls, *args):
        polls.append([call.arguments["job_id"] for call in calls])
        return [status(job, "done" if len(polls) == 4 else "running") for job in polls[-1]]

    monkeypatch.setattr(chat, "_next_agent_tool_calls", plan)
    monkeypatch.setattr(chat, "_execute_tool_batch", execute)
    monkeypatch.setattr(chat, "_wait_for_ingestion_interval", delays.append)
    result = chat._run_tool_loop("read all papers", "", [], max_steps=2)

    assert len(model_views) == 2
    assert polls == [jobs] * 4
    assert delays == [2.0, 4.0, 8.0]
    assert len(model_views[-1]) == 9
    assert all(item.items[0]["status"] == "done" for item in model_views[-1])
    assert len(result["trace"]["tool_observations"]) == 36  # Full audit retained.
    assert result["trace"]["stop_reason"] == "model_finished"


def test_repeated_shell_sleep_is_replaced_by_cooperative_job_wait(monkeypatch):
    chat = service()
    control = RunControl(None, "", "read papers")
    control.loop_state["observations"] = [status("a"), status("b")]
    wait = AgentToolCall("local_shell", {"command": "Start-Sleep -Seconds 60; Write-Output waited"})
    planned, executed = [[wait], [wait], []], []

    def execute(calls, *args):
        executed.extend(call.operation_name for call in calls)
        return [status(call.arguments["job_id"], "done" if call.arguments["job_id"] == "a" or len(executed) > 2 else "running")
                for call in calls]

    monkeypatch.setattr(chat, "_next_agent_tool_calls", lambda **kwargs: planned.pop(0))
    monkeypatch.setattr(chat, "_execute_tool_batch", execute)
    monkeypatch.setattr(chat, "_wait_for_ingestion_interval", lambda seconds: None)
    with control.bind():
        result = chat._run_tool_loop("read papers", "", [], max_steps=3)
    assert executed == ["arxiv_ingestion_status"] * 3
    assert result["trace"]["stop_reason"] == "model_finished"
    assert not chat._is_ingestion_wait_call(AgentToolCall("local_shell", {"command": "sleep 1; python work.py"}))


def test_wait_times_out_as_partial_without_spending_model_budget(monkeypatch):
    chat = service()
    clock = [0.0]
    model_steps = []
    monkeypatch.setenv("PAPERWIKI_INGESTION_WAIT_TIMEOUT_SECONDS", "5")
    monkeypatch.setattr("system.wiki.wiki_chat.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(chat, "_wait_for_ingestion_interval", lambda delay: clock.__setitem__(0, clock[0] + delay))
    monkeypatch.setattr(chat, "_next_agent_tool_calls", lambda **kwargs:
                        model_steps.append(kwargs["step_index"]) or [AgentToolCall("arxiv", {"action": "status", "job_id": "a"})])
    monkeypatch.setattr(chat, "_execute_tool_batch", lambda calls, *args: [status("a")])
    result = chat._run_tool_loop("read paper", "", [], max_steps=2)
    assert model_steps == [0]
    assert result["trace"]["stop_reason"] == "ingestion_wait_timeout"
    assert "尚未完成" in result["trace"]["tool_observations"][-1]["summary"]


def test_worker_failure_wakes_the_model_and_status_read_errors_are_bounded(monkeypatch):
    chat = service()
    observations = [status("a")]
    monkeypatch.setattr(chat, "_wait_for_ingestion_interval", lambda seconds: None)

    def fail_job(calls):
        observation = status("a", "failed")
        observations.append(observation)
        return [observation]

    assert chat._wait_for_ingestion(observations, fail_job, lambda event: None) == ""
    observations[:] = [status("a")]
    attempts = []

    def failed_read(calls):
        attempts.append(calls)
        observation = AgentToolObservation("arxiv_ingestion_status", "a", "error", "service unavailable")
        observations.append(observation)
        return [observation]

    assert chat._wait_for_ingestion(observations, failed_read, lambda event: None) == "ingestion_status_unavailable"
    assert len(attempts) == 3


def test_runtime_wait_can_be_cancelled_promptly():
    control = RunControl(None, "", "wait")
    timer = threading.Timer(0.03, control.abandoned.set)
    timer.start()
    started = time.monotonic()
    try:
        with control.bind(), pytest.raises(RunCancelled):
            WikiChatService._wait_for_ingestion_interval(60)
    finally:
        timer.cancel()
    assert time.monotonic() - started < 1


def test_only_latest_status_is_rendered_in_both_model_contexts():
    chat = service()
    observations = [status("a", "queued", result_id=101), status("b", "running", result_id=102), status("a", "done", result_id=103)]
    planning = chat._observation_context(observations)
    answer = chat._answer_observation_context([chat._observation_payload(item) for item in observations])
    for text in (planning, answer):
        assert "result_id=101" not in text
        assert "result_id=102" in text and "result_id=103" in text
    assert len(observations) == 3

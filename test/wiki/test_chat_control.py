from system.conversation.context_budget import ContextBudget, ContextPolicy
import asyncio
import time
from threading import Barrier, Event, Thread
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.agent_runs import router
from backend.deps import get_wiki_store
from system.agent_runtime import AgentRunStore
from system.memory.session_store import SessionStore
from system.wiki.wiki_chat import AgentToolCall, AgentToolObservation, WikiChatService, WikiToolPlan


class BlockingLLM:
    model = "test-control"

    def __init__(self, *, block_plan=False, block_stream=False):
        self.block_plan = block_plan
        self.block_stream = block_stream
        self.entered = Event()
        self.release = Event()
        self.finished = Event()
        self.prompts = []
        self.stream_closed = 0

    def invoke(self, prompt, **kwargs):
        self.prompts.append(prompt)
        if self.block_plan and len(self.prompts) == 1:
            self.entered.set()
            assert self.release.wait(5)
            self.finished.set()
        return "plan"

    def stream_invoke(self, prompt, **kwargs):
        self.prompts.append(prompt)
        try:
            if self.block_stream and self.stream_closed == 0:
                yield "旧的半截回答"
                self.entered.set()
                assert self.release.wait(5)
                yield "不应完成"
            else:
                yield f"最终回答：{prompt}"
        finally:
            self.stream_closed += 1


class ControlChat(WikiChatService):
    def __init__(self, runtime, llm):
        super().__init__(wiki_store=object(), wiki_resolver=object(), runtime=runtime, llm=llm, context_budget=ContextBudget(ContextPolicy()))
        self.saved = []
        self.profile_updates = []

    def _load_history(self, session_id):
        return []

    def _run_tool_loop(self, message, *args, **kwargs):
        self._invoke_llm(message, temperature=0, max_tokens=10)
        return {"plan": WikiToolPlan(), "cards": [], "web_results": [], "resources": [], "trace": {}}

    def _build_prompt(self, message, *args, **kwargs):
        return message

    def _update_profile_from_message(self, message, *args):
        self.profile_updates.append(message)
        return []

    def _save_turn(self, session_id, message, answer, *args):
        self.saved.append((message, answer))


def start_stream(service):
    stream = service.chat_stream("比较性能", session_id="session-control")
    run_id = next(stream)["run_id"]
    chunks = []
    consumer = Thread(target=lambda: chunks.extend(stream))
    consumer.start()
    return run_id, chunks, consumer


def test_cancel_blocked_provider_returns_promptly_and_discards_late_output(tmp_path):
    runtime = AgentRunStore(str(tmp_path / "cancel.db"))
    llm = BlockingLLM(block_plan=True)
    service = ControlChat(runtime, llm)
    run_id, chunks, consumer = start_stream(service)
    try:
        assert llm.entered.wait(2)
        runtime.enqueue_input(run_id, kind="command", content="/wiki 保存结论", input_id="queued-write")
        runtime.request_cancel(run_id)
        consumer.join(2)
        assert not consumer.is_alive(), "SSE must not wait for a blocked remote API"
        assert runtime.get_run(run_id)["current_state"] == "CANCELLED"
        assert any(item["type"] == "cancelled" for item in chunks)
        assert runtime.list_inputs(run_id)[0]["status"] == "blocked"
        assert service.saved == service.profile_updates == []
    finally:
        llm.release.set()
        consumer.join(3)
    assert llm.finished.wait(2)
    assert service.saved == []


def test_interrupt_replans_after_current_model_call(tmp_path):
    runtime = AgentRunStore(str(tmp_path / "interrupt.db"))
    llm = BlockingLLM(block_plan=True)
    service = ControlChat(runtime, llm)
    run_id, chunks, consumer = start_stream(service)
    try:
        assert llm.entered.wait(2)
        runtime.enqueue_input(run_id, kind="interrupt", content="不要性能，只比较设计思想", input_id="steer")
    finally:
        llm.release.set()
        consumer.join(4)
    assert not consumer.is_alive()
    assert runtime.get_run(run_id)["current_state"] == "COMPLETED"
    assert any(item["type"] == "answer_reset" for item in chunks)
    assert "只比较设计思想" in service.saved[0][1]
    assert len(service.saved) == 1
    assert runtime.list_inputs(run_id)[0]["status"] == "consumed"
    assert any(e["event_type"] == "model.interrupted" for e in runtime.list_events(run_id))
    metrics = runtime.summarize_trace(run_id)
    assert metrics["model_calls"] == 3
    assert metrics["model_interruptions"] == 1
    assert metrics["model_failures"] == 0


def test_interrupt_mid_answer_closes_old_stream_and_never_saves_partial(tmp_path):
    runtime = AgentRunStore(str(tmp_path / "stream.db"))
    llm = BlockingLLM(block_stream=True)
    service = ControlChat(runtime, llm)
    run_id, chunks, consumer = start_stream(service)
    try:
        assert llm.entered.wait(2)
        runtime.enqueue_input(run_id, kind="interrupt", content="改成三句话", input_id="mid-answer")
    finally:
        llm.release.set()
        consumer.join(4)
    assert not consumer.is_alive()
    assert llm.stream_closed == 2
    assert len(service.saved) == 1
    assert "旧的半截回答" not in service.saved[0][1]
    assert "改成三句话" in service.saved[0][1]
    assert any(item["type"] == "answer_reset" for item in chunks)


def test_interrupt_keeps_completed_tool_observations(tmp_path):
    runtime = AgentRunStore(str(tmp_path / "tool.db"))

    class ToolChat(ControlChat):
        _run_tool_loop = WikiChatService._run_tool_loop

        def __init__(self):
            super().__init__(runtime, BlockingLLM())
            self.entered = Event()
            self.release = Event()
            self.tool_count = 0
            self.seen_contexts = []

        def _next_agent_tool_calls(self, *, message, observations, **kwargs):
            self.seen_contexts.append((message, len(observations)))
            return [] if observations else [AgentToolCall("wiki_open", {"query": message})]

        def _execute_agent_tool_call_impl(self, call, cards, *args):
            self.tool_count += 1
            cards.append({"id": "page", "title": "论文", "page_type": "PaperPage", "summary": "知识", "markdown_path": ""})
            self.entered.set()
            assert self.release.wait(5)
            return AgentToolObservation("wiki_open", call.arguments["query"], "done", "已读取", [{"card_id": "page"}])

    service = ToolChat()
    run_id, chunks, consumer = start_stream(service)
    try:
        assert service.entered.wait(2)
        runtime.enqueue_input(run_id, kind="interrupt", content="不要重新检索", input_id="during-tool")
    finally:
        service.release.set()
        consumer.join(4)
    assert not consumer.is_alive()
    assert service.tool_count == 1
    assert any("不要重新检索" in message and count == 1 for message, count in service.seen_contexts)
    assert runtime.get_run(run_id)["current_state"] == "COMPLETED"


def test_independent_read_tools_run_concurrently_but_return_in_call_order():
    class ParallelChat(WikiChatService):
        def __init__(self):
            super().__init__(wiki_store=object(), wiki_resolver=object())
            self.barrier = Barrier(2)
            self.finished = []

        def _execute_agent_tool_call_impl(self, call, cards, web_results, resources, limit):
            self.barrier.wait(timeout=2)
            if call.arguments["query"] == "first":
                time.sleep(0.08)
            self.finished.append(call.arguments["query"])
            return AgentToolObservation(call.name, call.arguments["query"], "done", call.arguments["query"])

    service = ParallelChat()
    calls = [
        AgentToolCall("wiki_search", {"query": "first"}),
        AgentToolCall("web_search", {"query": "second"}),
    ]
    observations = service._execute_tool_batch(calls, [], [], [], 4)

    assert service.finished == ["second", "first"]
    assert [item.query for item in observations] == ["first", "second"]


def test_tool_batches_keep_dependencies_and_writes_serial():
    calls = [
        AgentToolCall("wiki_search", {"query": "paper"}),
        AgentToolCall("wiki_open", {"query": "paper"}),
        AgentToolCall("web_search", {"query": "paper"}),
        AgentToolCall("project_memory_update", {"content": "# Project Memory"}),
        AgentToolCall("search_project_history", {"query": "paper"}),
    ]

    batches = WikiChatService._tool_execution_batches(calls)

    assert [[call.name for call in batch] for batch in batches] == [
        ["wiki_search"],
        ["wiki_open", "web_search"],
        ["project_memory_update"],
        ["search_project_history"],
    ]


def test_queue_is_idempotent_and_finalization_fences_late_inputs(tmp_path):
    runtime = AgentRunStore(str(tmp_path / "queue.db"))
    run_id = runtime.create_run(run_type="wiki_chat")["id"]
    runtime.transition(run_id, "CHAT_RUNNING")
    args = dict(kind="command", content="/compact", input_id="command-1")
    runtime.enqueue_input(run_id, **args)
    runtime.enqueue_input(run_id, **args)
    runtime.enqueue_input(run_id, kind="followup", content="再举一个例子", input_id="next")
    runtime.enqueue_input(run_id, kind="interrupt", content="先解释概念", input_id="now")
    assert not runtime.close_chat_input(run_id)
    assert [item["id"] for item in runtime.take_interrupts(run_id)] == ["now"]
    assert runtime.close_chat_input(run_id)
    with pytest.raises(ValueError):
        runtime.request_cancel(run_id)
    with pytest.raises(ValueError):
        runtime.enqueue_input(run_id, kind="interrupt", content="太晚了", input_id="late")
    runtime.transition(run_id, "COMPLETED")
    assert [item["status"] for item in runtime.list_inputs(run_id)] == ["ready", "ready", "consumed"]
    runtime.update_input(run_id, "command-1", "running")
    with pytest.raises(ValueError):
        runtime.update_input(run_id, "command-1", "running")
    runtime.update_input(run_id, "command-1", "completed")


def test_project_purpose_command_can_be_queued(tmp_path):
    runtime = AgentRunStore(str(tmp_path / "purpose-queue.db"))
    run_id = runtime.create_run(run_type="wiki_chat")["id"]
    runtime.transition(run_id, "CHAT_RUNNING")

    viewed = runtime.enqueue_input(run_id, kind="command", content="/purpose", input_id="purpose-view")
    updated = runtime.enqueue_input(
        run_id,
        kind="command",
        content="/purpose 聚焦低成本推理",
        input_id="purpose-update",
    )

    assert viewed["status"] == "pending"
    assert updated["status"] == "pending"


def test_control_api_validation_and_cancel_flag(tmp_path):
    db = str(tmp_path / "api.db")
    runtime = AgentRunStore(db)
    run_id = runtime.create_run(run_type="wiki_chat")["id"]
    runtime.transition(run_id, "CHAT_RUNNING")
    app = FastAPI()
    app.include_router(router, prefix="/agent-runs")
    app.dependency_overrides[get_wiki_store] = lambda: SimpleNamespace(db_path=db)
    with TestClient(app) as client:
        assert client.post(f"/agent-runs/{run_id}/inputs", json={"kind": "command", "content": "/stop"}).status_code == 409
        assert client.post(f"/agent-runs/{run_id}/inputs", json={"kind": "invalid", "content": "test"}).status_code == 422
        assert client.post(f"/agent-runs/{run_id}/cancel").json()["cancel_requested"] == 1
        assert client.post("/agent-runs/not-found/cancel").status_code == 404


def test_cancelled_transcript_is_not_reused_as_model_context(tmp_path):
    sessions = SessionStore(str(tmp_path / "history.db"))
    session = sessions.create_session()
    sessions.save_message(session, "user", "已取消的问题", {"cancelled": True})
    sessions.save_message(session, "assistant", "本轮停止", {"cancelled": True})
    sessions.save_message(session, "user", "有效问题")
    sessions.save_message(session, "assistant", "有效回答")
    assert sessions.get_history(session) == [("有效问题", "有效回答")]
    assert len(sessions.get_display_history(session)) == 2


def test_disconnecting_before_worker_starts_closes_run_and_blocks_queue(tmp_path):
    from backend.api.wiki import WikiChatPayload, chat_with_wiki

    runtime = AgentRunStore(str(tmp_path / "disconnect.db"))
    service = ControlChat(runtime, BlockingLLM())
    response = chat_with_wiki(
        WikiChatPayload(message="测试断开", session_id="disconnect", stream=True),
        chat_service=service,
    )

    async def disconnect():
        first = await anext(response.body_iterator)
        assert "run_started" in first
        run_id = runtime.list_runs()[0]["id"]
        runtime.enqueue_input(run_id, kind="command", content="/compact", input_id="pending")
        await response.body_iterator.aclose()
        assert runtime.get_run(run_id)["current_state"] == "CANCELLED"
        assert runtime.list_inputs(run_id)[0]["status"] == "blocked"

    asyncio.run(disconnect())
    assert service.saved == []


def test_queue_capacity_and_blocking_remaining_ready_commands(tmp_path):
    runtime = AgentRunStore(str(tmp_path / "capacity.db"))
    run_id = runtime.create_run(run_type="wiki_chat")["id"]
    runtime.transition(run_id, "CHAT_RUNNING")
    for index in range(20):
        runtime.enqueue_input(run_id, kind="command", content="/compact", input_id=f"q{index}")
    with pytest.raises(ValueError, match="full"):
        runtime.enqueue_input(run_id, kind="command", content="/compact", input_id="too-many")
    runtime.close_chat_input(run_id)
    runtime.transition(run_id, "COMPLETED")
    runtime.update_input(run_id, "q0", "blocked")
    with pytest.raises(ValueError):
        runtime.update_input(run_id, "q0", "running")
    runtime.update_input(run_id, "q0", "dismissed")

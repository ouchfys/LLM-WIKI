import copy
import json
from types import SimpleNamespace

import pytest

from system.agent_runtime import AgentRunStore
from system.agent_runtime.control import RunControl, RunInterrupted
from system.conversation.session_store import SessionStore
from system.wiki.wiki_chat import AgentToolCall, AgentToolObservation, WikiChatService


class Model:
    def __init__(self, replies, fallback="fallback answer"):
        self.replies = iter(replies)
        self.requests = []
        self.answer_calls = 0
        self.fallback = fallback

    def tool_call(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        return next(self.replies)

    def invoke(self, *args, **kwargs):
        self.answer_calls += 1
        return self.fallback

    def stream_invoke(self, *args, **kwargs):
        self.answer_calls += 1
        yield self.fallback


def service(model, **kwargs):
    wiki = SimpleNamespace(list_cards=lambda **kw: [], get_card=lambda cid: {}, list_linked_pages=lambda *a, **kw: [])
    return WikiChatService(wiki, wiki_resolver=object(), llm=model, **kwargs)


@pytest.mark.parametrize("streaming", [True, False])
def test_native_public_answer_is_delivered_without_another_model_call(streaming):
    model = Model([{"content": "卡片记录了会话保存位置。", "reasoning_content": "PRIVATE", "tool_calls": []}])
    chat = service(model)
    if streaming:
        events = list(chat.chat_stream("解释会话存储"))
        assert "".join(e["text"] for e in events if e["type"] == "token") == "卡片记录了会话保存位置。"
        assert "PRIVATE" not in json.dumps(events)
    else:
        assert chat.chat("解释会话存储").answer == "卡片记录了会话保存位置。"
    assert len(model.requests) == 1
    assert model.answer_calls == 0


def test_completion_check_discards_earlier_answer(monkeypatch):
    model = Model([{"content": "过早的结论"}, {"content": "核对后的结论"}])
    chat = service(model)
    monkeypatch.setattr(chat, "_completion_context", lambda observations: "Recheck the actual outcomes.")
    events = list(chat.chat_stream("核对任务"))
    assert "".join(e["text"] for e in events if e["type"] == "token") == "核对后的结论"
    assert len(model.requests) == 2
    assert model.answer_calls == 0
    assert "runtime_completion_check" in json.dumps(model.requests[-1], ensure_ascii=False)


def test_pending_ingestion_discards_earlier_answer(monkeypatch):
    model = Model([{"content": "未完成就结束"}, {"content": "后台任务已结束"}])
    chat = service(model)
    pending = {"job": {"status": "running"}}
    monkeypatch.setattr(chat, "_pending_ingestion_jobs", lambda observations: dict(pending))
    monkeypatch.setattr(chat, "_wait_for_ingestion", lambda *args: pending.clear())
    assert chat.chat("等待任务完成").answer == "后台任务已结束"
    assert len(model.requests) == 2
    assert model.answer_calls == 0


def test_content_with_tools_is_progress_and_latest_citation_map_is_used(monkeypatch):
    model = Model([
        {"content": "正在读取卡片。", "tool_calls": [{"id": "read1", "function": {
            "name": "wiki_open", "arguments": '{"card_ids":["card-1"]}'}}]},
        {"content": "对话记录保存在日志中。[1]"},
    ])
    chat = service(model)

    def read(call, cards, *args, **kwargs):
        cards.append({"id": "card-1", "title": "会话记忆", "_full_text": "对话记录保存在日志中。"})
        return AgentToolObservation("wiki_open", "", "done", "opened", [{"card_id": "card-1", "content": "对话记录保存在日志中。"}])

    monkeypatch.setattr(chat, "_execute_agent_tool_call_impl", read)
    events = list(chat.chat_stream("解释会话记忆"))
    assert any(e.get("type") == "progress" and e.get("text") == "正在读取卡片。" for e in events)
    assert "".join(e["text"] for e in events if e["type"] == "token") == "对话记录保存在日志中。[1]"
    assert '[1] {"card_id": "card-1"' in model.requests[-1]["messages"][-1]["content"]
    assert model.answer_calls == 0


def test_unknown_citation_falls_back_instead_of_delivering_wrong_number():
    model = Model([{"content": "不存在的来源。[9]"}], fallback="当前没有相关卡片。")
    assert service(model).chat("解释卡片").answer == "当前没有相关卡片。"
    assert model.answer_calls == 1


def test_json_finish_without_public_answer_keeps_normal_answer_generation():
    class JsonModel:
        def __init__(self):
            self.calls = 0

        def invoke(self, *args, **kwargs):
            self.calls += 1
            return '{"finish":true,"tool_calls":[]}' if self.calls == 1 else "正常终答"

    model = JsonModel()
    assert service(model).chat("你好").answer == "正常终答"
    assert model.calls == 2


def test_user_interruption_invalidates_already_emitted_candidate():
    model = Model([{"content": "原要求的回答"}, {"content": "根据补充要求的回答"}])
    chat = service(model)
    control = RunControl(SimpleNamespace(append_event=lambda *a, **kw: None), "", "原要求")
    events = []

    def emit(event):
        events.append(event)
        if event.get("type") == "token" and event.get("text") == "原要求的回答":
            raise RunInterrupted([{"id": "new-input", "content": "请改为解释另一项"}])

    result = chat._produce_turn(control, None, "", 6, emit, streaming=True)
    assert result.answer == "根据补充要求的回答"
    assert any(e["type"] == "answer_reset" for e in events)
    candidate = control.loop_state["native_transcript"]["final_answer_candidate"]
    assert candidate["request"] == control.message
    assert "请改为解释另一项" in candidate["request"]
    assert model.answer_calls == 0


def test_persisted_candidate_is_not_reused_after_pause_resume(tmp_path):
    sessions = SessionStore(str(tmp_path / "sessions.db"))
    sid = sessions.create_session()
    model = Model([{"content": "暂停前的回答"}, {"content": "恢复并重新核对后的回答"}])
    chat = service(model, session_store=sessions, runtime=AgentRunStore(sessions.db_path))
    control, _ = chat._prepare_chat("原任务", sid, 6)
    try:
        with control.bind():
            first = chat._run_tool_loop(control.message, control.message, [])
            assert first["final_answer"] == "暂停前的回答"
            stored = chat.chat_recovery.load_model_state(control.run_id)
            assert stored["final_answer_candidate"]["accepted"] is True
        chat.runtime.transition(control.run_id, "CHAT_INTERRUPTED", reason="test pause")
    finally:
        control.lease.stop()
    resumed, _ = chat._prepare_chat("继续，并核对最新要求", sid, 6, resume_run_id=control.run_id)
    try:
        assert "final_answer_candidate" not in resumed.loop_state["native_transcript"]
        with resumed.bind():
            second = chat._run_tool_loop(resumed.message, resumed.message, [])
        assert second["final_answer"] == "恢复并重新核对后的回答"
        assert len(model.requests) == 2
    finally:
        resumed.lease.stop()


def test_writer_receives_commit_without_model_or_required_evidence(monkeypatch):
    seen = {}

    class Writer:
        def __init__(self, wiki_store, evidence_store, **kwargs):
            seen["constructor"] = kwargs

        def write(self, **kwargs):
            seen["write"] = kwargs
            return {"card_id": "written", "verified_readback": True}

    monkeypatch.setattr("system.wiki.wiki_chat.RepositoryWikiWriter", Writer)
    model = Model([])
    chat = service(model)
    call = AgentToolCall("wiki_write", {"title": "机制", "repository": "owner/repo", "commit": "a" * 40,
                        "topic": "memory", "sections": [{"heading": "结论", "content": "已自查正文"}]})
    result = chat._execute_agent_tool_call_impl(call, [], [], [], 6)
    assert result.status == "done"
    assert seen["constructor"] == {}
    assert seen["write"]["commit"] == "a" * 40
    assert "evidence_ids" not in seen["write"]["sections"][0]
    assert model.requests == [] and model.answer_calls == 0


def test_actual_writer_errors_remain_visible():
    def fail(**kwargs):
        raise OSError("disk is full")

    chat = service(Model([]), repository_writer=SimpleNamespace(write=fail))
    result = chat._execute_agent_tool_call_impl(AgentToolCall("wiki_write", {}), [], [], [], 6)
    assert result.status == "error"
    assert result.items[0]["message"] == "disk is full"


@pytest.mark.parametrize("tool", ["wiki_open", "read_tool_result"])
def test_native_continuation_keeps_long_wiki_middle_when_context_fits(monkeypatch, tool):
    body = "开头的已知背景。\n" * 3000 + "CARD_MIDDLE_FACT：这里保存关键机制。\n" + "末尾的补充说明。\n" * 3000
    args = {"card_ids": ["long-card"]} if tool == "wiki_open" else {"result_id": 1, "card_id": "long-card"}
    model = Model([
        {"tool_calls": [{"id": "long-read", "function": {"name": tool, "arguments": json.dumps(args)}}]},
        {"content": "中段记录了关键机制。[1]"},
        {"content": "中段记录了关键机制。[1]"},
    ])
    chat = service(model)
    # This is larger than the generic log-spill cap but far below the model window.
    assert "CARD_MIDDLE_FACT" not in chat.context_budget.bound_tool_result(body, 1)

    def read(call, cards, *args, **kwargs):
        cards.append({"id": "long-card", "title": "长卡片", "_full_text": body})
        return AgentToolObservation(tool, "", "done", "opened", [{
            "card_id": "long-card", "content": body, "read_version": "version-1", "view": "page",
        }])

    monkeypatch.setattr(chat, "_execute_agent_tool_call_impl", read)
    monkeypatch.setattr(chat, "_completion_context", lambda observations: "Check completion once.")
    assert chat.chat("解释卡片中段的机制").answer == "中段记录了关键机制。[1]"
    for request in model.requests[1:]:
        results = [m["content"] for m in request["messages"] if m["role"] == "tool"]
        assert len(results) == 1, "unchanged page bodies are not re-appended during continuation"
        assert "CARD_MIDDLE_FACT" in results[0]
    assert model.answer_calls == 0


def test_native_refreshed_wiki_version_keeps_new_long_body(monkeypatch):
    def call(call_id, refresh=False):
        args = {"card_ids": ["same-card"]}
        if refresh:
            args.update(refresh=True, reason="The card changed during this task")
        return {"tool_calls": [{"id": call_id, "function": {"name": "wiki_open", "arguments": json.dumps(args)}}]}

    model = Model([call("first"), call("updated", True), {"content": "更新后的卡片记录了新机制。[1]"}])
    chat = service(model)
    versions = iter(["OLD_MIDDLE_FACT", "NEW_MIDDLE_FACT"])

    def read(call, cards, *args, **kwargs):
        version = next(versions)
        body = "背景信息。\n" * 6000 + version + "\n" + "补充说明。\n" * 6000
        cards[:] = [{"id": "same-card", "title": "版本更新", "_full_text": body, "_read_version": version}]
        return AgentToolObservation("wiki_open", "", "done", "opened", [{
            "card_id": "same-card", "content": body, "read_version": version,
        }])

    monkeypatch.setattr(chat, "_execute_agent_tool_call_impl", read)
    assert chat.chat("检查这张卡片的更新").answer == "更新后的卡片记录了新机制。[1]"
    results = [m for m in model.requests[-1]["messages"] if m["role"] == "tool"]
    assert len(results) == 2
    assert "OLD_MIDDLE_FACT" in results[0]["content"]
    assert "NEW_MIDDLE_FACT" in results[1]["content"]
    assert "OLD_MIDDLE_FACT" not in results[1]["content"]
    assert model.answer_calls == 0

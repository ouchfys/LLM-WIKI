import json
from types import SimpleNamespace
import pytest

from system.agent_runtime.control import RunControl
from system.agent_runtime.task_plans import TaskPlanStore
from system.conversation.session_store import SessionStore
from system.wiki.wiki_chat import AgentToolCall, WikiChatService
from system.wiki.wiki_chat import AgentToolObservation


class _Wiki:
    def list_cards(self, limit=50, offset=0):
        return [
            {
                "id": "paper-qserve",
                "page_type": "PaperPage",
                "title": "QServe",
                "summary": "W4A8KV4 quantization and serving co-design.",
            },
            {
                "id": "topic-kv-cache",
                "page_type": "TopicPage",
                "title": "KV Cache",
                "summary": "A map of cache compression and serving methods.",
            },
        ]


def test_catalog_is_injected_and_open_does_not_require_search_first():
    service = WikiChatService(_Wiki(), wiki_resolver=object())

    prompt = service._tool_loop_prompt(
        message="QServe 的 KV Cache 是多少位？",
        effective_query="QServe KV Cache",
        history=[],
        observations=[],
        step_index=0,
        limit=6,
    )

    assert '"card_id":"paper-qserve"' in prompt
    assert "Select the relevant card_ids and call wiki_open" in prompt
    assert "wiki_search" not in prompt
    assert "local_shell" in prompt
    assert "task_plan_write" not in prompt
    assert "research_next_batch" not in prompt
    assert "KV Cache、RSI" not in prompt


def test_default_native_tools_hide_removed_plans_and_legacy_research_protocol():
    service = WikiChatService(_Wiki(), wiki_resolver=object())
    visible = service._default_native_tool_specs()
    names = {(item.get("function") or {}).get("name") for item in visible}

    assert names == service.DEFAULT_AGENT_TOOLS
    assert {"local_shell", "read_tool_result", "wiki_open"} <= names
    assert {
        "task_plan_read", "task_plan_write", "project_memory_update",
        "research_plan", "research_next_batch", "wiki_search",
        "workspace_read", "search_project_history", "web_search", "web_fetch",
    }.isdisjoint(names)


def test_model_can_finish_without_forced_search_or_web(monkeypatch):
    service = WikiChatService(_Wiki(), wiki_resolver=object(), llm=object())
    monkeypatch.setattr(service, "_next_agent_tool_calls", lambda **kwargs: [])
    executed = []
    monkeypatch.setattr(
        service,
        "_execute_agent_tool_call",
        lambda call, *args, **kwargs: executed.append(call.name),
    )

    result = service._run_tool_loop("你好", "你好", [], limit=6)

    assert executed == []
    assert result["plan"].tools == []


def test_no_model_fallback_never_calls_retired_web_clients():
    class RetiredClient:
        @property
        def available(self):
            raise AssertionError("Chat must not use the retired web clients")

    service = WikiChatService(
        _Wiki(), wiki_resolver=SimpleNamespace(resolve=lambda *args, **kwargs: []),
        web_search=RetiredClient(), web_fetch=RetiredClient(),
        resource_recommender=RetiredClient(),
    )

    result = service._run_tool_loop("搜索最近的开源 Agent 论文", "", [], limit=6)

    assert [call.name for call in result["plan"].tools] == ["wiki_open"]
    assert [item["tool"] for item in result["trace"]["tool_observations"]] == ["wiki_open"]
    assert result["web_results"] == []


@pytest.mark.parametrize("native", [True, False])
def test_query_open_resolves_without_reintroducing_retired_search(native, monkeypatch):
    service = WikiChatService(_Wiki(), wiki_resolver=object())
    args = {"query": "QServe"}
    payload = {"tool_calls": [{"function": {"name": "wiki_open", "arguments": json.dumps(args)}}]} if native else {
        "tool_calls": [{"name": "wiki_open", "arguments": args}]}
    normalize = service._normalize_native_tool_calls if native else service._normalize_agent_tool_calls
    calls = service._apply_query_tool_policy(
        normalize(payload, "QServe", 6), message="解释 QServe", effective_query="QServe",
        observations=[], limit=6,
    )
    assert [call.name for call in calls] == ["wiki_open"]

    resolved = []
    service.wiki_resolver = SimpleNamespace(
        resolve=lambda query, **kwargs: resolved.append(query) or [{"card_id": "paper-qserve"}],
    )
    service.wiki_store = SimpleNamespace(
        get_card=lambda cid: {"id": cid, "title": "QServe", "summary": "supported body"},
        list_linked_pages=lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(service, "_read_card_markdown", lambda card: "# QServe\nSupported body.")
    cards = []
    result = service._execute_agent_tool_call(calls[0], cards, [], [], 6)
    assert result.status == "done"
    assert resolved == ["QServe"]
    assert cards[0]["_full_text"] == "# QServe\nSupported body."



def test_model_can_read_and_answer_without_creating_plan(tmp_path, monkeypatch):
    sessions = SessionStore(str(tmp_path / "sessions.db"), memory_root=str(tmp_path / "memory"))
    sid = sessions.create_session()
    service = WikiChatService(_Wiki(), wiki_resolver=object(), llm=object(),
                              session_store=sessions, task_plans=sessions.task_plans)
    calls = iter([[AgentToolCall("wiki_open", {"card_ids": ["paper-qserve"]})], []])
    monkeypatch.setattr(service, "_next_agent_tool_calls", lambda **kwargs: next(calls))
    monkeypatch.setattr(service, "_execute_agent_tool_call", lambda call, *args, **kwargs:
                        AgentToolObservation(call.name, "", "done", "The card answers the question."))
    control = RunControl(None, "", "QServe 的 KV Cache 是多少位？")
    control.session_id = sid
    with control.bind():
        result = service._run_tool_loop(control.message, "", [], limit=6)
    assert [call.name for call in result["plan"].tools] == ["wiki_open"]
    assert sessions.task_plans.list_active(project_id=sessions.get_session_project_id(sid)) == []
    assert not any(o["tool"] == "runtime_completion_check" for o in result["trace"]["tool_observations"])

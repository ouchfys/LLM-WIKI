"""Model-visible contracts and accepted calls must describe the same tool set."""

import json

import pytest

from system.wiki.agent_tools import DEFAULT_AGENT_TOOLS, REGISTERED_TOOL_NAMES
from system.wiki.wiki_chat import AgentToolCall, WikiChatService


DEFAULT_NAMES = {
    "local_shell",
    "read_tool_result", "wiki_open", "evidence_lookup",
    "arxiv",
    "repository", "wiki_write",
}


def _payload(names, native):
    calls = [{"name": name, "arguments": {"query": "fact", "start_id": 1, "end_id": 2}}
             for name in names]
    if native:
        calls = [{"function": {**call, "arguments": json.dumps(call["arguments"])}}
                 for call in calls]
    return {"tool_calls": calls}


def _normalizer(native):
    return (WikiChatService._normalize_native_tool_calls if native
            else WikiChatService._normalize_agent_tool_calls)


def test_both_routing_modes_expose_identical_default_contracts():
    native = WikiChatService._default_native_tool_specs()
    fallback = WikiChatService._default_tool_specs()

    assert {item["function"]["name"] for item in native} == DEFAULT_NAMES
    assert DEFAULT_AGENT_TOOLS == REGISTERED_TOOL_NAMES == DEFAULT_NAMES
    assert WikiChatService.ALL_TOOL_NAMES == DEFAULT_NAMES
    assert WikiChatService._native_tool_specs() == native
    assert WikiChatService._tool_specs() == fallback
    assert len(native) == len(DEFAULT_NAMES)
    assert fallback == [item["function"] for item in native]
    # JSON routing now receives actual required fields and limits, not examples.
    by_name = {item["name"]: item for item in fallback}
    shell = by_name["local_shell"]["parameters"]
    assert shell["required"] == ["command"]
    assert shell["properties"]["timeout_seconds"]["maximum"] == 300
    # Both tool routes must allow the same detailed cards as the writer.
    sections = by_name["wiki_write"]["parameters"]["properties"]["sections"]
    assert sections["maxItems"] == 16
    assert set(sections["items"]["required"]) == {"heading", "content"}
    assert "evidence_ids" not in sections["items"]["properties"]
    assert "section_id" in sections["items"]["properties"]
    write_params = by_name["wiki_write"]["parameters"]
    assert write_params["properties"]["commit"]["type"] == "string"
    assert "commit" not in write_params["required"]


def test_reading_and_refresh_contracts_distinguish_reference_from_action():
    by_name = {item["name"]: item for item in WikiChatService._default_tool_specs()}
    opened = by_name["wiki_open"]
    assert opened["parameters"]["properties"]["refresh"]["type"] == "boolean"
    assert opened["parameters"]["properties"]["reason"]["type"] == "string"
    assert "Reuse pages already read" in opened["description"]
    assert "does not perform semantic verification" in by_name["evidence_lookup"]["description"]


def test_mutating_one_request_does_not_change_later_contracts():
    first = WikiChatService._default_native_tool_specs()
    first[0]["function"]["description"] = "request-only override"
    first[0]["function"]["parameters"]["properties"].clear()

    later = WikiChatService._default_native_tool_specs()
    assert later[0]["function"]["description"] != "request-only override"
    assert "command" in later[0]["function"]["parameters"]["properties"]


@pytest.mark.parametrize("native", [False, True])
def test_repository_navigation_keeps_schema_fields_and_has_no_implicit_query(native):
    args = {"operation": "list", "snapshot_id": "a" * 24, "path": "src", "limit": 80}
    item = {"name": "repository", "arguments": args}
    payload = {"tool_calls": [{"function": {**item, "arguments": json.dumps(args)}}] if native else [item]}
    call = _normalizer(native)(payload, "研究四个项目并写入wiki", 6)[0]
    assert call.arguments["query"] == ""
    assert all(call.arguments[key] == value for key, value in args.items())


@pytest.mark.parametrize("native", [False, True])
def test_final_card_without_evidence_ids_survives_both_routing_modes(native):
    args = {
        "title": "示例项目的对话保存方式", "repository": "sample/project", "topic": "memory-system",
        "commit": "a" * 40, "revision_id": "existing-revision",
        "sections": [{"section_id": "s1", "heading": "对话在哪里？", "content": "会话保存在本地文件。"}],
    }
    item = {"name": "wiki_write", "arguments": args}
    payload = {"tool_calls": [{"function": {**item, "arguments": json.dumps(args)}}] if native else [item]}

    calls = _normalizer(native)(payload, "更新卡片", 4)

    assert len(calls) == 1
    assert calls[0].name == "wiki_write"
    assert all(calls[0].arguments[key] == value for key, value in args.items())


@pytest.mark.parametrize("native", [False, True])
def test_unadvertised_legacy_and_unknown_calls_are_rejected(native):
    calls = _normalizer(native)(
        _payload(["resource_recommend", "web_search", "web_fetch", "invented_tool", "read_tool_result"], native), "fact", 4,
    )
    assert [call.name for call in calls] == ["read_tool_result"]


@pytest.mark.parametrize("native", [False, True])
def test_explicit_advertised_subset_also_rejects_other_default_tools(native):
    calls = _normalizer(native)(
        _payload(["wiki_open", "read_tool_result"], native), "fact", 4,
        allowed_tools={"read_tool_result"},
    )
    assert [call.name for call in calls] == ["read_tool_result"]
    assert _normalizer(native)(
        _payload(["read_tool_result"], native), "fact", 4, allowed_tools=set(),
    ) == []


@pytest.mark.parametrize("native", [False, True])
def test_explicit_allowlist_cannot_restore_retired_tools(native):
    calls = _normalizer(native)(
        _payload(["read_session_messages", "web_search", "web_fetch", "invented_tool", "read_tool_result"], native),
        "fact", 4, allowed_tools={"read_session_messages", "web_search", "web_fetch", "invented_tool"},
    )
    assert calls == []


@pytest.mark.parametrize("name", ["arxiv_lookup", "arxiv_import_paper", "arxiv_ingestion_status", "arxiv_search", "resource_recommend", "wiki_card", "research_plan", "web_search", "web_fetch", "task_plan_read", "task_plan_write", "project_memory_update"])
def test_execution_boundary_rejects_retired_calls_before_dispatch(name, monkeypatch):
    service = WikiChatService(object(), wiki_resolver=object())

    def unexpected_dispatch(*args, **kwargs):
        pytest.fail("Retired tool reached its implementation")

    monkeypatch.setattr(service, "_execute_agent_tool_call_impl", unexpected_dispatch)
    result = service._execute_agent_tool_call(AgentToolCall(name), [], [], [], 4)
    assert result.status == "error"
    assert "not registered" in result.summary


@pytest.mark.parametrize("native", [False, True])
def test_request_and_response_share_subclass_tool_selection(native):
    class ReadResultService(WikiChatService):
        DEFAULT_AGENT_TOOLS = frozenset({"read_tool_result"})

    class Planner:
        def tool_call(self, *, tools, **kwargs):
            assert [item["function"]["name"] for item in tools] == ["read_tool_result"]
            return _payload(["local_shell", "read_tool_result"], True)

        def invoke(self, prompt, **kwargs):
            return json.dumps(_payload(["local_shell", "read_tool_result"], False))

    service = ReadResultService(object(), wiki_resolver=object(), llm=Planner())
    if native:
        calls = service._next_native_tool_calls("resume", "resume", [], [], 0, 4)
    else:
        calls = service._normalize_agent_tool_calls(
            _payload(["local_shell", "read_tool_result"], False), "resume", 4,
        )
        assert [item["name"] for item in service._default_tool_specs()] == ["read_tool_result"]
    assert [call.name for call in calls] == ["read_tool_result"]


def test_legacy_internal_thought_does_not_become_public_call_reason():
    payload = _payload(["read_tool_result"], False)
    payload.update(thought="private reasoning", reasoning_content="private reasoning")
    assert WikiChatService._normalize_agent_tool_calls(payload, "resume", 4)[0].reason == ""
    payload["progress"] = "读取现有任务计划。"
    assert WikiChatService._normalize_agent_tool_calls(payload, "resume", 4)[0].reason == "读取现有任务计划。"


def test_native_response_is_limited_to_the_exact_request_tool_list(monkeypatch):
    class Planner:
        def tool_call(self, *, tools, **kwargs):
            assert [item["function"]["name"] for item in tools] == ["read_tool_result"]
            return _payload(["local_shell", "read_tool_result"], True)

    service = WikiChatService(object(), wiki_resolver=object(), llm=Planner())
    selected = [item for item in service._default_native_tool_specs()
                if item["function"]["name"] == "read_tool_result"]
    monkeypatch.setattr(service, "_default_native_tool_specs", lambda: selected)

    calls = service._next_native_tool_calls("resume", "resume", [], [], 0, 4)

    assert [call.name for call in calls] == ["read_tool_result"]


@pytest.mark.parametrize("native", [False, True])
def test_removed_auxiliary_tools_cannot_be_restored_by_allowlist(native):
    retired = {"task_plan_read", "task_plan_write", "project_memory_update"}
    assert _normalizer(native)(_payload(sorted(retired), native), "continue", 4, allowed_tools=retired) == []
    service = WikiChatService(object(), wiki_resolver=object())
    prompt = service._tool_loop_prompt("continue", "continue", [], [], 0, 4)
    assert all(name not in prompt for name in retired)

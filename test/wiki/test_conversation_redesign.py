"""Behavioral regression tests; no provider or user database is used."""
import json
import sqlite3

import pytest

from system.agent_runtime.control import RunControl
from system.conversation.context_budget import ContextBudget, ContextPolicy
from system.conversation.session_store import SessionStore
from system.memory.profile_signal_extractor import ProfileSignalExtractor
from system.wiki.wiki_chat import AgentToolCall, AgentToolObservation, WikiChatService
from test_context_budget import ByteCounter, SummaryLLM, populated, small_budget


def test_model_window_and_optional_runtime_cap(monkeypatch):
    for key in ("WINDOW", "RUNTIME_LIMIT", "SUMMARY", "COMPACT_TRIGGER", "COMPACT_KEEP"):
        monkeypatch.delenv("PAPERWIKI_CONTEXT_" + key, raising=False)
    policy = ContextPolicy.from_env("deepseek-v4-flash")
    assert policy.window == 1_000_000
    assert policy.history == policy.planning_history == policy.input_limit
    assert policy.compact_trigger == 800_000
    monkeypatch.setenv("PAPERWIKI_CONTEXT_RUNTIME_LIMIT", "100000")
    capped = ContextPolicy.from_env("deepseek-v4-flash")
    assert capped.window == 100000 and capped.compact_trigger == 80000
    with pytest.raises(ValueError, match="unknown model"):
        ContextPolicy.from_env("unknown-provider-model")
    monkeypatch.setenv("PAPERWIKI_CONTEXT_WINDOW", "2000000")
    with pytest.raises(ValueError, match="exceeds"):
        ContextPolicy.from_env("deepseek-v4-flash")


def test_uncompressed_history_larger_than_old_8k_is_shared():
    budget = ContextBudget(ContextPolicy(), ByteCounter())
    service = WikiChatService(object(), wiki_resolver=object(), context_budget=budget)
    history = [(f"original-constraint-{i}", "abc " * 600) for i in range(12)]
    for prompt in (service._build_prompt("current", [], history),
                   service._tool_loop_prompt("current", "query", history, [], 0, 4),
                   json.dumps(service._native_tool_messages("current", "query", history, [], 0, 4))):
        assert "original-constraint-0" in prompt
        assert "original-constraint-11" in prompt


def test_request_pressure_includes_material_and_prunes_tools_first():
    budget = small_budget()
    pressures = []
    with budget.on_pressure(pressures.append):
        prompt = budget.compose("current", [("Previous observations", "large tool output " * 300, 6000),
                                            ("history", "history " * 100, 1200)], extra_tokens=400)
    assert "省略" in prompt
    assert pressures[0] < 3000  # Original tool output alone was > 5000 bytes.
    budget.check_request(prompt, output_tokens=1200)


def test_checkpoint_recovery_history_search_and_deletion(tmp_path):
    from system.conversation.context_compaction import auto_compact
    store, sid = populated(tmp_path)
    other = store.create_session()
    store.save_message(other, "user", "SECRET-OTHER-SESSION")
    store.upsert_preference("language", "中文", "以后用中文", source_session_id=sid)
    result_id = store.save_tool_result(sid, "wiki_search", {"query": "QServe"}, {"content": "X" * 8000 + "TAIL"})
    assert auto_compact(store, sid, SummaryLLM(), small_budget())["status"] == "compacted"
    reopened = SessionStore(store.db_path)
    checkpoints = reopened.get_context_checkpoints(sid)
    assert len(checkpoints) == 1
    assert json.loads(checkpoints[0]["stats_json"])["history_tokens_before"] > 0
    hit = reopened.search_session_history(sid, "Question-0")[0]
    assert hit["message_id"] <= checkpoints[0]["through_message_id"]
    assert reopened.read_session_messages(sid, hit["message_id"], hit["message_id"])[0]["content"].startswith("Question-0")
    assert reopened.search_session_history(sid, "SECRET-OTHER-SESSION") == []
    assert reopened.read_tool_result(other, result_id) == []
    assert "TAIL" in reopened.read_tool_result(sid, result_id, 8000)[0]["content"]
    assert reopened.delete_session(sid)
    assert reopened.get_context_checkpoints(sid) == []
    assert reopened.read_tool_result(sid, result_id) == []
    assert reopened.get_preference("language") == "中文"
    assert reopened.get_session(other)


@pytest.mark.parametrize("method", ["clear_session", "delete_all_sessions"])
def test_all_clear_paths_remove_derived_records(tmp_path, method):
    store, sid = populated(tmp_path)
    cutoff = store.get_messages(sid, 100)[1]["id"]
    assert store.commit_context_summary(sid, "summary", cutoff, 0, "")
    result_id = store.save_tool_result(sid, "test", {}, {"raw": "value"})
    getattr(store, method)(sid) if method == "clear_session" else getattr(store, method)()
    assert store.get_context_checkpoints(sid) == []
    assert store.read_tool_result(sid, result_id) == []


def test_legacy_database_migration_is_idempotent(tmp_path):
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE user_profile (key TEXT PRIMARY KEY, value TEXT, evidence TEXT, updated_at TEXT)")
        conn.execute("INSERT INTO user_profile VALUES ('language', '中文', 'original', '2026')")
    SessionStore(path)
    store = SessionStore(path)
    assert store.get_preference("language") == "中文"
    assert store.get_all_preferences_detailed()[0]["source_session_id"] == ""


def test_history_tools_reach_both_model_prompt_paths(tmp_path):
    store, sid = populated(tmp_path)
    service = WikiChatService(object(), wiki_resolver=object(), session_store=store, context_budget=small_budget())
    control = RunControl(None, "", "read earlier")
    control.session_id = sid
    for normalize, raw in [
        (service._normalize_agent_tool_calls, {"tool_calls": [{"name": "read_session_messages", "arguments": {"start_id": 1, "end_id": 2}}]}),
        (service._normalize_native_tool_calls, {"tool_calls": [{"function": {"name": "read_session_messages", "arguments": '{"start_id": 1, "end_id": 2}'}}]}),
    ]:
        calls = normalize(raw, "earlier", 4)
        assert calls[0].arguments["start_id"] == 1
    with control.bind():
        observation = service._execute_agent_tool_call(AgentToolCall("search_session_history", {"query": "Question-0"}), [], [], [], 4)
    assert "Question-0" in service._observation_context([observation])
    assert "Question-0" in service._answer_observation_context([service._observation_payload(observation)])
    assert "result_id=" in observation.summary


def test_temporary_requests_and_paper_language_are_not_durable_preferences():
    extractor = ProfileSignalExtractor()
    for text in ["这次用中文详细回答", "请展开讲讲", "默认检索英文论文", "以后不要用英文回答"]:
        assert extractor.extract(text)["preferences"] == []
    preferences = extractor.extract("以后用中文回答，先给结论")["preferences"]
    assert {"key": "language_preference", "value": "中文"} in preferences


def test_wiki_prompt_explicitly_treats_documents_as_knowledge():
    service = WikiChatService(object(), wiki_resolver=object(), context_budget=small_budget())
    prompt = service._build_prompt("question", [], [])
    assert "paper knowledge base, not personal memory" in prompt
    assert "stable personal memory" not in prompt


def test_compaction_under_tool_loop_pressure_updates_shared_history(tmp_path):
    store, sid = populated(tmp_path)
    budget = small_budget()
    observed = []

    class Service(WikiChatService):
        def _run_tool_loop(self, message, effective_query, history, **kwargs):
            self._tool_loop_prompt(message, effective_query, history, [], 0, 4)
            observed.append(list(history))
            from system.wiki.wiki_chat import WikiToolPlan
            return {"plan": WikiToolPlan(), "cards": [], "web_results": [], "resources": [], "trace": {"tool_observations": []}}

        def _answer(self, message, cards, history, *args, **kwargs):
            assert history == observed[-1]
            return "done"

        def _save_turn(self, *args, **kwargs):
            pass

    service = Service(object(), wiki_resolver=object(), session_store=store, llm=SummaryLLM(), context_budget=budget)
    service.chat("current", sid)
    assert observed[0][0][0] == "[SYSTEM_CONTEXT_SUMMARY]"
    assert len(store.get_context_checkpoints(sid)) == 1

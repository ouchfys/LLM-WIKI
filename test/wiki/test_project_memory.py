import json
import sqlite3

import pytest

from backend.api.wiki import ProjectPurposePayload, project_purpose_command
from system.agent_runtime.control import RunControl
from system.conversation.session_store import SessionStore
from system.memory.project_context import ProjectContextManager
from system.memory.project_memory import DEFAULT_PROJECT_ID
from system.wiki.wiki_chat import AgentToolCall, WikiChatService


def test_existing_sessions_migrate_into_default_project(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, created_at TEXT, settings_json TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('old', 'Old chat', '2026', '{}')")

    store = SessionStore(str(path))

    assert store.get_session("old")["project_id"] == DEFAULT_PROJECT_ID
    assert store.get_project(DEFAULT_PROJECT_ID)["name"] == "PaperWiki 研究项目"


def test_project_purpose_and_model_managed_memory_are_shared_without_session_state(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.db"))
    first = store.create_session("first")
    second = store.create_session("second")
    message_id = store.save_message(first, "user", "这个项目不考虑定制 CUDA kernel")
    store.set_project_purpose(
        DEFAULT_PROJECT_ID,
        "# 项目目标\n研究低成本 LLM Serving",
        evidence="这个项目研究低成本推理",
        source_session_id=first,
    )
    store.update_session_state(first, {"current_goal": "比较 QServe"}, through_message_id=message_id)
    store.write_project_memory(
        first,
        """# Project Memory

## Constraints

- 暂时排除需要定制 CUDA kernel 的方案
""",
    )

    context = store.render_project_context(second, "CUDA kernel")
    memory_dir = tmp_path / ".paperwiki" / "memory" / "projects" / DEFAULT_PROJECT_ID

    assert "研究低成本 LLM Serving" in context
    assert "暂时排除" in context
    assert "比较 QServe" not in context
    assert "[PROJECT_MEMORY_FILE]" in context
    assert (memory_dir / "purpose.md").exists()
    assert (memory_dir / "MEMORY.md").exists()
    assert "暂时排除" in (memory_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert store.get_session_state(second) == {}


def test_project_history_cannot_cross_project_boundary(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.db"))
    first = store.create_session("default")
    store.save_message(first, "user", "PROJECT-DEFAULT-SECRET")
    store.create_project("Other", "other")
    other = store.create_session("other", project_id="other")
    other_id = store.save_message(other, "user", "PROJECT-OTHER-SECRET")

    hits = store.search_project_history(DEFAULT_PROJECT_ID, "SECRET")

    assert [item["session_id"] for item in hits] == [first]
    assert store.read_project_messages(DEFAULT_PROJECT_ID, other, other_id, other_id) == []


def test_model_resolves_followup_and_memory_is_updated_directly(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.db"))
    session_id = store.create_session("research")
    user_text = "这个项目以后不考虑定制 kernel；以后回答先说结论"
    user_id = store.save_message(session_id, "user", user_text)
    assistant_id = store.save_message(session_id, "assistant", "已按工程约束重新筛选。")

    def invoke(_prompt, operation, _max_tokens):
        if operation == "memory.resolve_turn":
            return json.dumps({
                "is_followup": True,
                "standalone_query": "在不能修改 CUDA kernel 的约束下继续比较 QServe",
                "referenced_context": ["QServe"],
            }, ensure_ascii=False)
        raise AssertionError("follow-up resolution should be the only model operation")

    manager = ProjectContextManager(store, invoke)
    resolved = manager.resolve_turn(session_id, "那 QServe 呢", "上一轮讨论量化", "project", "fallback")
    saved = store.write_project_memory(
        session_id,
        """# Project Memory

## Constraints

- 不能修改 CUDA kernel

## Decisions

- 回答先说结论
""",
    )

    assert resolved == "在不能修改 CUDA kernel 的约束下继续比较 QServe"
    assert "不能修改 CUDA kernel" in saved["content"]
    assert "回答先说结论" in store.render_project_context(session_id, "QServe")
    assert store.get_session_state(session_id) == {}


def test_model_managed_memory_replaces_stale_project_state(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.db"))
    session_id = store.create_session("research")
    store.write_project_memory(session_id, "# Project Memory\n\n## Constraints\n\n- 不允许定制 CUDA kernel")
    store.write_project_memory(session_id, "# Project Memory\n\n## Constraints\n\n- 允许定制 CUDA kernel")

    content = store.project_memory_files.read_memory(DEFAULT_PROJECT_ID)
    assert "允许定制 CUDA kernel" in content
    assert "不允许定制 CUDA kernel" not in content


def test_agent_memory_tool_updates_only_the_current_project_file(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.db"))
    session_id = store.create_session("memory tool")
    service = WikiChatService(
        wiki_store=object(), wiki_resolver=object(), session_store=store,
    )
    control = RunControl(None, "", "更新项目状态")
    control.session_id = session_id

    with control.bind():
        observation = service._execute_agent_tool_call(
            AgentToolCall(
                "project_memory_update",
                {"content": "# Project Memory\n\n## Decisions\n\n- 使用 SQLite"},
            ),
            [], [], [], 4,
        )

    assert observation.status == "done"
    assert "使用 SQLite" in store.project_memory_files.read_memory(DEFAULT_PROJECT_ID)
    assert not store.search_project_memories(DEFAULT_PROJECT_ID, "SQLite")

    # A later compatibility sync must not replace model-managed notes with the
    # old generated index/topic projection.
    store.add_project_memory(
        DEFAULT_PROJECT_ID,
        "decision",
        "legacy compatibility record",
        source_session_id=session_id,
    )
    assert "使用 SQLite" in store.project_memory_files.read_memory(DEFAULT_PROJECT_ID)
    assert "legacy compatibility record" not in store.project_memory_files.read_memory(DEFAULT_PROJECT_ID)


def test_model_managed_memory_rejects_oversize_write_without_truncating(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.db"))
    session_id = store.create_session("bounded memory")
    original = "# Project Memory\n\n## Decisions\n\n- 保留原内容"
    store.write_project_memory(session_id, original)

    with pytest.raises(ValueError, match="exceeds"):
        store.write_project_memory(
            session_id,
            "# Project Memory\n\n" + "长" * (store.project_memory_files.MAX_MEMORY_CHARS + 1),
        )

    assert "保留原内容" in store.project_memory_files.read_memory(DEFAULT_PROJECT_ID)


def test_memory_files_preserve_manual_notes_and_reject_invalid_topics(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.db"))
    session_id = store.create_session("memory files")
    directory = tmp_path / ".paperwiki" / "memory" / "projects" / DEFAULT_PROJECT_ID
    topic_path = directory / "topics" / "failed-attempts.md"
    topic_path.write_text(
        topic_path.read_text(encoding="utf-8").replace(
            "可在这里补充人工备注；自动同步不会覆盖本节。",
            "人工备注：不要重复运行旧解析器。",
        ),
        encoding="utf-8",
    )

    store.add_project_memory(
        DEFAULT_PROJECT_ID,
        "failed_attempt",
        "旧解析器会丢失表格结构",
        source_session_id=session_id,
        confidence=0.99,
        importance=0.9,
    )

    content = topic_path.read_text(encoding="utf-8")
    assert "旧解析器会丢失表格结构" in content
    assert "人工备注：不要重复运行旧解析器。" in content
    try:
        store.open_project_memory_topic(session_id, "../purpose")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid project-memory topic must be rejected")


def test_invalid_supersedes_id_does_not_create_a_memory(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.db"))

    saved = store.add_project_memory(
        DEFAULT_PROJECT_ID,
        "decision",
        "采用不存在的替代关系",
        supersedes_id=999999,
    )

    assert saved is None
    assert store.search_project_memories(DEFAULT_PROJECT_ID, "不存在的替代关系") == []


def test_purpose_command_view_update_and_clear(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.db"))
    session_id = store.create_session("purpose")

    updated = project_purpose_command(
        session_id,
        ProjectPurposePayload(instruction="研究可复现的推理优化", use_llm=False),
        session_store=store,
    )
    viewed = project_purpose_command(
        session_id,
        ProjectPurposePayload(),
        session_store=store,
    )
    cleared = project_purpose_command(
        session_id,
        ProjectPurposePayload(instruction="清除", use_llm=False),
        session_store=store,
    )

    assert updated["purpose"] == "研究可复现的推理优化"
    assert "研究可复现的推理优化" in viewed["answer"]
    assert cleared["purpose"] == ""
    assert all(question != "/purpose" for question, _ in store.get_history(session_id, None))
    assert len(store.list_project_events(DEFAULT_PROJECT_ID)) == 2

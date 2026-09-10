import json
import sqlite3

from backend.api.wiki import ProjectPurposePayload, project_purpose_command
from system.conversation.session_store import SessionStore
from system.memory.project_context import ProjectContextManager
from system.memory.project_memory import DEFAULT_PROJECT_ID


def test_existing_sessions_migrate_into_default_project(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, created_at TEXT, settings_json TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('old', 'Old chat', '2026', '{}')")

    store = SessionStore(str(path))

    assert store.get_session("old")["project_id"] == DEFAULT_PROJECT_ID
    assert store.get_project(DEFAULT_PROJECT_ID)["name"] == "PaperWiki 研究项目"


def test_project_purpose_and_memory_are_shared_without_sharing_session_state(tmp_path):
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
    store.update_project_state(
        DEFAULT_PROJECT_ID,
        {"constraints": ["不考虑定制 CUDA kernel"]},
        through_message_id=message_id,
        source_session_id=first,
    )
    store.add_project_memory(
        DEFAULT_PROJECT_ID,
        "decision",
        "暂时排除需要定制 CUDA kernel 的方案",
        source_session_id=first,
        evidence_message_ids=[message_id],
        confidence=0.95,
        importance=0.9,
    )

    context = store.render_project_context(second, "CUDA kernel")
    memory_dir = tmp_path / ".paperwiki" / "memory" / "projects" / DEFAULT_PROJECT_ID

    assert "研究低成本 LLM Serving" in context
    assert "暂时排除" in context
    assert "比较 QServe" not in context
    assert "[PROJECT_MEMORY_INDEX]" in context
    assert (memory_dir / "purpose.md").exists()
    assert (memory_dir / "MEMORY.md").exists()
    assert "暂时排除" in (memory_dir / "topics" / "decisions.md").read_text(encoding="utf-8")
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


def test_model_resolves_followup_and_distills_validated_project_memory(tmp_path):
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
        return json.dumps({
            "session_state": {
                "current_goal": "比较 QServe",
                "active_entities": ["QServe"],
                "constraints": ["不能修改 CUDA kernel"],
                "decisions": [],
                "open_questions": [],
                "last_turn_summary": "增加工程约束",
            },
            "project_state": {
                "goals": [],
                "active_topics": ["QServe"],
                "constraints": ["不能修改 CUDA kernel"],
                "decisions": [],
                "open_questions": [],
                "milestones": [],
            },
            "user_preferences": [{
                "key": "answer_style",
                "value": "conclusion_first",
                "evidence_quote": "以后回答先说结论",
                "confidence": 0.94,
            }],
            "memory_patches": [{
                "operation": "upsert",
                "target": "constraints",
                "content": "不能修改 CUDA kernel",
                "evidence_quote": "这个项目以后不考虑定制 kernel",
                "confidence": 0.96,
                "importance": 0.9,
                "durability": "durable",
            }, {
                "operation": "upsert",
                "target": "decisions",
                "content": "低置信度内容不应落库",
                "evidence_quote": "以后回答先说结论",
                "confidence": 0.4,
                "importance": 0.9,
                "durability": "durable",
            }],
        }, ensure_ascii=False)

    manager = ProjectContextManager(store, invoke)
    resolved = manager.resolve_turn(session_id, "那 QServe 呢", "上一轮讨论量化", "project", "fallback")
    updates = manager.maintain_turn(
        session_id,
        user_text,
        "已按工程约束重新筛选。",
        evidence_message_ids=[user_id, assistant_id],
        cards=[{"id": "paper-qserve", "title": "QServe"}],
    )

    assert resolved == "在不能修改 CUDA kernel 的约束下继续比较 QServe"
    assert store.get_session_state(session_id)["selected_wiki_pages"][0]["card_id"] == "paper-qserve"
    assert "不能修改 CUDA kernel" in store.get_project_state(DEFAULT_PROJECT_ID)["constraints"]
    assert len(store.search_project_memories(DEFAULT_PROJECT_ID, "CUDA kernel")) == 1
    assert updates == [
        {"signal_type": "preference", "value": "answer_style=conclusion_first"},
        {"signal_type": "project_constraint", "value": "不能修改 CUDA kernel"},
    ]
    preference = store.get_all_preferences_detailed()[0]
    assert preference["evidence_message_id"] == user_id
    assert preference["source_session_id"] == session_id
    assert preference["confidence"] == 0.94
    assert "conclusion_first" in (
        tmp_path / ".paperwiki" / "memory" / "preferences.md"
    ).read_text(encoding="utf-8")
    topic = store.open_project_memory_topic(session_id, "constraints")
    assert topic["path"] == "topics/constraints.md"
    assert "不能修改 CUDA kernel" in topic["content"]


def test_new_project_memory_can_supersede_conflicting_old_memory(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.db"))
    session_id = store.create_session("research")
    old = store.add_project_memory(
        DEFAULT_PROJECT_ID,
        "constraint",
        "不允许定制 CUDA kernel",
        source_session_id=session_id,
        confidence=0.98,
        importance=0.9,
    )
    store.replace_project_state(DEFAULT_PROJECT_ID, {"constraints": [old["content"]]})
    message = "项目约束改为允许定制 kernel"
    user_id = store.save_message(session_id, "user", message)
    assistant_id = store.save_message(session_id, "assistant", "已更新约束。")

    def invoke(_prompt, _operation, _max_tokens):
        return json.dumps({
            "session_state": {},
            "user_preferences": [],
            "memory_patches": [{
                "operation": "supersede",
                "target": "constraints",
                "content": "允许定制 CUDA kernel",
                "evidence_quote": message,
                "confidence": 0.97,
                "importance": 0.9,
                "durability": "durable",
                "supersedes_id": old["id"],
            }],
        }, ensure_ascii=False)

    ProjectContextManager(store, invoke).maintain_turn(
        session_id,
        message,
        "已更新约束。",
        evidence_message_ids=[user_id, assistant_id],
    )

    assert store.get_project_memory(old["id"])["status"] == "superseded"
    active = store.search_project_memories(DEFAULT_PROJECT_ID, "CUDA kernel")
    assert [item["content"] for item in active] == ["允许定制 CUDA kernel"]
    assert store.get_project_state(DEFAULT_PROJECT_ID)["constraints"] == ["允许定制 CUDA kernel"]


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

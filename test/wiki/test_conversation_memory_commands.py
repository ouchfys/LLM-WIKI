from types import SimpleNamespace

from backend.api.wiki import (
    SessionCompactPayload,
    _conversation_messages_for_command,
    compact_chat_session,
)
from system.memory.session_store import SessionStore
from system.wiki.maintenance.query_insight import QueryInsightDistiller


def test_compaction_keeps_raw_history_but_changes_model_context(tmp_path):
    db_path = tmp_path / "sessions.db"
    store = SessionStore(db_path=str(db_path))
    session_id = store.create_session("compact test")
    first_user_id = store.save_message(session_id, "user", "旧问题：Agent Runtime 是什么？")
    first_answer_id = store.save_message(session_id, "assistant", "旧回答：它负责状态和恢复。")
    store.save_message(session_id, "user", "新问题：lease 是什么？")
    store.save_message(session_id, "assistant", "新回答：它是有期限的任务所有权。")

    result = compact_chat_session(
        session_id,
        SessionCompactPayload(keep_recent_turns=1, use_llm=False),
        session_store=store,
        wiki_store=SimpleNamespace(db_path=str(db_path)),
    )

    assert result["ok"] is True
    assert result["compacted_through_message_id"] == first_answer_id
    assert first_user_id < result["compacted_through_message_id"]

    model_history = store.get_history(session_id, last_n=10)
    assert model_history[0][0] == "[SYSTEM_CONTEXT_SUMMARY]"
    assert "Agent Runtime" in model_history[0][1]
    assert ("新问题：lease 是什么？", "新回答：它是有期限的任务所有权。") in model_history
    assert all(question != "/compact" for question, _ in model_history)

    display_history = store.get_display_history(session_id, last_n=20)
    assert display_history[0][0] == "旧问题：Agent Runtime 是什么？"
    assert any(question == "/compact" for question, _, _ in display_history)


def test_conversation_slice_uses_persisted_ids_and_skips_previous_commands():
    messages = [
        {"id": 1, "role": "user", "content": "讨论任务记忆", "metadata": {}},
        {"id": 2, "role": "assistant", "content": "任务记忆保存执行进度", "metadata": {}},
        {"id": 3, "role": "user", "content": "/compact", "metadata": {"command": "compact"}},
        {"id": 4, "role": "assistant", "content": "已压缩", "metadata": {"command": "compact"}},
    ]

    selected = _conversation_messages_for_command(messages)

    assert [item["id"] for item in selected] == ["1", "2"]
    assert selected[1]["content"] == "任务记忆保存执行进度"


def test_wiki_command_distills_conversation_into_source_note(tmp_path):
    distiller = QueryInsightDistiller(db_path=tmp_path / "wiki.db", llm=None)
    artifact = {
        "query_id": "conversation-test",
        "source_type": "conversation_command",
        "session_id": "session-1",
        "instruction": "把任务记忆和用户记忆的区别保存下来",
        "artifact_uri": "local://queries/conversation-test.md",
        "messages": [
            {"id": "11", "role": "user", "content": "任务记忆和用户记忆有什么区别？"},
            {"id": "12", "role": "assistant", "content": "任务记忆用于恢复执行；用户记忆用于跨会话个性化。"},
        ],
        "citations": [],
    }

    candidate = distiller._conversation_candidate(artifact)

    assert candidate["status"] == "candidate_ready"
    assert candidate["candidate_type"] == "source_note"
    assert candidate["content_json"]["source_type"] == "conversation_insight"
    assert candidate["content_json"]["source_message_ids"] == ["11", "12"]
    assert candidate["content_json"]["knowledge_kind"] == "discussion_conclusion"


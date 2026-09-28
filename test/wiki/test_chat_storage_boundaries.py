"""Ordinary chat persists in SQLite without producing exported answer files."""

from contextlib import nullcontext
import sqlite3

import pytest

from system.agent_runtime import AgentRunStore, LocalShell
from system.agent_runtime.control import RunControl
from system.conversation.session_store import SessionStore
from system.storage.layout import StorageLayout
from system.wiki.maintenance.query_archive import QueryArchive
from system.wiki.wiki_chat import WikiChatService, WikiCitation


@pytest.mark.parametrize("recoverable", [False, True])
def test_save_turn_keeps_messages_and_metadata_without_automatic_export(tmp_path, monkeypatch, recoverable):
    sessions = SessionStore(str(tmp_path / "sessions.db"))
    sid = sessions.create_session()
    runtime = AgentRunStore(sessions.db_path) if recoverable else None
    chat = WikiChatService(object(), wiki_resolver=object(), session_store=sessions, runtime=runtime)
    exports = []
    monkeypatch.setattr(QueryArchive, "archive_turn", lambda *args, **kwargs: exports.append(kwargs))
    question = "这张 Wiki 卡片讲了什么？"
    answer = "卡片说明会话保存在日志中，压缩会保留摘要及近期内容。" * 12
    citations = [WikiCitation("card-a", "会话记忆", "ConceptPage", "摘要", "wiki/concepts/card-a.md")]
    trace = {"tool_observations": [{"tool": "wiki_open", "status": "done", "items": [{"card_id": "card-a"}]}]}
    control = None
    if runtime:
        run = runtime.create_run(run_type="wiki_chat", source_uri=f"session:{sid}")
        runtime.transition(run["id"], "CHAT_RUNNING")
        control = RunControl(runtime, run["id"], question)
        control.session_id = sid
    with control.bind() if control else nullcontext():
        ids = chat._save_turn(sid, question, answer, citations, [], [], trace=trace)
        if recoverable:
            assert chat._save_turn(sid, question, answer, citations, [], [], trace=trace) == ids
    messages = sessions.get_messages(sid)
    assert [message["id"] for message in messages] == ids
    assert [(message["role"], message["content"]) for message in messages] == [("user", question), ("assistant", answer)]
    assert messages[1]["metadata"]["citations"][0]["card_id"] == "card-a"
    assert messages[1]["metadata"]["trace"]["tool_observations"][0]["tool"] == "wiki_open"
    assert exports == []
    assert not (tmp_path / "queries").exists()
    assert not (tmp_path / "tmp").exists()
    with sqlite3.connect(sessions.db_path) as conn:
        table = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='wiki_query_insights'").fetchone()
        if table:
            assert conn.execute("SELECT COUNT(*) FROM wiki_query_insights").fetchone()[0] == 0


@pytest.mark.parametrize("explicit_cwd", [False, True])
def test_chat_prompt_reports_actual_shell_directory_without_creating_it(tmp_path, monkeypatch, explicit_cwd):
    layout = StorageLayout(tmp_path / "data")
    monkeypatch.setattr("system.storage.layout.get_storage_layout", lambda: layout)
    monkeypatch.setattr("system.agent_runtime.local_shell.get_storage_layout", lambda: layout)
    shell = LocalShell(tmp_path / "user-selected") if explicit_cwd else LocalShell()
    chat = WikiChatService(object(), wiki_resolver=object(), local_shell=shell)
    control = RunControl(None, "run-id", "")
    control.session_id = "session-id"
    with control.bind():
        rules = chat._local_agent_rules()
        assert str(shell.cwd) in rules
    assert str(layout.projects_dir) in rules
    assert ".paperwiki/research" not in rules
    assert "Do not read archives" in rules
    assert not (layout.data_root / "tmp").exists()
    assert not layout.projects_dir.exists()

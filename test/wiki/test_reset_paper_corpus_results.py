"""Reset must also remove semantic retrieval rows, while keeping conversations."""

import sqlite3

from scripts.reset_paper_corpus_results import _backup_sqlite, _clear_db
from system.agent_runtime import AgentRunStore
from system.conversation.session_store import SessionStore


def test_corpus_reset_preserves_chat_runtime_and_sessions(tmp_path):
    db_path = tmp_path / "scope.db"
    sessions = SessionStore(str(db_path))
    session_id = sessions.create_session("keep")
    runtime = AgentRunStore(str(db_path))
    chat = runtime.create_run(
        run_type="wiki_chat",
        source_uri=f"session:{session_id}",
        context={"session_id": session_id},
    )
    paper = runtime.create_run(run_type="paper_ingestion", source_uri="paper.pdf")
    runtime.append_event(chat["id"], event_type="tool.completed", tool_name="wiki_search")
    runtime.append_event(paper["id"], event_type="model.completed", model="validator")
    with sqlite3.connect(db_path) as conn:
        chat_event_count = conn.execute(
            "SELECT COUNT(*) FROM agent_events WHERE run_id=?", (chat["id"],)
        ).fetchone()[0]

    deleted = _clear_db(db_path)

    assert deleted["agent_runs(non_chat)"] == 1
    assert sessions.get_session(session_id) is not None
    assert runtime.get_run(chat["id"]) is not None
    assert runtime.get_run(paper["id"]) is None
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM agent_events WHERE run_id=?", (chat["id"],)
        ).fetchone()[0] == chat_event_count


def test_reset_clears_search_units_and_fts_but_preserves_chat(tmp_path):
    path = tmp_path / "sessions.db"
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE wiki_search_units (id TEXT, text TEXT, embedding BLOB);
            CREATE VIRTUAL TABLE wiki_search_units_fts USING fts5(
                text, content='wiki_search_units', content_rowid='rowid'
            );
            INSERT INTO wiki_search_units VALUES ('claim:old', 'quantization', X'0102');
            INSERT INTO wiki_search_units_fts(wiki_search_units_fts) VALUES ('rebuild');
            CREATE TABLE messages (id INTEGER, content TEXT);
            INSERT INTO messages VALUES (1, 'Keep my conversation');
        """)
    backup = tmp_path / "backup.db"
    _backup_sqlite(path, backup)
    deleted = _clear_db(path)
    assert deleted["wiki_search_units"] == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM wiki_search_units").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM wiki_search_units_fts WHERE wiki_search_units_fts MATCH 'quantization'").fetchone()[0] == 0
        assert conn.execute("SELECT content FROM messages").fetchone()[0] == "Keep my conversation"
    with sqlite3.connect(backup) as conn:
        assert conn.execute("SELECT count(*) FROM wiki_search_units").fetchone()[0] == 1

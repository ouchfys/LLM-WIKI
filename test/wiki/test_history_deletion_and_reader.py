import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.wiki import router
from backend.deps import get_session_store
from system.memory.session_store import SessionStore
from system.wiki.markdown_vault import readable_markdown


def test_delete_endpoint_physically_deletes_messages_and_summary(tmp_path):
    db = tmp_path / "sessions.db"
    store = SessionStore(str(db))
    sessions = [store.create_session("first"), store.create_session("second")]
    for session in sessions:
        store.save_message(session, "user", "question")
        store.save_message(session, "assistant", "answer")
        store.update_session_settings(session, {"context_summary": "old summary"})
    store.upsert_preference("topic", "retained preference")
    app = FastAPI()
    app.include_router(router, prefix="/api/wiki")
    app.dependency_overrides[get_session_store] = lambda: store
    client = TestClient(app)
    response = client.delete("/api/wiki/sessions")
    assert response.json() == {"ok": True, "deleted": 2, "memory_retained": True}
    # A new SQLite connection proves this is committed storage, not UI state.
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM user_profile").fetchone()[0] == 1
    assert client.get(f"/api/wiki/sessions/{sessions[0]}/messages").status_code == 404
    assert store.get_history(sessions[0]) == []


def test_late_response_cannot_restore_deleted_history(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.db"))
    deleted = store.create_session("delete this")
    keep = store.create_session("keep this")
    store.save_message(deleted, "user", "question")
    store.save_message(keep, "user", "retained")
    assert store.delete_session(deleted)
    assert store.save_message(deleted, "assistant", "late model response") == 0
    assert store.get_messages(deleted) == []
    assert len(store.get_messages(keep)) == 1


def test_legacy_audit_sections_do_not_enter_model_reading_context():
    text = "# QServe\n\n## Problem\nUseful knowledge\n"
    for heading in ("markdown status", "sources", "source_packet_ids", "claims", "affected claims", "compiler", "review status text"):
        text += f"\n## {heading}\nINTERNAL-ONLY\n"
    text += "\n## Results\nMeasured results\n<!-- wiki-system {\"claims\": []} -->"
    cleaned = readable_markdown(text)
    assert "Useful knowledge" in cleaned and "Measured results" in cleaned
    assert "INTERNAL-ONLY" not in cleaned and "wiki-system" not in cleaned

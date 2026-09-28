"""Crash boundaries and deletion ownership, using temporary databases only."""
from contextlib import closing
from io import BytesIO
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from system.agent_runtime import AgentRunStore
from system.agent_runtime.chat_recovery import ChatRecovery
from system.agent_runtime.control import RunCancelled
from system.agent_runtime.store import InvalidStateTransition
from system.conversation.session_store import SessionStore
from system.wiki.ingestion_jobs import IngestionJobStore
from system.wiki.wiki_chat import WikiChatService, AgentToolCall, AgentToolObservation


PLAN = "\n\n".join(f"# {heading}\n- Read the papers" for heading in
    ("Goal", "Sources / Objects", "Steps", "Done When", "Progress", "Blocked / Open Questions"))


class OfflineChat(WikiChatService):
    def __init__(self, sessions):
        super().__init__(object(), wiki_resolver=object(), session_store=sessions,
                         runtime=AgentRunStore(sessions.db_path), llm=object(), task_plans=sessions.task_plans)
        self.read_count = 0

    def _next_agent_tool_calls(self, **kwargs):
        if any(o.tool == "wiki_open" and o.status == "done" for o in kwargs["observations"]):
            return []
        return [AgentToolCall("wiki_open", {"card_ids": ["paper-a"]})]

    def _execute_agent_tool_call_impl(self, call, cards, web_results, resources, limit):
        self.read_count += 1
        card = {"id": "paper-a", "title": "Paper A", "_full_text": "Original evidence", "page_type": "PaperPage"}
        self._merge_cards(cards, [card])
        return AgentToolObservation(call.name, "", "done", "opened paper", [self._trace_card(card)])

    def _update_profile_from_message(self, *args):
        return []

    def _answer(self, message, cards, *args, **kwargs):
        assert cards[0]["_full_text"] == "Original evidence"
        return "Answer using recovered evidence"

    def _build_prompt(self, *args, **kwargs):
        return "Use recovered evidence"


def setup(tmp_path):
    sessions = SessionStore(str(tmp_path / "sessions.db"))
    sid = sessions.create_session()
    service = OfflineChat(sessions)
    control, recorder = service._prepare_chat("Read and compare papers", sid, 6)
    return sessions, sid, service, control, recorder


def crash(service, control):
    control.lease.stop()
    with closing(service.runtime._connect()) as conn:
        conn.execute("UPDATE agent_runs SET lease_expires_at='2000-01-01',lease_owner='dead-worker' WHERE id=?", (control.run_id,))
        conn.commit()


def test_resume_reconstructs_tools_without_rereading_and_saves_once(tmp_path):
    sessions, sid, service, control, _ = setup(tmp_path)
    with control.bind():
        service._execute_agent_tool_call(AgentToolCall("wiki_open", {"card_ids": ["paper-a"]}), [], [], [], 6)
    assert service.read_count == 1
    crash(service, control)
    fresh = OfflineChat(SessionStore(sessions.db_path))
    candidates = fresh.chat_recovery.list_for_session(sid)
    assert candidates[0]["completed_tools"] == 1
    resumed, recorder = fresh._prepare_chat("", sid, 6, resume_run_id=control.run_id)
    try:
        result = fresh._produce_turn(resumed, recorder, sid, 6, lambda event: None, streaming=False)
        assert "recovered" in result.answer
        assert fresh.read_count == 0
        assert len(sessions.get_messages(sid)) == 2
        assert fresh.chat_recovery.list_for_session(sid) == []
        with pytest.raises(ValueError, match="active, completed or cancelled"):
            fresh._prepare_chat("", sid, 6, resume_run_id=control.run_id)
    finally:
        resumed.lease.stop()


def test_unknown_write_requires_explicit_reconciliation_and_resume_is_exclusive(tmp_path):
    sessions, sid, service, control, _ = setup(tmp_path)
    call = AgentToolCall("local_shell", {"command": "write something"})
    with control.bind():
        service._journal_calls([call])
        service.chat_recovery.begin(control.run_id, call.recovery_id)
    crash(service, control)
    assert service.chat_recovery.list_for_session(sid)[0]["unknown_writes"][0]["id"] == call.recovery_id
    with pytest.raises(ValueError, match="unresolved write"):
        service._prepare_chat("", sid, 6, resume_run_id=control.run_id)
    service.chat_recovery.resolve(control.run_id, call.recovery_id, "confirmed_done")
    resumed, _ = service._prepare_chat("", sid, 6, resume_run_id=control.run_id)
    try:
        assert service._tool_signature(call) in resumed.loop_state["seen_signatures"]
        with pytest.raises(ValueError, match="active, completed or cancelled"):
            service._prepare_chat("", sid, 6, resume_run_id=control.run_id)
        with control.bind(), pytest.raises(RunCancelled):
            control.check()
    finally:
        resumed.lease.stop()


@pytest.mark.parametrize("interruption", ["pause", "failure"])
def test_continue_with_new_requirements_reuses_results_and_keeps_instructions_once(tmp_path, interruption):
    sessions, sid, service, control, _ = setup(tmp_path)
    original = control.message
    with control.bind():
        service._execute_agent_tool_call(AgentToolCall("wiki_open", {"card_ids": ["paper-a"]}), [], [], [], 6)
    service.runtime.enqueue_input(control.run_id, kind="interrupt", content="First restrict to RSI", input_id="earlier")
    if interruption == "pause":
        service.runtime.request_pause(control.run_id)
        with control.bind(), pytest.raises(RunCancelled):
            control.check()
    else:
        service.runtime.mark_failed(control.run_id, "API quota exhausted")
    control.lease.stop()
    fresh = OfflineChat(SessionStore(sessions.db_path))
    resumed, _ = fresh._prepare_chat("Only compare memory usage", sid, 6, resume_run_id=control.run_id)
    assert resumed.run_id == control.run_id
    assert resumed.message.index("First restrict to RSI") < resumed.message.index("Only compare memory usage")
    assert fresh.runtime.get_run(control.run_id)["context"]["request_message"] == original
    crash(fresh, resumed)
    again, recorder = fresh._prepare_chat("Use a table", sid, 6, resume_run_id=control.run_id)
    try:
        for requirement in (original, "First restrict to RSI", "Only compare memory usage", "Use a table"):
            assert again.message.count(requirement) == 1
        result = fresh._produce_turn(again, recorder, sid, 6, lambda event: None, streaming=False)
        assert "recovered evidence" in result.answer
        assert fresh.read_count == 0
        assert len(sessions.get_messages(sid)) == 2
        assert "Use a table" in sessions.get_messages(sid)[0]["content"]
    finally:
        again.lease.stop()


def test_pause_before_dispatch_does_not_create_unknown_write_and_delete_removes_recovery(tmp_path):
    sessions, sid, service, control, _ = setup(tmp_path)
    call = AgentToolCall("local_shell", {"command": "not started"})
    with control.bind():
        service._journal_calls([call])
    service.runtime.request_pause(control.run_id)
    with control.bind(), pytest.raises(RunCancelled):
        service.chat_recovery.begin(control.run_id, call.recovery_id)
    control.lease.stop()
    assert service.chat_recovery.list_for_session(sid)[0]["unknown_writes"] == []
    resumed, _ = service._prepare_chat("Change the scope", sid, 6, resume_run_id=control.run_id)
    try:
        assert not resumed.loop_state["tool_attempts"]
        assert service._tool_signature(call) not in resumed.loop_state["seen_signatures"]
        service.runtime.request_pause(control.run_id)
    finally:
        resumed.lease.stop()
    sessions.delete_session(sid)
    assert service.chat_recovery.list_for_session(sid) == []
    with pytest.raises(ValueError, match="no longer exists"):
        service._prepare_chat("Continue", sid, 6, resume_run_id=control.run_id)


def test_pause_after_dispatch_never_replays_unknown_write_or_accepts_unapplied_input(tmp_path):
    sessions, sid, service, control, _ = setup(tmp_path)
    call = AgentToolCall("local_shell", {"command": "write something"})
    with control.bind():
        service._journal_calls([call])
        service.chat_recovery.begin(control.run_id, call.recovery_id)
    service.runtime.request_pause(control.run_id)
    control.lease.stop()
    with pytest.raises(ValueError, match="unresolved write"):
        service._prepare_chat("A new requirement", sid, 6, resume_run_id=control.run_id)
    assert service.runtime.list_inputs(control.run_id) == []
    service.chat_recovery.resolve(control.run_id, call.recovery_id, "confirmed_done")
    resumed, _ = service._prepare_chat("A new requirement", sid, 6, resume_run_id=control.run_id)
    try:
        assert resumed.message.count("A new requirement") == 1
        assert service._tool_signature(call) in resumed.loop_state["seen_signatures"]
    finally:
        resumed.lease.stop()


@pytest.mark.parametrize("unified", [False, True])
def test_pause_preserves_ingestion_and_recovery_uses_actual_job_status(tmp_path, unified):
    sessions, sid, service, control, _ = setup(tmp_path)
    call = AgentToolCall("arxiv_import_paper", {"arxiv_id": "2405.04532"})
    if unified:
        call = AgentToolCall("arxiv", {"action": "import", "arxiv_id": "2405.04532"})
    with control.bind():
        service._journal_calls([call])
        assert service.chat_recovery.calls(control.run_id)[0]["read_only"] == 0
        service.chat_recovery.begin(control.run_id, call.recovery_id)
    jobs = IngestionJobStore(sessions.db_path)
    job = jobs.create_job(source_type="paper_pdf", source_uri="paper.pdf", job_id=call.recovery_id,
                          metadata={"owner_session_ids": [sid]})
    service.runtime.request_pause(control.run_id)
    control.lease.stop()
    assert jobs.get_job(job["id"])["status"] == "queued"
    jobs.update_job(job["id"], status="done", paper_card_id="paper-a")
    resumed, _ = service._prepare_chat("Compare only the completed papers", sid, 6, resume_run_id=control.run_id)
    try:
        recovered = resumed.loop_state["observations"][0].items[0]
        assert recovered["job_id"] == job["id"]
        assert recovered["status"] == "done"
        assert service._tool_signature(call) in resumed.loop_state["seen_signatures"]
    finally:
        resumed.lease.stop()


def test_pause_does_not_interrupt_atomic_answer_commit(tmp_path):
    _, _, service, control, _ = setup(tmp_path)
    try:
        assert service.runtime.close_chat_input(control.run_id)
        with pytest.raises(ValueError, match="already being saved"):
            service.runtime.request_pause(control.run_id)
        assert service.runtime.get_run(control.run_id)["current_state"] == "CHAT_RUNNING"
    finally:
        control.lease.stop()


def test_stop_button_api_acknowledges_pause_and_late_provider_cannot_overwrite_continuation(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.api.agent_runs import router
    from backend.deps import get_wiki_store

    sessions = SessionStore(str(tmp_path / "sessions.db"))
    sid = sessions.create_session()
    service = OfflineChat(sessions)
    entered, release, finished = Event(), Event(), Event()

    def blocked_stream(*args, **kwargs):
        entered.set()
        try:
            assert release.wait(30)
            yield "STALE provider answer"
        finally:
            finished.set()

    service._stream_llm = blocked_stream
    stream = service.chat_stream("Read and compare papers", session_id=sid)
    run_id = next(stream)["run_id"]
    chunks = []
    consumer = Thread(target=lambda: chunks.extend(stream))
    app = FastAPI()
    app.include_router(router, prefix="/agent-runs")
    app.dependency_overrides[get_wiki_store] = lambda: SimpleNamespace(db_path=sessions.db_path)
    consumer.start()
    try:
        assert entered.wait(5)
        with TestClient(app) as client:
            response = client.post(f"/agent-runs/{run_id}/pause")
            assert response.status_code == 200
            assert response.json()["current_state"] == "CHAT_INTERRUPTED"
            assert client.post(f"/agent-runs/{run_id}/pause").status_code == 200
        consumer.join(3)
        assert not consumer.is_alive()
        assert any(item.get("type") == "paused" for item in chunks)
        assert not any(item.get("type") == "token" for item in chunks)
        assert service.chat_recovery.list_for_session(sid)[0]["can_resume"]
        fresh = OfflineChat(SessionStore(sessions.db_path))
        resumed, recorder = fresh._prepare_chat("Use a table", sid, 6, resume_run_id=run_id)
        try:
            result = fresh._produce_turn(resumed, recorder, sid, 6, lambda event: None, streaming=False)
            assert "recovered evidence" in result.answer
            assert fresh.read_count == 0
        finally:
            resumed.lease.stop()
        release.set()
        assert finished.wait(3)
        assert fresh.runtime.get_run(run_id)["current_state"] == "COMPLETED"
        assert len(sessions.get_messages(sid)) == 2
        assert all("STALE" not in item["content"] for item in sessions.get_messages(sid))
    finally:
        release.set()
        consumer.join(3)


def test_missing_read_output_is_retryable_and_deleted_runs_never_resume(tmp_path):
    sessions, sid, service, control, _ = setup(tmp_path)
    call = AgentToolCall("wiki_open", {"card_ids": ["paper-a"]})
    with control.bind():
        service._journal_calls([call])
        service.chat_recovery.begin(control.run_id, call.recovery_id)
    crash(service, control)
    resumed, recorder = service._prepare_chat("", sid, 6, resume_run_id=control.run_id)
    try:
        service._produce_turn(resumed, recorder, sid, 6, lambda event: None, streaming=False)
        assert service.read_count == 1
    finally:
        resumed.lease.stop()
    sessions.delete_session(sid)
    with pytest.raises(ValueError, match="no longer exists"):
        service._prepare_chat("", sid, 6, resume_run_id=control.run_id)


def test_saved_answer_before_final_checkpoint_is_not_regenerated(tmp_path):
    sessions, sid, service, control, _ = setup(tmp_path)
    with control.bind():
        sessions.save_chat_answer(sid, control.run_id, "request", "answer", {}, "title")
    crash(service, control)
    with pytest.raises(ValueError, match="already saved"):
        service._prepare_chat("", sid, 6, resume_run_id=control.run_id)
    assert len(sessions.get_messages(sid)) == 2
    assert service.runtime.get_run(control.run_id)["current_state"] == "COMPLETED"


def test_delete_detaches_shared_plans_cancels_only_exclusive_jobs_and_blocks_late_writes(tmp_path):
    sessions, sid, service, control, _ = setup(tmp_path)
    other = sessions.create_session()
    exclusive = sessions.task_plans.write(PLAN, project_id="p", session_id=sid)
    shared = sessions.task_plans.write(PLAN, project_id="p", session_id=sid, create_new=True)
    sessions.task_plans.attach(task_id=shared["task_id"], project_id="p", session_id=other)
    jobs = IngestionJobStore(sessions.db_path)
    owned = jobs.create_job(source_type="paper_pdf", source_uri="one.pdf", metadata={"owner_session_ids": [sid]})
    common = jobs.create_job(source_type="paper_pdf", source_uri="two.pdf", metadata={"owner_session_ids": [sid, other]})
    done = jobs.create_job(source_type="paper_pdf", source_uri="three.pdf", metadata={"owner_session_ids": [sid]})
    jobs.update_job(done["id"], status="done", paper_card_id="committed-page")
    failed = jobs.create_job(source_type="paper_pdf", source_uri="failed.pdf", metadata={"owner_session_ids": [sid]})
    jobs.update_job(failed["id"], status="failed")
    failed_run = service.runtime.create_run(run_type="paper_ingestion", ingestion_job_id=failed["id"])
    service.runtime.mark_failed(failed_run["id"], "temporary failure")
    run = service.runtime.create_run(run_type="paper_ingestion", ingestion_job_id=owned["id"])
    control.lease.stop()
    assert sessions.delete_session(sid)
    with pytest.raises(ValueError, match="not found"):
        sessions.task_plans.read(task_id=exclusive["task_id"], project_id="p", session_id=sid)
    assert sessions.task_plans.read(task_id=shared["task_id"], project_id="p", session_id=other)["session_ids"] == [other]
    assert jobs.get_job(owned["id"])["status"] == "cancelled"
    assert service.runtime.get_run(run["id"])["current_state"] == "CANCELLED"
    assert jobs.get_job(common["id"])["status"] == "queued"
    assert jobs.get_job(done["id"])["paper_card_id"] == "committed-page"
    assert jobs.get_job(failed["id"])["status"] == "cancelled"
    with pytest.raises(InvalidStateTransition, match="CANCELLED"):
        service.runtime.restart_run(failed_run["id"])
    jobs.update_job(owned["id"], status="running")
    assert jobs.get_job(owned["id"])["status"] == "cancelled"
    with pytest.raises(RunCancelled):
        sessions.task_plans.write(PLAN, project_id="p", session_id=sid)
    with pytest.raises(RunCancelled):
        sessions.save_chat_answer(sid, control.run_id, "late", "late", {}, "late")
    sessions.delete_all_sessions()
    with pytest.raises(ValueError, match="not found"):
        sessions.task_plans.read(task_id=shared["task_id"], project_id="p", session_id=other)


def test_lost_ingestion_http_response_is_reconciled_without_new_submission(tmp_path, monkeypatch):
    from backend.api import papers
    from fastapi import UploadFile
    sessions, sid, service, control, _ = setup(tmp_path)
    call = AgentToolCall("arxiv_import_paper", {"arxiv_id": "2405.04532"})
    with control.bind():
        service._journal_calls([call])
        service.chat_recovery.begin(control.run_id, call.recovery_id)
    monkeypatch.setattr(papers, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(papers, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(papers, "_existing_pdf_ingestion", lambda *args: None)
    monkeypatch.setattr(papers, "submit_agent_task", lambda **kwargs: {"status": "scheduled"})
    kwargs = dict(local_path="", source_url="https://arxiv.org/abs/2405.04532", pipeline="wiki_compile", approval_mode="risk",
                  owner_session_id=sid, parent_run_id=control.run_id, request_id=call.recovery_id, store=SimpleNamespace(db_path=sessions.db_path))
    first = papers.create_ingestion_job(file=UploadFile(filename="a.pdf", file=BytesIO(b"%PDF fake")), **kwargs)
    second = papers.create_ingestion_job(file=UploadFile(filename="a.pdf", file=BytesIO(b"%PDF fake")), **kwargs)
    assert first["job_id"] == second["job_id"] == call.recovery_id
    crash(service, control)
    resumed, _ = service._prepare_chat("", sid, 6, resume_run_id=control.run_id)
    try:
        assert resumed.loop_state["observations"][0].items[0]["job_id"] == first["job_id"]
        assert service._tool_signature(call) in resumed.loop_state["seen_signatures"]
    finally:
        resumed.lease.stop()
    sessions.delete_session(sid)
    assert IngestionJobStore(sessions.db_path).get_job(first["job_id"])["status"] == "cancelled"


def test_resume_endpoint_and_scope_checks(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.api.wiki import router
    from backend.deps import get_wiki_chat, get_session_store
    sessions, sid, service, control, _ = setup(tmp_path)
    with control.bind():
        service._execute_agent_tool_call(AgentToolCall("wiki_open", {"card_ids": ["paper-a"]}), [], [], [], 6)
    crash(service, control)
    app = FastAPI()
    app.include_router(router, prefix="/wiki")
    app.dependency_overrides[get_wiki_chat] = lambda: service
    app.dependency_overrides[get_session_store] = lambda: sessions
    with TestClient(app) as client:
        response = client.get(f"/wiki/sessions/{sid}/recovery")
        assert response.json()["items"][0]["run_id"] == control.run_id
        wrong = sessions.create_session()
        assert client.post(f"/wiki/sessions/{wrong}/recovery/{control.run_id}/missing", json={"decision": "confirmed_done"}).status_code == 404
        streamed = client.post("/wiki/chat", json={"session_id": sid, "resume_run_id": control.run_id, "stream": True})
        assert "Answer using recovered evidence" in streamed.text
        assert service.runtime.get_run(control.run_id)["current_state"] == "COMPLETED"
        assert service.read_count == 1


def test_consumed_interrupt_survives_crash_before_in_memory_apply(tmp_path):
    sessions, sid, service, control, _ = setup(tmp_path)
    service.runtime.enqueue_input(control.run_id, kind="interrupt", content="Only compare memory usage", input_id="steering")
    service.runtime.take_interrupts(control.run_id)
    crash(service, control)
    resumed, _ = service._prepare_chat("", sid, 6, resume_run_id=control.run_id)
    try:
        assert resumed.message.count("Only compare memory usage") == 1
        with resumed.bind():
            service.runtime.save_chat_cursor(resumed.run_id, resumed.message, 1)
        crash(service, resumed)
        again, _ = service._prepare_chat("", sid, 6, resume_run_id=control.run_id)
        try:
            assert again.message.count("Only compare memory usage") == 1
        finally:
            again.lease.stop()
    finally:
        resumed.lease.stop()


def test_clear_session_prevents_old_worker_recreating_plan(tmp_path):
    sessions, sid, service, control, _ = setup(tmp_path)
    with control.bind():
        plan = sessions.task_plans.write(PLAN, project_id="p", session_id=sid)
    sessions.clear_session(sid)
    try:
        assert sessions.get_session(sid)
        with pytest.raises(ValueError, match="not found"):
            sessions.task_plans.read(task_id=plan["task_id"], project_id="p", session_id=sid)
        with control.bind(), pytest.raises(RunCancelled):
            sessions.task_plans.write(PLAN, project_id="p", session_id=sid)
    finally:
        control.lease.stop()


def test_reused_call_object_records_distinct_completed_attempts(tmp_path):
    sessions, sid, service, control, _ = setup(tmp_path)
    call = AgentToolCall("wiki_open", {"card_ids": ["paper-a"]})
    try:
        with control.bind():
            service._execute_agent_tool_call(call, [], [], [], 6)
            service._execute_agent_tool_call(call, [], [], [], 6)
        calls = service.chat_recovery.calls(control.run_id)
        assert len(calls) == 2
        assert all(c["status"] == "finished" for c in calls)
        assert calls[0]["result_id"] != calls[1]["result_id"]
    finally:
        control.lease.stop()


def test_disconnect_during_answer_commit_keeps_worker_lease(tmp_path, monkeypatch):
    sessions = SessionStore(str(tmp_path / "sessions.db"))
    sid = sessions.create_session()
    service = OfflineChat(sessions)
    saving, allow_save, completed = Event(), Event(), Event()
    original_save = service._save_turn
    original_complete = service._complete_chat_runtime

    def save(*args, **kwargs):
        saving.set()
        assert allow_save.wait(10)
        return original_save(*args, **kwargs)

    def complete(*args, **kwargs):
        original_complete(*args, **kwargs)
        completed.set()

    monkeypatch.setattr(service, "_save_turn", save)
    monkeypatch.setattr(service, "_complete_chat_runtime", complete)
    stream = service.chat_stream("Read papers", sid)
    run_id = next(stream)["run_id"]
    try:
        for _ in stream:
            if saving.wait(0.1):
                break
        assert saving.is_set()
        stream.close()
        assert service.runtime.get_run(run_id)["lease_owner"]
        allow_save.set()
        assert completed.wait(10)
        assert service.runtime.get_run(run_id)["current_state"] == "COMPLETED"
        assert len(sessions.get_messages(sid)) == 2
    finally:
        allow_save.set()
        stream.close()

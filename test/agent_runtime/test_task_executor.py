from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

from backend import task_executor as task_executor_module
from backend.task_executor import BoundedTaskExecutor, submit_agent_task
from system.agent_runtime import AgentRunStore
from system.wiki.ingestion_jobs import IngestionJobStore


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached before timeout")


def test_bounded_executor_limits_running_and_queued_work() -> None:
    executor = BoundedTaskExecutor(max_workers=1, queue_capacity=1, thread_name_prefix="test-bound")
    first_started = threading.Event()
    release_first = threading.Event()
    completed: list[str] = []

    def blocking_task() -> None:
        first_started.set()
        assert release_first.wait(2)
        completed.append("first")

    def queued_task() -> None:
        completed.append("second")

    try:
        assert executor.submit("first", blocking_task)["status"] == "scheduled"
        assert first_started.wait(1)
        assert executor.submit("second", queued_task)["status"] == "scheduled"

        rejected = executor.submit("third", queued_task)
        assert rejected["accepted"] is False
        assert rejected["status"] == "capacity"
        assert rejected["capacity_rejections"] == 1

        duplicate = executor.submit("first", blocking_task)
        assert duplicate["accepted"] is True
        assert duplicate["status"] == "duplicate"

        snapshot = executor.snapshot()
        assert snapshot["running"] == 1
        assert snapshot["queued"] == 1
        assert snapshot["available"] == 0

        release_first.set()
        _wait_until(lambda: executor.snapshot()["completed_count"] == 2)
        assert completed == ["first", "second"]
        assert executor.snapshot()["available"] == 2
    finally:
        release_first.set()
        executor.shutdown(wait=True)


def test_agent_task_backpressure_is_persisted_for_recovery(monkeypatch) -> None:
    executor = BoundedTaskExecutor(max_workers=1, queue_capacity=1, thread_name_prefix="test-agent")
    monkeypatch.setattr(task_executor_module, "get_task_executor", lambda: executor)
    first_started = threading.Event()
    release_first = threading.Event()

    def blocking_task() -> None:
        first_started.set()
        assert release_first.wait(2)

    def no_op_task() -> None:
        return

    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db_path = str(Path(tmp) / "executor.db")
            jobs = IngestionJobStore(db_path=db_path)
            runtime = AgentRunStore(db_path=db_path)

            def create_durable_task() -> tuple[str, str]:
                job = jobs.create_job(source_type="paper_pdf", source_uri="paper.pdf")
                run = runtime.create_run(
                    run_type="paper_ingestion",
                    source_uri="paper.pdf",
                    ingestion_job_id=str(job["id"]),
                    context={"job_id": job["id"], "pdf_path": "paper.pdf"},
                )
                return str(run["id"]), str(job["id"])

            first_run, first_job = create_durable_task()
            second_run, second_job = create_durable_task()
            third_run, third_job = create_durable_task()

            first = submit_agent_task(
                run_id=first_run,
                db_path=db_path,
                job_id=first_job,
                task_name="paper_ingestion",
                target=blocking_task,
                kwargs={},
            )
            assert first["status"] == "scheduled"
            assert first_started.wait(1)
            second = submit_agent_task(
                run_id=second_run,
                db_path=db_path,
                job_id=second_job,
                task_name="paper_ingestion",
                target=no_op_task,
                kwargs={},
            )
            assert second["status"] == "scheduled"
            third = submit_agent_task(
                run_id=third_run,
                db_path=db_path,
                job_id=third_job,
                task_name="paper_ingestion",
                target=no_op_task,
                kwargs={},
            )

            assert third["status"] == "capacity"
            assert runtime.get_run(third_run)["current_state"] == "QUEUED"
            assert jobs.get_job(third_job)["stage"] == "queued_capacity"
            assert any(
                event["event_type"] == "dispatch.deferred"
                for event in runtime.list_events(third_run)
            )

            first_events = runtime.list_events(first_run)
            event_types = [event["event_type"] for event in first_events]
            assert event_types.index("dispatch.scheduled") < event_types.index("dispatch.started")

            release_first.set()
            _wait_until(lambda: executor.snapshot()["completed_count"] == 2)
            second_job_data = jobs.get_job(second_job) or {}
            assert second_job_data["metadata"]["queue_wait_ms"] >= 0
    finally:
        release_first.set()
        executor.shutdown(wait=True)

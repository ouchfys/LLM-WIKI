from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from system.agent_runtime import AgentRunStore
from system.core.config import AGENT_TASK_MAX_WORKERS, AGENT_TASK_QUEUE_CAPACITY
from system.wiki.ingestion_jobs import IngestionJobStore


TaskCallable = Callable[..., Any]
TaskCallback = Callable[[float], None]
ScheduleCallback = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class _Task:
    task_id: str
    fn: TaskCallable
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    submitted_at: float
    on_start: TaskCallback | None = None


class BoundedTaskExecutor:
    """Small in-process worker pool with an explicit total capacity.

    The executor deliberately remains a local transport. Durable task state is
    owned by AgentRunStore, so work rejected by backpressure remains QUEUED and
    can be submitted again by the recovery scanner.
    """

    def __init__(
        self,
        *,
        max_workers: int,
        queue_capacity: int,
        thread_name_prefix: str = "agent-task",
    ) -> None:
        self.max_workers = max(1, int(max_workers))
        self.queue_capacity = max(0, int(queue_capacity))
        self.total_capacity = self.max_workers + self.queue_capacity
        self._slots = threading.BoundedSemaphore(self.total_capacity)
        self._queue: queue.Queue[_Task | None] = queue.Queue()
        self._lock = threading.RLock()
        self._scheduled: set[str] = set()
        self._running: set[str] = set()
        self._accepting = True
        self._submitted_count = 0
        self._completed_count = 0
        self._failed_count = 0
        self._capacity_rejections = 0
        self._threads = [
            threading.Thread(
                target=self._worker,
                name=f"{thread_name_prefix}-{index + 1}",
                daemon=True,
            )
            for index in range(self.max_workers)
        ]
        for thread in self._threads:
            thread.start()

    def submit(
        self,
        task_id: str,
        fn: TaskCallable,
        /,
        *args: Any,
        on_scheduled: ScheduleCallback | None = None,
        on_start: TaskCallback | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        normalized_id = str(task_id or "").strip()
        if not normalized_id:
            raise ValueError("task_id cannot be empty")

        with self._lock:
            if normalized_id in self._scheduled:
                return {"accepted": True, "status": "duplicate", **self.snapshot()}
            if not self._accepting:
                return {"accepted": False, "status": "shutdown", **self.snapshot()}

        if not self._slots.acquire(blocking=False):
            with self._lock:
                self._capacity_rejections += 1
            return {"accepted": False, "status": "capacity", **self.snapshot()}

        with self._lock:
            # A concurrent submit can only win while this caller was acquiring a
            # slot. Give the unused reservation back instead of scheduling twice.
            if normalized_id in self._scheduled:
                self._slots.release()
                return {"accepted": True, "status": "duplicate", **self.snapshot()}
            if not self._accepting:
                self._slots.release()
                return {"accepted": False, "status": "shutdown", **self.snapshot()}
            self._scheduled.add(normalized_id)
            self._submitted_count += 1

        if on_scheduled is not None:
            try:
                on_scheduled(self.snapshot())
            except Exception as exc:
                print(f"[task-executor] schedule callback failed for {normalized_id}: {exc}")

        self._queue.put(
            _Task(
                task_id=normalized_id,
                fn=fn,
                args=args,
                kwargs=kwargs,
                submitted_at=time.monotonic(),
                on_start=on_start,
            )
        )
        return {"accepted": True, "status": "scheduled", **self.snapshot()}

    def is_scheduled(self, task_id: str) -> bool:
        with self._lock:
            return str(task_id) in self._scheduled

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            running = len(self._running)
            scheduled = len(self._scheduled)
            return {
                "max_workers": self.max_workers,
                "queue_capacity": self.queue_capacity,
                "total_capacity": self.total_capacity,
                "running": running,
                "queued": max(0, scheduled - running),
                "available": max(0, self.total_capacity - scheduled),
                "submitted_count": self._submitted_count,
                "completed_count": self._completed_count,
                "failed_count": self._failed_count,
                "capacity_rejections": self._capacity_rejections,
            }

    def shutdown(self, *, wait: bool = False) -> None:
        with self._lock:
            if not self._accepting:
                return
            self._accepting = False
        for _ in self._threads:
            self._queue.put(None)
        if wait:
            for thread in self._threads:
                thread.join()

    def _worker(self) -> None:
        while True:
            task = self._queue.get()
            if task is None:
                self._queue.task_done()
                return
            with self._lock:
                self._running.add(task.task_id)
            queue_wait_ms = max(0.0, (time.monotonic() - task.submitted_at) * 1000)
            try:
                if task.on_start is not None:
                    try:
                        task.on_start(queue_wait_ms)
                    except Exception as exc:
                        print(f"[task-executor] start callback failed for {task.task_id}: {exc}")
                task.fn(*task.args, **task.kwargs)
            except Exception as exc:
                with self._lock:
                    self._failed_count += 1
                print(f"[task-executor] task {task.task_id} failed: {exc}")
            else:
                with self._lock:
                    self._completed_count += 1
            finally:
                with self._lock:
                    self._running.discard(task.task_id)
                    self._scheduled.discard(task.task_id)
                self._slots.release()
                self._queue.task_done()


_EXECUTOR: BoundedTaskExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()


def get_task_executor() -> BoundedTaskExecutor:
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = BoundedTaskExecutor(
                max_workers=AGENT_TASK_MAX_WORKERS,
                queue_capacity=AGENT_TASK_QUEUE_CAPACITY,
            )
        return _EXECUTOR


def task_executor_snapshot() -> dict[str, Any]:
    return get_task_executor().snapshot()


def shutdown_task_executor(*, wait: bool = False) -> None:
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        executor = _EXECUTOR
        _EXECUTOR = None
    if executor is not None:
        executor.shutdown(wait=wait)


def submit_agent_task(
    *,
    run_id: str,
    db_path: str,
    job_id: str,
    task_name: str,
    target: TaskCallable,
    kwargs: dict[str, Any],
    record_deferred: bool = True,
) -> dict[str, Any]:
    """Submit durable Agent work and expose queue wait through existing trace data."""

    def on_start(queue_wait_ms: float) -> None:
        runtime = AgentRunStore(db_path=db_path)
        runtime.append_event(
            run_id,
            event_type="dispatch.started",
            node_name=task_name,
            status="running",
            duration_ms=queue_wait_ms,
            output_data={"queue_wait_ms": round(queue_wait_ms, 2)},
        )
        if job_id:
            IngestionJobStore(db_path=db_path).merge_metadata(
                job_id,
                {
                    "executor_status": "running",
                    "queue_wait_ms": round(queue_wait_ms, 2),
                },
            )

    def on_scheduled(snapshot: dict[str, Any]) -> None:
        runtime = AgentRunStore(db_path=db_path)
        runtime.append_event(
            run_id,
            event_type="dispatch.scheduled",
            node_name=task_name,
            status="queued",
            output_data={
                "queued": snapshot.get("queued", 0),
                "running": snapshot.get("running", 0),
                "total_capacity": snapshot.get("total_capacity", 0),
            },
        )
        if job_id:
            IngestionJobStore(db_path=db_path).merge_metadata(
                job_id,
                {
                    "executor_status": "scheduled",
                    "executor_queue_depth": snapshot.get("queued", 0),
                },
            )

    executor = get_task_executor()
    result = executor.submit(
        run_id,
        target,
        on_scheduled=on_scheduled,
        on_start=on_start,
        **kwargs,
    )
    runtime = AgentRunStore(db_path=db_path)
    status = str(result.get("status") or "")
    if status in {"capacity", "shutdown"}:
        if record_deferred:
            runtime.append_event(
                run_id,
                event_type="dispatch.deferred",
                node_name=task_name,
                status=status,
                output_data={
                    "queued": result.get("queued", 0),
                    "running": result.get("running", 0),
                    "total_capacity": result.get("total_capacity", 0),
                },
            )
        if job_id:
            jobs = IngestionJobStore(db_path=db_path)
            jobs.update_job(job_id, status="queued", stage="queued_capacity", progress=0.0)
            jobs.merge_metadata(job_id, {"executor_status": "deferred"})
    return result

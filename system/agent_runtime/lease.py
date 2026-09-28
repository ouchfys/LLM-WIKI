from __future__ import annotations

import threading
import uuid

from system.agent_runtime.store import AgentRunStore


class RunLeaseBusy(RuntimeError):
    pass


class AgentRunLease:
    """Renewable SQLite worker lease; expiry makes crashed runs recoverable."""

    def __init__(
        self,
        store: AgentRunStore,
        run_id: str,
        *,
        owner: str = "",
        ttl_seconds: int = 90,
        heartbeat_seconds: int = 15,
    ):
        self.store = store
        self.run_id = run_id
        self.owner = owner or f"worker-{uuid.uuid4()}"
        self.ttl_seconds = max(15, int(ttl_seconds))
        self.heartbeat_seconds = max(3, min(int(heartbeat_seconds), self.ttl_seconds // 2))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.acquired = False

    def start(self) -> bool:
        if self.acquired:
            return True
        self.acquired = self.store.acquire_lease(
            self.run_id, self.owner, ttl_seconds=self.ttl_seconds
        )
        if not self.acquired:
            return False
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"agent-heartbeat-{self.run_id[:8]}",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(1, self.heartbeat_seconds + 1))
        if self.owner:
            # A stopped heartbeat may have cleared acquired before the worker
            # exits. Release only our own lease; never touch a replacement worker.
            self.store.release_lease(self.run_id, self.owner)
        self.acquired = False

    def check(self):
        from system.agent_runtime.control import RunCancelled
        run = self.store.get_run(self.run_id) or {}
        if (not self.acquired or run.get("lease_owner") != self.owner
                or run.get("lease_expires_at", "") <= self.store.now_iso()
                or run.get("cancel_requested") or run.get("current_state") in {"CANCELLED", "CHAT_INTERRUPTED"}):
            raise RunCancelled("Task cancelled or execution lease lost")

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            if not self.store.heartbeat(
                self.run_id, self.owner, ttl_seconds=self.ttl_seconds
            ):
                self.acquired = False
                return

    def __enter__(self) -> "AgentRunLease":
        if not self.start():
            raise RunLeaseBusy(f"Agent run {self.run_id} is owned by another live worker.")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.stop()

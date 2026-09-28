"""Short local commit/delete critical sections; never held across model calls."""
from functools import wraps
from pathlib import Path
from threading import Lock, RLock

_locks = {}
_guard = Lock()


def serialized_write(path_for):
    def decorate(function):
        @wraps(function)
        def wrapped(self, *args, **kwargs):
            key = str(Path(path_for(self)).resolve()).casefold()
            with _guard:
                lock = _locks.setdefault(key, RLock())
            with lock:
                return function(self, *args, **kwargs)
        return wrapped
    return decorate


def check_commit_owner():
    from system.agent_runtime.control import check_run_control, RunCancelled
    from system.agent_runtime.tracing import get_current_trace
    check_run_control()
    trace = get_current_trace()
    if trace:
        run = trace.store.get_run(trace.run_id) or {}
        if not run or run.get("cancel_requested") or run.get("current_state") == "CANCELLED":
            raise RunCancelled("Task was cancelled before knowledge commit")
        owner = getattr(trace, "lease_owner", "")
        if owner and (run.get("lease_owner") != owner or run.get("lease_expires_at", "") <= trace.store.now_iso()):
            raise RunCancelled("Execution lease lost before knowledge commit")

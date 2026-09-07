"""Small cooperative control channel, independent of model/provider internals."""
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event
from time import monotonic


class RunCancelled(BaseException):
    """Control flow, deliberately not caught by model/tool Exception fallbacks."""
    trace_status = "cancelled"


class RunInterrupted(BaseException):
    trace_status = "interrupted"

    def __init__(self, items):
        self.items = items
        super().__init__("User supplied additional instructions")


_control: ContextVar["RunControl | None"] = ContextVar("agent_run_control", default=None)


class RunControl:
    def __init__(self, store, run_id, message):
        self.store = store
        self.run_id = run_id
        self.message = message
        self.interruptions = []
        self.loop_state = {}
        self.abandoned = Event()
        self.last_check = 0.0

    @contextmanager
    def bind(self):
        token = _control.set(self)
        try:
            yield self
        finally:
            _control.reset(token)

    def check(self, *, force=True):
        if self.abandoned.is_set():
            raise RunCancelled("Chat connection closed")
        if not self.store or not self.run_id:
            return
        if not force and monotonic() - self.last_check < 0.1:
            return
        self.last_check = monotonic()
        run = self.store.get_run(self.run_id) or {}
        if run.get("cancel_requested") or run.get("current_state") == "CANCELLED":
            raise RunCancelled("Cancelled by user")
        items = self.store.take_interrupts(self.run_id)
        if items:
            raise RunInterrupted(items)

    def apply(self, interruption):
        self.interruptions.extend(interruption.items)
        for item in interruption.items:
            self.message += f"\n\n[用户中途补充，以此为最新要求]\n{item['content']}"
            self.store.append_event(
                self.run_id, event_type="input.interrupt.consumed", status="consumed",
                input_data={"input_id": item["id"], "chars": len(item["content"])},
            )


def get_run_control():
    return _control.get()


def check_run_control(*, force=True):
    control = _control.get()
    if control:
        control.check(force=force)

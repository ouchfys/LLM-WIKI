from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from system.agent_runtime.store import AgentRunStore


_current_span: ContextVar[str] = ContextVar("agent_current_span", default="")
_current_recorder: ContextVar["TraceRecorder | None"] = ContextVar(
    "agent_trace_recorder", default=None
)


class TraceRecorder:
    def __init__(self, store: AgentRunStore, run_id: str):
        self.store = store
        self.run_id = run_id
        self.trace_id = run_id

    @contextmanager
    def bind(self) -> Iterator["TraceRecorder"]:
        """Make this recorder available to nested model/tool helpers."""
        token = _current_recorder.set(self)
        try:
            yield self
        finally:
            _current_recorder.reset(token)

    @contextmanager
    def span(
        self,
        name: str,
        *,
        kind: str = "node",
        model: str = "",
        tool_name: str = "",
        input_data: Any = None,
    ) -> Iterator[dict[str, Any]]:
        span_id = str(uuid.uuid4())
        parent = _current_span.get()
        token = _current_span.set(span_id)
        started = time.perf_counter()
        state: dict[str, Any] = {"output": {}, "usage": {}}
        self.store.append_event(
            self.run_id,
            event_type=f"{kind}.started",
            node_name=name,
            status="running",
            trace_id=self.trace_id,
            span_id=span_id,
            parent_span_id=parent,
            model=model,
            tool_name=tool_name,
            input_data=input_data,
        )
        try:
            yield state
        except Exception as exc:
            self.store.append_event(
                self.run_id,
                event_type=f"{kind}.failed",
                node_name=name,
                status="failed",
                trace_id=self.trace_id,
                span_id=span_id,
                parent_span_id=parent,
                model=model,
                tool_name=tool_name,
                input_data=input_data,
                output_data=state.get("output"),
                usage=state.get("usage"),
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                error=str(exc),
            )
            raise
        else:
            self.store.append_event(
                self.run_id,
                event_type=f"{kind}.completed",
                node_name=name,
                status="completed",
                trace_id=self.trace_id,
                span_id=span_id,
                parent_span_id=parent,
                model=model,
                tool_name=tool_name,
                input_data=input_data,
                output_data=state.get("output"),
                usage=state.get("usage"),
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        finally:
            _current_span.reset(token)

    def event(self, event_type: str, *, name: str = "", status: str = "", data: Any = None) -> dict[str, Any]:
        return self.store.append_event(
            self.run_id,
            event_type=event_type,
            node_name=name,
            status=status,
            trace_id=self.trace_id,
            parent_span_id=_current_span.get(),
            output_data=data,
        )


def get_current_trace() -> TraceRecorder | None:
    return _current_recorder.get()

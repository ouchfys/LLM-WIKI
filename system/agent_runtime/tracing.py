from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from math import ceil
from typing import Any, Iterator

from system.agent_runtime.store import AgentRunStore


_current_span: ContextVar[str] = ContextVar("agent_current_span", default="")
_current_recorder: ContextVar["TraceRecorder | None"] = ContextVar(
    "agent_trace_recorder", default=None
)
_current_span_state: ContextVar["dict[str, Any] | None"] = ContextVar(
    "agent_trace_span_state", default=None
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
        state = self.start_span(
            name,
            kind=kind,
            model=model,
            tool_name=tool_name,
            input_data=input_data,
        )
        try:
            with self.activate_span(state):
                yield state
        except BaseException as exc:
            self.finish_span(state, status=getattr(exc, "trace_status", "failed"), error=str(exc) or exc.__class__.__name__)
            raise
        else:
            status = str(state.get("status") or "completed").lower()
            self.finish_span(
                state,
                status="failed" if status in {"error", "failed"} else "completed",
                error=str(state.get("error") or ""),
            )

    def start_span(
        self,
        name: str,
        *,
        kind: str = "node",
        model: str = "",
        tool_name: str = "",
        input_data: Any = None,
        parent_span_id: str = "",
    ) -> dict[str, Any]:
        """Start a span that may outlive one Python context (for SSE streams)."""
        span_id = str(uuid.uuid4())
        parent = parent_span_id or _current_span.get()
        state: dict[str, Any] = {
            "name": name,
            "kind": kind,
            "model": model,
            "tool_name": tool_name,
            "input": input_data,
            "output": {},
            "usage": {},
            "status": "running",
            "error": "",
            "retry_count": 0,
            "span_id": span_id,
            "parent_span_id": parent,
            "started_perf": time.perf_counter(),
            "finished": False,
        }
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
        return state

    @contextmanager
    def activate_span(self, state: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Temporarily bind a detached span while advancing streaming work."""
        recorder_token = _current_recorder.set(self)
        span_token = _current_span.set(str(state.get("span_id") or ""))
        state_token = _current_span_state.set(state)
        try:
            yield state
        finally:
            _current_span_state.reset(state_token)
            _current_span.reset(span_token)
            _current_recorder.reset(recorder_token)

    def finish_span(
        self,
        state: dict[str, Any],
        *,
        status: str = "completed",
        error: str = "",
    ) -> dict[str, Any]:
        if state.get("finished"):
            return {}
        state["finished"] = True
        failed = str(status).lower() in {"error", "failed"}
        outcome = str(status).lower() if status in {"cancelled", "interrupted"} else ("failed" if failed else "completed")
        state["status"] = outcome
        state["error"] = error or str(state.get("error") or "")
        return self.store.append_event(
            self.run_id,
            event_type=f"{state.get('kind', 'node')}.{outcome}",
            node_name=str(state.get("name") or ""),
            status=outcome,
            trace_id=self.trace_id,
            span_id=str(state.get("span_id") or ""),
            parent_span_id=str(state.get("parent_span_id") or ""),
            model=str(state.get("model") or ""),
            tool_name=str(state.get("tool_name") or ""),
            input_data=state.get("input"),
            output_data=state.get("output"),
            usage=state.get("usage"),
            duration_ms=round(
                (time.perf_counter() - float(state.get("started_perf") or time.perf_counter())) * 1000,
                2,
            ),
            error=state["error"],
        )

    def event(
        self,
        event_type: str,
        *,
        name: str = "",
        status: str = "",
        data: Any = None,
        model: str = "",
        tool_name: str = "",
        error: str = "",
    ) -> dict[str, Any]:
        return self.store.append_event(
            self.run_id,
            event_type=event_type,
            node_name=name,
            status=status,
            trace_id=self.trace_id,
            parent_span_id=_current_span.get(),
            output_data=data,
            model=model,
            tool_name=tool_name,
            error=error,
        )


def get_current_trace() -> TraceRecorder | None:
    return _current_recorder.get()


def estimate_token_usage(prompt: Any, completion: Any = "") -> dict[str, Any]:
    """Portable fallback when an OpenAI-compatible provider omits usage."""
    prompt_tokens = _estimate_tokens(_usage_text(prompt))
    completion_tokens = _estimate_tokens(_usage_text(completion))
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "estimated": True,
    }


def set_current_span_usage(usage: Any) -> None:
    """Let a provider client attach its exact usage to the active model span."""
    state = _current_span_state.get()
    if state is not None and isinstance(usage, dict) and usage:
        state["usage"] = dict(usage)
        state["usage"]["estimated"] = False


def record_current_retry(
    *,
    kind: str = "model",
    name: str = "",
    model: str = "",
    tool_name: str = "",
    attempt: int,
    max_attempts: int,
    error: str,
    delay_seconds: float = 0.0,
) -> None:
    """Persist retry attempts under the active span and update its counters."""
    state = _current_span_state.get()
    recorder = get_current_trace()
    if state is not None:
        state["retry_count"] = int(state.get("retry_count") or 0) + 1
        output = state.setdefault("output", {})
        output["retry_count"] = state["retry_count"]
        output["attempts"] = max(int(output.get("attempts") or 1), int(attempt) + 1)
    if recorder is not None:
        recorder.event(
            f"{kind}.retry",
            name=name or str((state or {}).get("name") or ""),
            status="retrying",
            data={
                "attempt": int(attempt),
                "next_attempt": min(int(attempt) + 1, int(max_attempts)),
                "max_attempts": int(max_attempts),
                "delay_seconds": float(delay_seconds or 0),
            },
            model=model or str((state or {}).get("model") or ""),
            tool_name=tool_name or str((state or {}).get("tool_name") or ""),
            error=error,
        )


def _usage_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        import json

        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value or "")


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for char in text if "\u3400" <= char <= "\u9fff")
    non_cjk = max(0, len(text) - cjk)
    return max(1, cjk + ceil(non_cjk / 4))

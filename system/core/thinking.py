"""Per-turn thinking settings, shared by tool use, answers and Wiki review."""
import os

from system.agent_runtime.control import get_run_control

EFFORTS = ("none", "low", "high", "max")


def thinking_effort(value=None):
    if value is None:
        control = get_run_control()
        value = control.loop_state.get("thinking_effort") if control else None
    value = value or os.environ.get("PAPERWIKI_THINKING_EFFORT", "high")
    if value not in EFFORTS:
        raise ValueError("thinking_effort must be none, low, high or max")
    return value


def thinking_options(value=None):
    effort = thinking_effort(value)
    return {"enable_thinking": effort != "none", "reasoning_effort": effort}

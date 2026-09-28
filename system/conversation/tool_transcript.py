"""Provider messages retain call IDs, arguments and opaque reasoning across tools.

This transcript is private transport state. It is never a public trace/answer.
Full tool bodies remain in session_tool_results; large transport results reference
those records through the normal context budget.
"""
from copy import deepcopy
import json


def append_results(messages, observations, render):
    if not messages or messages[-1].get("role") != "assistant":
        return
    by_id = {o.call_id: o for o in observations if o.call_id}
    for call in messages[-1].get("tool_calls") or []:
        observation = by_id.get(call["id"])
        content = render(observation) if observation else json.dumps({
            "status": "not_executed",
            "reason": "This call was filtered, already completed, or could not be dispatched. "
                      "Inspect the saved observations and runtime feedback. Reuse existing results "
                      "or choose another pending task; do not assume this operation succeeded.",
        })
        messages.append({"role": "tool", "tool_call_id": call["id"], "content": content})


def assistant_message(raw):
    # Preserve provider reasoning verbatim for its continuation protocol, without
    # interpreting it or including it in public progress/trace output.
    return {"role": "assistant", **deepcopy({key: raw[key] for key in
            ("content", "reasoning_content", "tool_calls") if key in raw})}

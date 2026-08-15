"""Provider-compatible helpers for deterministic and structured LLM calls."""

from __future__ import annotations

import hashlib
from typing import Any

from system.agent_runtime.tracing import get_current_trace


def _invoke_with_trace(
    llm: Any,
    prompt: str,
    *,
    max_tokens: int,
    temperature: float,
    call,
) -> str:
    trace = get_current_trace()
    if trace is None:
        return call()
    model = str(getattr(llm, "model", "") or getattr(llm, "model_name", "") or llm.__class__.__name__)
    with trace.span(
        "llm.invoke",
        kind="model",
        model=model,
        tool_name="invoke",
        input_data={
            "prompt_chars": len(prompt),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
    ) as span:
        output = call()
        span["output"] = {
            "response_chars": len(output),
            "response_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        }
        return output


def invoke_structured(
    llm: Any,
    prompt: str,
    *,
    max_tokens: int,
    temperature: float = 0.0,
) -> str:
    """Request JSON mode when supported without requiring provider-specific kwargs."""
    def call() -> str:
        try:
            return llm.invoke(
                prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                enable_thinking=False,
                response_format={"type": "json_object"},
            )
        except TypeError as exc:
            message = str(exc)
            if "unexpected keyword argument" not in message:
                raise
            return llm.invoke(prompt, temperature=temperature, max_tokens=max_tokens)

    return _invoke_with_trace(
        llm, prompt, max_tokens=max_tokens, temperature=temperature, call=call
    )


def invoke_deterministic(
    llm: Any,
    prompt: str,
    *,
    max_tokens: int,
    temperature: float = 0.0,
) -> str:
    """Disable hidden reasoning when supported, with a generic-client fallback."""
    def call() -> str:
        try:
            return llm.invoke(
                prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                enable_thinking=False,
            )
        except TypeError as exc:
            message = str(exc)
            if "unexpected keyword argument" not in message:
                raise
            return llm.invoke(prompt, temperature=temperature, max_tokens=max_tokens)

    return _invoke_with_trace(
        llm, prompt, max_tokens=max_tokens, temperature=temperature, call=call
    )

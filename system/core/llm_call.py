"""Provider-compatible helpers for deterministic and structured LLM calls."""

from __future__ import annotations

import hashlib
from typing import Any

from system.agent_runtime.tracing import get_current_trace
from system.core.thinking import thinking_options


def _invoke_with_trace(
    llm: Any,
    prompt: str,
    *,
    max_tokens: int | None,
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
        span["output"].update({
            "response_chars": len(output),
            "response_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
        })
        return output


def invoke_structured(
    llm: Any,
    prompt: str,
    *,
    max_tokens: int | None,
    temperature: float = 0.0,
    max_attempts: int | None = None,
    thinking: bool = False,
) -> str:
    """Request JSON mode when supported without requiring provider-specific kwargs."""
    def call() -> str:
        try:
            return llm.invoke(
                prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                **(thinking_options() if thinking else {"enable_thinking": False}),
                response_format={"type": "json_object"},
                **({"max_attempts": max_attempts} if max_attempts is not None else {}),
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

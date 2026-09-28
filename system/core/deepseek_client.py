"""Official DeepSeek Chat Completions client with tracing and bounded retries."""

import hashlib
import json
import time
import requests
from typing import List, Dict, Optional, Tuple, Any, Iterator

from system.core.config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_CHAT_MODEL,
    DEEPSEEK_CHAT_URL,
)
from system.agent_runtime.tracing import record_current_retry, set_current_span_usage, set_current_span_output
from system.agent_runtime.control import check_run_control


def _require_api_key(api_key: str, env_name: str = "DEEPSEEK_API_KEY") -> str:
    """Validate API credentials early so runtime errors are easier to understand."""
    if api_key:
        return api_key
    raise ValueError(
        f"Missing {env_name}. Please configure it in .env or the environment before starting the app."
    )


class OutputTruncatedError(RuntimeError):
    """A successful HTTP response can still contain an incomplete generation."""

    def __init__(self, *, max_tokens: Optional[int], output_tokens: int = 0):
        self.finish_reason = "length"
        self.max_tokens = max_tokens
        self.output_tokens = output_tokens
        super().__init__(
            f"Model output was truncated (finish_reason=length, "
            f"max_tokens={max_tokens}, output_tokens={output_tokens}); "
            "the incomplete output must not be accepted as a completed result."
        )


def _check_completion(data: Dict[str, Any], max_tokens: Optional[int]) -> Dict[str, Any]:
    """Validate the finish condition before returning text or tool arguments."""
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    reason = choice.get("finish_reason")
    set_current_span_output({
        "finish_reason": reason,
        "max_tokens": max_tokens,
        "response_chars": len(content),
        "response_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    })
    if reason == "length":
        raise OutputTruncatedError(
            max_tokens=max_tokens,
            output_tokens=int((data.get("usage") or {}).get("completion_tokens") or 0),
        )
    return message


# ===========================================================
#  DeepSeekChat — 替代本地 ChatGLM3
# ===========================================================

class DeepSeekChat:
    """
    DeepSeek 官方 Chat API 客户端

    提供与 ChatGLM3 兼容的接口:
      - chat(tokenizer, prompt, history, **kwargs) -> (response, history)
      - stream_chat(tokenizer, prompt, history, **kwargs) -> Iterator

    这样现有代码中 model.chat(...) 可以无缝切换。
    """

    def __init__(
        self,
        api_key: str = None,
        model: str = None,
        base_url: str = None,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        temperature: float = 0.1,
        max_tokens: Optional[int] = None,
    ):
        self.api_key = _require_api_key(api_key or DEEPSEEK_API_KEY)
        self.model = model or DEEPSEEK_CHAT_MODEL
        self.base_url = base_url or DEEPSEEK_CHAT_URL
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.default_temperature = temperature
        self.default_max_tokens = max_tokens

        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        print(f"[{self.__class__.__name__}] 初始化完成 | 模型: {self.model}")

    # ---- 兼容 ChatGLM3 的 chat 接口 ----
    def chat(
        self,
        tokenizer,       # 保留参数位，保持兼容 (实际不使用)
        prompt: str,
        history: list = None,
        **kwargs,
    ) -> Tuple[str, list]:
        """
        兼容 ChatGLM3 的 chat 接口

        Args:
            tokenizer: 占位参数 (不使用，保持接口兼容)
            prompt: 用户输入
            history: 对话历史 [(query, response), ...]
            **kwargs: do_sample, temperature, max_length, repetition_penalty 等

        Returns:
            (response_text, updated_history)
        """
        messages = self._build_messages(prompt, history)

        temperature = kwargs.get("temperature", self.default_temperature)
        if not kwargs.get("do_sample", True):
            temperature = 0
        max_tokens = kwargs.get("max_length", kwargs.get("max_tokens", self.default_max_tokens))

        response_text = self._call_api(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=kwargs.get("response_format"),
            enable_thinking=kwargs.get("enable_thinking"),
        )

        new_history = (history or []) + [(prompt, response_text)]
        return response_text, new_history

    # ---- 兼容 ChatGLM3 的 stream_chat 接口 ----
    def stream_chat(
        self,
        tokenizer,
        prompt: str,
        history: list = None,
        **kwargs,
    ) -> Iterator[Tuple[str, list, None]]:
        """
        兼容 ChatGLM3 的 stream_chat 接口 (流式输出)

        Yields:
            (current_response, history, past_key_values=None)
        """
        messages = self._build_messages(prompt, history)

        temperature = kwargs.get("temperature", self.default_temperature)
        if not kwargs.get("do_sample", True):
            temperature = 0
        max_tokens = kwargs.get("max_length", kwargs.get("max_tokens", self.default_max_tokens))

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        self._set_output_limit(payload, max_tokens)

        new_history = list(history or [])
        full_response = ""

        try:
            response = requests.post(
                self.base_url,
                headers=self.headers,
                json=payload,
                timeout=120,
                stream=True,
            )
            response.raise_for_status()

            import json
            for line in response.iter_lines():
                if not line:
                    continue
                line_str = line.decode("utf-8")
                if line_str.startswith("data: "):
                    data_str = line_str[6:]
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                        delta = data.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            full_response += content
                            yield full_response, new_history, None
                    except json.JSONDecodeError:
                        continue

        except Exception as e:
            print(f"[DeepSeekChat] 流式请求失败: {e}")
            # 降级为非流式
            full_response = self._call_api(messages, temperature=temperature, max_tokens=max_tokens)
            yield full_response, new_history, None

    # ---- 纯文本调用 (供内部组件使用) ----
    def invoke(self, prompt: str, **kwargs) -> str:
        """简单的文本输入 → 文本输出"""
        messages = [{"role": "user", "content": prompt}]
        return self._call_api(messages, **kwargs)

    def tool_call(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        tool_choice: Any = "auto",
        temperature: float = None,
        max_tokens: int = None,
    ) -> Dict[str, Any]:
        """Call the chat API with OpenAI-compatible function-calling tools."""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.default_temperature,
            "stream": False,
            "tools": tools,
            "tool_choice": tool_choice,
        }
        from system.core.thinking import thinking_options
        self._set_thinking(payload, **thinking_options())
        self._set_output_limit(payload, max_tokens)

        for attempt in range(1, self.max_retries + 1):
            check_run_control()
            try:
                return self._request_completion(payload)

            except requests.exceptions.HTTPError as e:
                status_code = e.response.status_code if e.response is not None else "N/A"
                print(
                    f"[DeepSeekChat] Tool call HTTP error "
                    f"(attempt {attempt}/{self.max_retries}): status={status_code}"
                )
                if attempt >= self.max_retries:
                    raise
                delay = self.retry_delay * attempt * (2 if status_code == 429 else 1)
                record_current_retry(
                    name="llm.tool_call", model=self.model, attempt=attempt,
                    max_attempts=self.max_retries, error=str(e), delay_seconds=delay,
                )
                time.sleep(delay)

            except requests.exceptions.RequestException as e:
                print(f"[DeepSeekChat] Tool call request failed (attempt {attempt}/{self.max_retries}): {e}")
                if attempt >= self.max_retries:
                    raise
                delay = self.retry_delay * attempt
                record_current_retry(
                    name="llm.tool_call", model=self.model, attempt=attempt,
                    max_attempts=self.max_retries, error=str(e), delay_seconds=delay,
                )
                time.sleep(delay)

        return {}

    def stream_invoke(self, prompt: str, **kwargs):
        """流式文本输入 → 逐 token 输出 iterator"""
        messages = [{"role": "user", "content": prompt}]
        return self._call_api_stream(messages, **kwargs)

    def _call_api_stream(
        self,
        messages: List[Dict[str, str]],
        temperature: float = None,
        max_tokens: int = None,
        enable_thinking: Optional[bool] = None,
        reasoning_effort: Optional[str] = None,
    ):
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.default_temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        self._set_thinking(payload, enable_thinking, reasoning_effort)
        self._set_output_limit(payload, max_tokens)

        for attempt in range(1, self.max_retries + 1):
            check_run_control()
            response = None
            try:
                response = requests.post(
                    self.base_url,
                    headers=self.headers,
                    json=payload,
                    timeout=120,
                    stream=True,
                )
                response.raise_for_status()
                # 强制 UTF-8：流式 SSE 响应常省略 charset，requests 默认 ISO-8859-1 会导致中文乱码
                response.encoding = "utf-8"
                text_parts, usage, finish_reason = [], {}, None
                for line in response.iter_lines(decode_unicode=True):
                    check_run_control(force=False)
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    data = json.loads(data_str)
                    if data.get("usage"):
                        usage = data["usage"]
                        set_current_span_usage(usage)
                    choice = (data.get("choices") or [{}])[0]
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                    content = (choice.get("delta") or {}).get("content")
                    if content:
                        text_parts.append(content)
                        yield content
                if finish_reason is None:
                    raise RuntimeError("Model stream ended without a completion status")
                _check_completion({"choices": [{"message": {"content": "".join(text_parts)},
                    "finish_reason": finish_reason}], "usage": usage}, payload.get("max_tokens"))
                return

            except requests.exceptions.HTTPError as e:
                status_code = e.response.status_code if e.response is not None else "N/A"
                print(f"[DeepSeekChat] 流式 HTTP 错误 (尝试 {attempt}/{self.max_retries}): 状态码={status_code}")
                if attempt >= self.max_retries:
                    raise
                delay = self.retry_delay * attempt * (2 if status_code == 429 else 1)
                record_current_retry(
                    name="llm.stream_invoke", model=self.model, attempt=attempt,
                    max_attempts=self.max_retries, error=str(e), delay_seconds=delay,
                )
                time.sleep(delay)

            except requests.exceptions.RequestException as e:
                print(f"[DeepSeekChat] 流式请求异常 (尝试 {attempt}/{self.max_retries}): {e}")
                if attempt >= self.max_retries:
                    raise
                delay = self.retry_delay * attempt
                record_current_retry(
                    name="llm.stream_invoke", model=self.model, attempt=attempt,
                    max_attempts=self.max_retries, error=str(e), delay_seconds=delay,
                )
                time.sleep(delay)

            finally:
                if response is not None:
                    response.close()

    # ---- 内部方法 ----
    def _set_output_limit(self, payload, max_tokens):
        """Omit max_tokens unless a caller explicitly requests a bound."""
        selected = self.default_max_tokens if max_tokens is None else max_tokens
        if selected is not None:
            payload["max_tokens"] = selected

    def _request_completion(self, payload):
        """Collect streaming completions, keeping long reasoning connections alive.

        Only the finished assistant message is returned. Reasoning is retained
        for provider continuation, never emitted as user-facing progress.
        """
        request = {**payload, "stream": True, "stream_options": {"include_usage": True}}
        response = requests.post(self.base_url, headers=self.headers, json=request,
                                 timeout=120, stream=True)
        try:
            response.raise_for_status()
            if "text/event-stream" not in getattr(response, "headers", {}).get("Content-Type", ""):
                data = response.json()
            else:
                response.encoding = "utf-8"
                message = {"role": "assistant", "content": ""}
                calls, usage, finish_reason = {}, {}, None
                for line in response.iter_lines(decode_unicode=True):
                    check_run_control(force=False)
                    if not line or not line.startswith("data:"):
                        continue
                    value = line[5:].strip()
                    if value == "[DONE]":
                        break
                    chunk = json.loads(value)
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                        set_current_span_usage(usage)
                    choice = (chunk.get("choices") or [{}])[0]
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                    delta = choice.get("delta") or {}
                    for key in ("content", "reasoning_content"):
                        if delta.get(key):
                            message[key] = message.get(key, "") + delta[key]
                    for part in delta.get("tool_calls") or []:
                        call = calls.setdefault(part.get("index", 0),
                            {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        if part.get("id"):
                            call["id"] += part["id"]
                        for key in ("name", "arguments"):
                            call["function"][key] += (part.get("function") or {}).get(key) or ""
                if calls:
                    message["tool_calls"] = [calls[index] for index in sorted(calls)]
                if finish_reason is None:
                    raise RuntimeError("Model stream ended without a completion status")
                data = {"choices": [{"message": message, "finish_reason": finish_reason}], "usage": usage}
            set_current_span_usage(data.get("usage") or {})
            return _check_completion(data, payload.get("max_tokens"))
        finally:
            close = getattr(response, "close", None)
            if close:
                close()

    def _set_thinking(self, payload: Dict[str, Any], enable_thinking: Optional[bool], reasoning_effort: Optional[str] = None) -> None:
        """Set the correct thinking switch for the selected API provider."""
        if enable_thinking is None and reasoning_effort is None:
            return
        if reasoning_effort is not None:
            from system.core.thinking import thinking_effort
            reasoning_effort = thinking_effort(reasoning_effort)
            enable_thinking = reasoning_effort != "none"
        payload["thinking"] = {"type": "enabled" if enable_thinking else "disabled"}
        if enable_thinking:
            payload["reasoning_effort"] = reasoning_effort or "high"
            payload.pop("temperature", None)

    def _build_messages(self, prompt: str, history: list = None) -> List[Dict[str, str]]:
        """将 ChatGLM3 格式的 history 转换为 OpenAI 格式的 messages"""
        messages = []
        for q, a in (history or []):
            messages.append({"role": "user", "content": q})
            messages.append({"role": "assistant", "content": a})
        messages.append({"role": "user", "content": prompt})
        return messages

    def _call_api(
        self,
        messages: List[Dict[str, str]],
        temperature: float = None,
        max_tokens: int = None,
        response_format: Optional[Dict[str, Any]] = None,
        enable_thinking: Optional[bool] = None,
        reasoning_effort: Optional[str] = None,
        max_attempts: Optional[int] = None,
    ) -> str:
        """带重试的 API 调用"""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.default_temperature,
            "stream": False,
        }
        if response_format:
            payload["response_format"] = response_format
        self._set_thinking(payload, enable_thinking, reasoning_effort)
        self._set_output_limit(payload, max_tokens)

        attempts = max(1, max_attempts if max_attempts is not None else self.max_retries)
        for attempt in range(1, attempts + 1):
            check_run_control()
            try:
                content = self._request_completion(payload).get("content") or ""
                return content.strip()

            except requests.exceptions.HTTPError as e:
                status_code = e.response.status_code if e.response is not None else "N/A"
                print(
                    f"[DeepSeekChat] HTTP 错误 (尝试 {attempt}/{attempts}): "
                    f"状态码={status_code}"
                )
                if attempt >= attempts:
                    raise
                wait = self.retry_delay * attempt * (2 if status_code == 429 else 1)
                if status_code == 429:
                    print(f"[DeepSeekChat] 触发限流，等待 {wait:.1f}s...")
                record_current_retry(
                    name="llm.invoke", model=self.model, attempt=attempt,
                    max_attempts=attempts, error=str(e), delay_seconds=wait,
                )
                time.sleep(wait)

            except requests.exceptions.RequestException as e:
                print(f"[DeepSeekChat] 请求异常 (尝试 {attempt}/{attempts}): {e}")
                if attempt >= attempts:
                    raise
                delay = self.retry_delay * attempt
                record_current_retry(
                    name="llm.invoke", model=self.model, attempt=attempt,
                    max_attempts=attempts, error=str(e), delay_seconds=delay,
                )
                time.sleep(delay)

        return ""

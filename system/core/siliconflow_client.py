"""
硅基流动 (SiliconFlow) 统一客户端

提供与本地 ChatGLM3 兼容的接口，替代所有本地模型调用:
1. SiliconFlowChat   — 替代 ChatGLM3 (chat / stream_chat)
2. SiliconFlowEmbeddings — 替代本地 bge-large-zh-v1.5 HuggingFaceEmbeddings

所有类都通过 config.py 读取 .env 中的配置。
"""

import os
import time
import requests
from typing import List, Dict, Optional, Tuple, Any, Iterator

from system.core.config import (
    SILICONFLOW_API_KEY,
    SILICONFLOW_CHAT_URL,
    SILICONFLOW_BASE_URL,
    SILICONFLOW_CHAT_MODEL,
    SILICONFLOW_FAST_MODEL,
)


def _require_api_key(api_key: str, env_name: str = "SILICONFLOW_API_KEY") -> str:
    """Validate API credentials early so runtime errors are easier to understand."""
    if api_key:
        return api_key
    raise ValueError(
        f"Missing {env_name}. Please configure it in .env or the environment before starting the app."
    )


# ===========================================================
#  SiliconFlowChat — 替代本地 ChatGLM3
# ===========================================================

class SiliconFlowChat:
    """
    硅基流动 Chat API 客户端
    
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
        max_tokens: int = 4096,
    ):
        self.api_key = _require_api_key(api_key or SILICONFLOW_API_KEY)
        self.model = model or SILICONFLOW_CHAT_MODEL
        self.base_url = base_url or SILICONFLOW_CHAT_URL
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.default_temperature = temperature
        self.default_max_tokens = max_tokens

        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        print(f"[SiliconFlowChat] 初始化完成 | 模型: {self.model}")

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
            "max_tokens": max_tokens,
            "stream": True,
        }

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
            print(f"[SiliconFlowChat] 流式请求失败: {e}")
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
            "max_tokens": max_tokens or self.default_max_tokens,
            "stream": False,
            "tools": tools,
            "tool_choice": tool_choice,
        }

        for attempt in range(1, self.max_retries + 1):
            try:
                response = requests.post(
                    self.base_url,
                    headers=self.headers,
                    json=payload,
                    timeout=120,
                )
                response.raise_for_status()
                data = response.json()
                return data.get("choices", [{}])[0].get("message", {}) or {}

            except requests.exceptions.HTTPError as e:
                status_code = e.response.status_code if e.response is not None else "N/A"
                print(
                    f"[SiliconFlowChat] Tool call HTTP error "
                    f"(attempt {attempt}/{self.max_retries}): status={status_code}"
                )
                if status_code == 429:
                    time.sleep(self.retry_delay * attempt * 2)
                elif attempt < self.max_retries:
                    time.sleep(self.retry_delay * attempt)
                else:
                    raise

            except requests.exceptions.RequestException as e:
                print(f"[SiliconFlowChat] Tool call request failed (attempt {attempt}/{self.max_retries}): {e}")
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay * attempt)
                else:
                    raise

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
    ):
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.default_temperature,
            "max_tokens": max_tokens or self.default_max_tokens,
            "stream": True,
        }

        for attempt in range(1, self.max_retries + 1):
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

                for line in response.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        return
                    try:
                        import json
                        data = json.loads(data_str)
                        delta = data.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            yield content
                    except Exception:
                        continue
                return

            except requests.exceptions.HTTPError as e:
                status_code = e.response.status_code if e.response is not None else "N/A"
                print(f"[SiliconFlowChat] 流式 HTTP 错误 (尝试 {attempt}/{self.max_retries}): 状态码={status_code}")
                if status_code == 429:
                    import time
                    time.sleep(self.retry_delay * attempt * 2)
                elif attempt < self.max_retries:
                    import time
                    time.sleep(self.retry_delay * attempt)
                else:
                    raise

            except requests.exceptions.RequestException as e:
                print(f"[SiliconFlowChat] 流式请求异常 (尝试 {attempt}/{self.max_retries}): {e}")
                if attempt < self.max_retries:
                    import time
                    time.sleep(self.retry_delay * attempt)
                else:
                    raise

    # ---- 内部方法 ----
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
    ) -> str:
        """带重试的 API 调用"""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.default_temperature,
            "max_tokens": max_tokens or self.default_max_tokens,
            "stream": False,
        }
        if response_format:
            payload["response_format"] = response_format

        for attempt in range(1, self.max_retries + 1):
            try:
                response = requests.post(
                    self.base_url,
                    headers=self.headers,
                    json=payload,
                    timeout=120,
                )
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                return content.strip()

            except requests.exceptions.HTTPError as e:
                status_code = e.response.status_code if e.response is not None else "N/A"
                print(
                    f"[SiliconFlowChat] HTTP 错误 (尝试 {attempt}/{self.max_retries}): "
                    f"状态码={status_code}"
                )
                if status_code == 429:
                    wait = self.retry_delay * attempt * 2
                    print(f"[SiliconFlowChat] 触发限流，等待 {wait:.1f}s...")
                    time.sleep(wait)
                elif attempt < self.max_retries:
                    time.sleep(self.retry_delay * attempt)
                else:
                    raise

            except requests.exceptions.RequestException as e:
                print(f"[SiliconFlowChat] 请求异常 (尝试 {attempt}/{self.max_retries}): {e}")
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay * attempt)
                else:
                    raise

        return ""


# ===========================================================
#  SiliconFlowEmbeddings — 替代本地 HuggingFaceBgeEmbeddings
# ===========================================================

class SiliconFlowEmbeddings:
    """
    硅基流动 Embedding API 客户端
    
    兼容 LangChain 的 Embeddings 接口:
      - embed_documents(texts) -> List[List[float]]
      - embed_query(text) -> List[float]
    
    可直接传入 Neo4jVector.from_documents / from_existing_graph 等方法。
    """

    def __init__(
        self,
        api_key: str = None,
        model: str = None,
        base_url: str = None,
        max_retries: int = 3,
        retry_delay: float = 2.0,
        batch_size: int = 32,
    ):
        self.api_key = _require_api_key(api_key or SILICONFLOW_API_KEY)
        self.model = model or "Qwen/Qwen3-Embedding-8B"
        self.base_url = base_url or f"{SILICONFLOW_BASE_URL}/embeddings"
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.batch_size = batch_size

        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        print(f"[SiliconFlowEmbeddings] 初始化完成 | 模型: {self.model}")

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """
        批量生成文本向量
        
        Args:
            texts: 文本列表
            
        Returns:
            向量列表 (每个向量为 float 列表)
        """
        all_embeddings = []

        # 分批处理
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]
            batch_embeddings = self._call_api(batch)
            all_embeddings.extend(batch_embeddings)
            
            if i + self.batch_size < len(texts):
                time.sleep(0.1)  # 小延迟避免限流

        return all_embeddings

    def embed_query(self, text: str) -> List[float]:
        """
        生成单个查询文本的向量
        
        Args:
            text: 查询文本
            
        Returns:
            向量 (float 列表)
        """
        result = self._call_api([text])
        return result[0] if result else []

    def _call_api(self, texts: List[str]) -> List[List[float]]:
        """带重试的 Embedding API 调用"""
        payload = {
            "model": self.model,
            "input": texts,
            "encoding_format": "float",
        }

        for attempt in range(1, self.max_retries + 1):
            try:
                response = requests.post(
                    self.base_url,
                    headers=self.headers,
                    json=payload,
                    timeout=60,
                )
                response.raise_for_status()
                data = response.json()

                # 按 index 排序 (API 返回可能乱序)
                embeddings_data = sorted(data["data"], key=lambda x: x["index"])
                return [item["embedding"] for item in embeddings_data]

            except requests.exceptions.HTTPError as e:
                status_code = e.response.status_code if e.response is not None else "N/A"
                print(
                    f"[SiliconFlowEmbeddings] HTTP 错误 (尝试 {attempt}/{self.max_retries}): "
                    f"状态码={status_code}"
                )
                if status_code == 429:
                    wait = self.retry_delay * attempt * 2
                    print(f"[SiliconFlowEmbeddings] 触发限流，等待 {wait:.1f}s...")
                    time.sleep(wait)
                elif attempt < self.max_retries:
                    time.sleep(self.retry_delay * attempt)
                else:
                    raise

            except requests.exceptions.RequestException as e:
                print(
                    f"[SiliconFlowEmbeddings] 请求异常 (尝试 {attempt}/{self.max_retries}): {e}"
                )
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay * attempt)
                else:
                    raise

        return [[] for _ in texts]

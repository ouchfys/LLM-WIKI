"""Bailian dense embeddings via its native DashScope HTTP endpoint.

Only embeddings use this provider. Errors intentionally exclude response bodies,
request texts and credentials because the indexer prints them to server logs.
"""
from functools import lru_cache
import math
import threading
import time

import requests

from system.core.config import (
    BAILIAN_API_KEY, BAILIAN_BASE_URL, WIKI_EMBEDDING_BATCH_SIZE,
    WIKI_EMBEDDING_DIMENSIONS, WIKI_EMBEDDING_MODEL,
)


class BailianEmbeddings:
    def __init__(self, *, api_key=None, base_url=None, model=None,
                 dimensions=None, batch_size=None, timeout=30, max_retries=1):
        self._api_key = BAILIAN_API_KEY if api_key is None else api_key
        if not self._api_key.strip():
            raise ValueError("Bailian embedding API key is not configured")
        self.model = model or WIKI_EMBEDDING_MODEL
        self.dimensions = WIKI_EMBEDDING_DIMENSIONS if dimensions is None else dimensions
        if self.dimensions not in {256, 512, 768, 1024}:
            raise ValueError("Unsupported Bailian embedding dimensions")
        base = (base_url or BAILIAN_BASE_URL).rstrip("/")
        # Accept the familiar compatible base URL, but use native text_index
        # responses: Flash's compatible batch endpoint can return duplicate 0s.
        if base.endswith("/compatible-mode/v1"):
            base = base[:-len("/compatible-mode/v1")] + "/api/v1"
        self.url = base + "/services/embeddings/text-embedding/text-embedding"
        self.batch_size = max(1, min(20, batch_size or WIKI_EMBEDDING_BATCH_SIZE))
        self.timeout = timeout
        self.max_retries = max(0, min(2, max_retries))
        # Changing dimensions/model must invalidate the derived index as well.
        self.index_model = f"bailian:{self.model}:{self.dimensions}:native-v1"
        self._slots = threading.BoundedSemaphore(2)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("Embedding inputs must be non-empty strings")
        vectors = []
        for start in range(0, len(texts), self.batch_size):
            vectors.extend(self._request(texts[start:start + self.batch_size]))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return list(self._cached_query(text)) if text.strip() else []

    @lru_cache(maxsize=128)
    def _cached_query(self, text: str) -> tuple[float, ...]:
        return tuple(self._request([text], text_type="query")[0])

    def _request(self, texts: list[str], text_type: str = "document") -> list[list[float]]:
        payload = {"model": self.model, "input": {"texts": texts},
                   "parameters": {"dimension": self.dimensions,
                                  "text_type": text_type, "output_type": "dense"}}
        for attempt in range(self.max_retries + 1):
            retry = False
            try:
                with self._slots:
                    response = requests.post(
                        self.url, headers={"Authorization": f"Bearer {self._api_key}"},
                        json=payload, timeout=(10, self.timeout), allow_redirects=False,
                    )
                try:
                    status = response.status_code
                    if status == 200:
                        try:
                            data = response.json()
                        except ValueError:
                            raise RuntimeError("Bailian embedding returned invalid JSON") from None
                        return self._validate(data, len(texts))
                    retry = status in {408, 429, 500, 502, 503, 504}
                    error = f"Bailian embedding HTTP {status}"
                finally:
                    response.close()
            except (requests.Timeout, requests.ConnectionError):
                retry = True
                error = "Bailian embedding request timed out or connection failed"
            except requests.RequestException:
                raise RuntimeError("Bailian embedding transport error") from None
            if not retry or attempt == self.max_retries:
                raise RuntimeError(error) from None
            time.sleep(1 + attempt)
        raise RuntimeError("Bailian embedding request failed")

    def _validate(self, data, expected: int) -> list[list[float]]:
        try:
            rows = data["output"]["embeddings"]
            if not isinstance(rows, list) or len(rows) != expected:
                raise ValueError()
            # Flash returns `index`; older native models/docs use `text_index`.
            def position(row):
                return row["text_index"] if "text_index" in row else row["index"]
            indices = [position(row) for row in rows]
            if any(type(index) is not int for index in indices) or sorted(indices) != list(range(expected)):
                raise ValueError()
            vectors = []
            for row in sorted(rows, key=position):
                vector = row["embedding"]
                if not isinstance(vector, list) or len(vector) != self.dimensions:
                    raise ValueError()
                if any(type(value) not in {int, float} or not math.isfinite(value) for value in vector):
                    raise ValueError()
                if not any(vector):
                    raise ValueError()
                vectors.append([float(value) for value in vector])
            return vectors
        except (KeyError, TypeError, ValueError, OverflowError):
            raise RuntimeError("Bailian embedding returned invalid vector count, indices or dimensions") from None

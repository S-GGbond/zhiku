"""对 OpenAI 兼容接口的薄封装：/chat/completions 与 /embeddings。"""

from __future__ import annotations

import time
from typing import Callable, Iterator, Sequence

import numpy as np
from openai import OpenAI


class ConfigError(RuntimeError):
    """配置缺失或不完整。"""


class ProviderError(RuntimeError):
    """上游模型服务返回错误。"""


def _client(base_url: str, api_key: str, timeout: float = 120.0) -> OpenAI:
    if not base_url:
        raise ConfigError("未配置接口地址（Base URL）")
    if not api_key:
        raise ConfigError("未配置 API Key")
    base = base_url.rstrip("/")
    if not base.endswith("/v1") and "/api/paas" not in base:
        base = base + "/v1"
    return OpenAI(base_url=base, api_key=api_key, timeout=timeout, max_retries=0)


class Embedder:
    """批量向量化，返回 L2 归一化后的 float32 矩阵。"""

    def __init__(self, base_url: str, api_key: str, model: str, batch: int = 32, timeout: float = 120.0):
        self.model = model
        self.batch = max(1, int(batch))
        self._client = _client(base_url, api_key, timeout)
        self.name = f"{model}"

    def encode(self, texts: Sequence[str], progress: Callable[[int, int], None] | None = None) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        vectors: list[list[float]] = []
        total = len(texts)
        for start in range(0, total, self.batch):
            batch = [t if t.strip() else " " for t in texts[start:start + self.batch]]
            vectors.extend(self._embed_batch(batch))
            if progress:
                progress(min(start + self.batch, total), total)
        arr = np.asarray(vectors, dtype=np.float32)
        if arr.ndim != 2 or arr.size == 0:
            raise ProviderError("向量服务返回了空结果")
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (arr / norms).astype(np.float32)

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                resp = self._client.embeddings.create(model=self.model, input=batch)
                data = sorted(resp.data, key=lambda d: d.index)
                return [d.embedding for d in data]
            except Exception as exc:  # noqa: BLE001 上游错误类型不统一
                last_error = exc
                if attempt < 2:
                    time.sleep(1.2 * (attempt + 1))
        raise ProviderError(f"向量化失败（模型 {self.model}）：{_brief(last_error)}") from last_error


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str, temperature: float = 0.2,
                 max_tokens: int = 1500, timeout: float = 180.0):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = _client(base_url, api_key, timeout)

    def complete(self, messages: list[dict], *, max_tokens: int | None = None,
                 temperature: float | None = None) -> str:
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature if temperature is None else temperature,
                max_tokens=max_tokens or self.max_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"对话模型调用失败（{self.model}）：{_brief(exc)}") from exc
        return (resp.choices[0].message.content or "").strip()

    def stream(self, messages: list[dict], *, max_tokens: int | None = None,
               temperature: float | None = None) -> Iterator[str]:
        try:
            stream = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature if temperature is None else temperature,
                max_tokens=max_tokens or self.max_tokens,
                stream=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"对话模型调用失败（{self.model}）：{_brief(exc)}") from exc
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            piece = getattr(delta, "content", None)
            if piece:
                yield piece


def _brief(exc: Exception | None) -> str:
    if exc is None:
        return "未知错误"
    text = str(exc).strip().replace("\n", " ")
    return text[:300] if text else exc.__class__.__name__

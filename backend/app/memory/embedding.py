"""Embedding 客户端（ADR-002 Phase C）。

- `embedding_model` 非空时启用向量召回，走 Volces Ark /embeddings（推理接入点 ep-xxx）；
- embedding 网关与 chat 网关是两个 base_url（ark/api/v3 vs ark/api/coding/v3），独立建客户端；
- api_key 缺省复用 LLM_API_KEY（同一方舟账号）；
- 失败策略：embedding 属于召回旁路，失败记 warning 后返回空列表，调用方降级到
  全量注入/关键词召回，绝不阻塞回答主链路。
"""
from __future__ import annotations

import logging
from typing import Protocol

from app.config import settings

log = logging.getLogger(__name__)


class EmbeddingClient(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class ArkEmbeddingClient:
    """Volces Ark OpenAI-compatible embeddings。方舟限制单请求最多 10 条 input，自动分批。"""

    BATCH_SIZE = 10

    def __init__(self, model: str | None = None, base_url: str | None = None, api_key: str | None = None) -> None:
        from openai import AsyncOpenAI

        self._model = model or settings.embedding_model
        self._client = AsyncOpenAI(
            base_url=base_url or settings.embedding_base_url,
            api_key=api_key or settings.embedding_api_key or settings.llm_api_key,
            timeout=settings.console_llm_timeout_seconds,
            max_retries=0,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        for i in range(0, len(texts), self.BATCH_SIZE):
            batch = texts[i : i + self.BATCH_SIZE]
            resp = await self._client.embeddings.create(model=self._model, input=batch)
            out.extend(item.embedding for item in resp.data)
        return out


class NullEmbeddingClient:
    """未配置 embedding 时的占位实现。"""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return []


def create_embedding_client() -> EmbeddingClient:
    if settings.embedding_model:
        log.info("embedding enabled: model=%s dim=%s", settings.embedding_model, settings.embedding_dim)
        return ArkEmbeddingClient()
    log.info("embedding disabled: EMBEDDING_MODEL empty, vector recall off")
    return NullEmbeddingClient()


def cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    da = sum(x * x for x in a) ** 0.5
    db = sum(y * y for y in b) ** 0.5
    if da == 0 or db == 0:
        return 0.0
    return num / (da * db)

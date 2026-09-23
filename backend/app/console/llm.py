"""LLM 客户端抽象。测试可注入 fake，生产用 OpenAI 兼容接口。"""
from __future__ import annotations

from typing import Protocol

from app.config import settings


class LLMClient(Protocol):
    async def complete(self, system: str, user: str, *, model: str | None = None) -> str:
        """返回模型文本输出。"""
        ...


class OpenAIClient:
    def __init__(self) -> None:
        from openai import AsyncOpenAI

        # 硬超时防群队列冻结；禁用 SDK 自动重试（它会在超时窗口内默默重试 2 次，
        # 把最坏阻塞拖到数分钟），快速失败交给上层降级
        self._client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=settings.console_llm_timeout_seconds,
            max_retries=0,
        )

    async def complete(self, system: str, user: str, *, model: str | None = None) -> str:
        resp = await self._client.chat.completions.create(
            model=model or settings.console_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
        )
        return resp.choices[0].message.content or ""

"""LLM 客户端抽象。测试可注入 fake，生产用 OpenAI 兼容接口。"""
from __future__ import annotations

import json
import time
from pathlib import Path
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
        self._dump_seq = 0

    def _dump(self, model: str, system: str, user: str) -> None:
        """调试开关：CONSOLE_LLM_DUMP_DIR 非空时把完整请求报文落盘。"""
        dump_dir = settings.console_llm_dump_dir
        if not dump_dir:
            return
        try:
            self._dump_seq += 1
            p = Path(dump_dir)
            p.mkdir(parents=True, exist_ok=True)
            payload = {
                "seq": self._dump_seq,
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
            (p / f"llm_dump_{time.strftime('%H%M%S')}_{self._dump_seq:03d}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001 —— 调试落盘失败不影响主链路
            pass

    async def complete(self, system: str, user: str, *, model: str | None = None) -> str:
        resolved_model = model or settings.console_model
        self._dump(resolved_model, system, user)
        resp = await self._client.chat.completions.create(
            model=resolved_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
        )
        return resp.choices[0].message.content or ""

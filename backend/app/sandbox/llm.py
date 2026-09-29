"""Sandbox 多轮 LLM 客户端（ADR-004 详细设计 §1.1）。

与 console/llm.py 的单轮 complete 分离：执行循环需要原生 function calling
（messages 历史 + tools 定义 → 文本与结构化 tool_calls）。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config import settings

log = logging.getLogger(__name__)


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResult:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)


class SandboxLLMClient(Protocol):
    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResult: ...


class OpenAIChatClient:
    """OpenAI 兼容多轮客户端。工具参数解析失败整体视为文本（上层自纠）。"""

    def __init__(self) -> None:
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=settings.sandbox_llm_timeout_seconds,
            max_retries=1,
        )
    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResult:
        kwargs: dict[str, Any] = {
            "model": settings.sandbox_model or settings.console_model,  # 留空复用 console 模型
            "messages": messages,
            "temperature": 0.2,
            # 显式上限防端点默认值截断：content 为空 + finish_reason=length 是
            # 「排查未产出结论」类失败的隐形来源（任务 #dccfa430），必须可见
            "max_tokens": settings.sandbox_llm_max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        resp = await self._client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        finish = resp.choices[0].finish_reason or ""
        # 空正文可观测（含 finish_reason）：agent 层有补调兜底，这里负责留下诊断线索
        if not (msg.content or "").strip():
            log.warning(
                "sandbox llm empty content finish_reason=%s reasoning_len=%d tool_calls=%d model=%s",
                finish, len(getattr(msg, "reasoning_content", None) or ""), len(msg.tool_calls or []),
                kwargs["model"],
            )
        elif finish == "length":
            log.warning("sandbox llm content truncated finish_reason=length model=%s", kwargs["model"])

        calls: list[ToolCall] = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                log.warning("tool call args not json, wrapping as _raw: %s", tc.function.arguments[:120])
                args = {"_raw": tc.function.arguments}
            calls.append(ToolCall(id=tc.id, name=tc.function.name, args=args))
        return LLMResult(text=msg.content or "", tool_calls=calls)

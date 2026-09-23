"""路由器（ADR-003）：一次 LLM 调用产出 JSON 决策，解析失败重试 1 次后降级。"""
from __future__ import annotations

import json
import logging
import re

from app.console.llm import LLMClient
from app.console.prompts import build_route_system
from app.console.router_types import Decision

log = logging.getLogger(__name__)


def _extract_json(text: str) -> dict:
    """从模型输出里抠出第一个 JSON 对象，容忍 markdown 围栏与前后杂字。"""
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.S)
    if fence:
        t = fence.group(1)
    start = t.find("{")
    if start == -1:
        raise ValueError("no json object found")
    depth = 0
    for i in range(start, len(t)):
        if t[i] == "{":
            depth += 1
        elif t[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(t[start : i + 1])
    raise ValueError("unbalanced json braces")


class Router:
    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm
        self._system = build_route_system(Decision.json_schema_hint())

    @property
    def llm(self) -> LLMClient:
        """压缩等辅助功能复用同一 LLM 客户端。"""
        return self._llm

    async def route(self, context: str, user_text: str) -> Decision:
        user = f"{context}\n\n[用户本条消息]\n{user_text}" if context else user_text
        raw = await self._llm.complete(self._system, user)
        decision = self._try_parse(raw)
        if decision is not None:
            return decision
        # 重试一次：把解析错误反馈给模型
        log.warning("route json parse failed, retrying once. raw=%s", raw[:200])
        retry_user = (
            f"{user}\n\n[系统提示] 你上一条输出不是合法 JSON，请只输出一个 JSON 对象。\n上次输出：{raw[:500]}"
        )
        raw2 = await self._llm.complete(self._system, retry_user)
        decision = self._try_parse(raw2)
        if decision is not None:
            return decision
        # 降级：原样回复，不静默失败
        log.error("route json parse failed after retry, degrade to answer. raw=%s", raw2[:200])
        return Decision.model_validate(
            {"action": "answer", "answer": raw2.strip()[:1000] or "我暂时无法理解，请换个说法。"}
        )

    @staticmethod
    def _try_parse(raw: str) -> Decision | None:
        try:
            data = _extract_json(raw)
            return Decision.model_validate(data)
        except Exception:  # noqa: BLE001
            return None

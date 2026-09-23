"""ToolRegistry：极简注册协议（ADR-006）。

无基类、无装饰器、无 pydantic。工具 = schema dict + handler callable + 可选 check_fn。
handler 统一签名 (args: dict, **ctx) -> JSON 字符串。
参考 hermes-agent tools/registry.py。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.config import settings
from app.tools.schema import coerce_args, tool_error, tool_result

Handler = Callable[..., str]
CheckFn = Callable[[], bool]

# check_fn 结果缓存：name -> (ok, fetched_at)
_check_cache: dict[str, tuple[bool, float]] = {}


@dataclass
class ToolEntry:
    name: str
    toolset: str
    schema: dict
    handler: Handler
    check_fn: CheckFn | None = None
    is_async: bool = False
    max_result_size_chars: int | None = None
    description: str = ""


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolEntry] = {}
        self._generation = 0

    # ---- 注册 ----

    def register(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Handler,
        check_fn: CheckFn | None = None,
        is_async: bool = False,
        max_result_size_chars: int | None = None,
        description: str = "",
        override: bool = False,
    ) -> None:
        if name in self._tools and not override:
            raise ValueError(f"tool already registered: {name}")
        self._tools[name] = ToolEntry(
            name=name, toolset=toolset, schema=schema, handler=handler,
            check_fn=check_fn, is_async=is_async,
            max_result_size_chars=max_result_size_chars, description=description,
        )
        self._generation += 1

    def deregister(self, name: str) -> None:
        if self._tools.pop(name, None) is not None:
            self._generation += 1

    def get_entry(self, name: str) -> ToolEntry | None:
        return self._tools.get(name)

    @property
    def generation(self) -> int:
        return self._generation

    # ---- check_fn 可用性（带缓存 + 抗抖动）----

    def _check_available(self, entry: ToolEntry) -> bool:
        if entry.check_fn is None:
            return True
        now = time.time()
        cached = _check_cache.get(entry.name)
        if cached and now - cached[1] < settings.tool_check_ttl_seconds:
            return cached[0]
        try:
            ok = bool(entry.check_fn())
        except Exception:  # noqa: BLE001
            ok = False
        if not ok and cached and cached[0] and now - cached[1] < settings.tool_check_grace_seconds:
            # last-good 抗抖动：探测偶发失败时短期内仍按可用
            return True
        _check_cache[entry.name] = (ok, now)
        return ok

    # ---- 给模型的工具定义 ----

    def get_definitions(
        self,
        enabled_toolsets: set[str] | None = None,
        disabled_toolsets: set[str] | None = None,
    ) -> list[dict]:
        """返回 OpenAI 格式的工具定义列表，仅含通过三层过滤的工具。"""
        out: list[dict] = []
        for entry in self._tools.values():
            if enabled_toolsets is not None and entry.toolset not in enabled_toolsets:
                continue
            if disabled_toolsets is not None and entry.toolset in disabled_toolsets:
                continue
            if not self._check_available(entry):
                continue
            out.append({"type": "function", "function": entry.schema})
        return out

    def valid_names(
        self,
        enabled_toolsets: set[str] | None = None,
        disabled_toolsets: set[str] | None = None,
    ) -> set[str]:
        return {
            d["function"]["name"]
            for d in self.get_definitions(enabled_toolsets, disabled_toolsets)
        }

    def get_max_result_size(self, name: str) -> int:
        entry = self._tools.get(name)
        if entry and entry.max_result_size_chars:
            return entry.max_result_size_chars
        return settings.tool_output_max_chars

    # ---- 执行 ----

    async def dispatch(self, name: str, args: dict, **ctx: Any) -> str:
        entry = self._tools.get(name)
        if entry is None:
            return tool_error(f"Unknown tool: {name}", max_chars=settings.tool_error_max_chars)
        try:
            coerced = coerce_args(args, entry.schema)
            if entry.is_async:
                result = await entry.handler(coerced, **ctx)
            else:
                result = await asyncio.to_thread(entry.handler, coerced, **ctx)
            result = result if isinstance(result, str) else tool_result(result)
            # 结果截断
            limit = self.get_max_result_size(name)
            if len(result) > limit:
                result = result[:limit] + "…(truncated)"
            return result
        except Exception as e:  # noqa: BLE001
            return tool_error(
                f"Tool execution failed: {type(e).__name__}: {e}",
                max_chars=settings.tool_error_max_chars,
            )


# 模块级单例
registry = ToolRegistry()

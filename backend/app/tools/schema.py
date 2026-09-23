"""工具结果序列化与参数强转（ADR-006）。"""
from __future__ import annotations

import json
from typing import Any


def tool_result(payload: Any) -> str:
    """工具成功结果统一序列化为 JSON 字符串。"""
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False, default=str)


def tool_error(message: str, *, max_chars: int = 2048) -> str:
    """工具错误结果统一为 {"error": ...}，错误体封顶。"""
    if len(message) > max_chars:
        message = message[:max_chars] + "…(truncated)"
    return json.dumps({"error": message}, ensure_ascii=False)


def coerce_arg(value: Any, schema_prop: dict) -> Any:
    """按 schema 声明把字符串参数强转类型（"42"→42、"true"→True、JSON 串→对象）。"""
    if not isinstance(value, str):
        return value
    t = schema_prop.get("type")
    try:
        if t == "integer":
            return int(value)
        if t == "number":
            return float(value)
        if t == "boolean":
            return value.strip().lower() in ("1", "true", "yes", "on")
        if t in ("object", "array"):
            return json.loads(value)
    except (ValueError, json.JSONDecodeError):
        return value
    return value


def coerce_args(args: dict, schema: dict) -> dict:
    props = (schema or {}).get("parameters", {}).get("properties", {})
    if not props:
        return args
    out = dict(args)
    for k, v in args.items():
        if k in props:
            out[k] = coerce_arg(v, props[k])
    return out

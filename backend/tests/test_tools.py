"""ToolRegistry 单元测试（ADR-006）。"""
from __future__ import annotations

import pytest

from app.tools.registry import ToolRegistry
from app.tools.schema import coerce_args, tool_error, tool_result


def make_registry() -> ToolRegistry:
    return ToolRegistry()


READ_SCHEMA = {
    "name": "read_file",
    "description": "读文件",
    "parameters": {
        "type": "object",
        "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["path"],
    },
}


def test_register_and_definitions():
    r = make_registry()
    r.register("read_file", "file", READ_SCHEMA, lambda a, **k: tool_result({"ok": 1}))
    defs = r.get_definitions()
    assert len(defs) == 1 and defs[0]["function"]["name"] == "read_file"
    assert r.valid_names() == {"read_file"}


def test_duplicate_register_raises_without_override():
    r = make_registry()
    r.register("t", "file", READ_SCHEMA, lambda a, **k: "x")
    with pytest.raises(ValueError):
        r.register("t", "file", READ_SCHEMA, lambda a, **k: "y")
    r.register("t", "file", READ_SCHEMA, lambda a, **k: "y", override=True)  # 不抛


def test_toolset_whitelist_filter():
    r = make_registry()
    r.register("a", "file", {**READ_SCHEMA, "name": "a"}, lambda a, **k: "1")
    r.register("b", "git", {**READ_SCHEMA, "name": "b"}, lambda a, **k: "2")
    assert r.valid_names(enabled_toolsets={"file"}) == {"a"}
    assert r.valid_names(disabled_toolsets={"git"}) == {"a"}


def test_check_fn_hides_unavailable():
    r = make_registry()
    r.register("a", "file", READ_SCHEMA, lambda a, **k: "1", check_fn=lambda: False)
    r.register("b", "file", {**READ_SCHEMA, "name": "b"}, lambda a, **k: "2", check_fn=lambda: True)
    assert r.valid_names() == {"b"}


@pytest.mark.asyncio
async def test_dispatch_coerces_args():
    captured = {}

    def handler(args, **ctx):
        captured.update(args)
        return tool_result({"got": args})

    r = make_registry()
    r.register("read_file", "file", READ_SCHEMA, handler)
    out = await r.dispatch("read_file", {"path": "/a", "limit": "42"})
    assert captured["limit"] == 42  # 字符串 "42" 被强转为 int
    assert '"got"' in out


@pytest.mark.asyncio
async def test_dispatch_unknown_tool():
    r = make_registry()
    out = await r.dispatch("nope", {})
    assert '"error"' in out and "Unknown tool" in out


@pytest.mark.asyncio
async def test_dispatch_exception_becomes_error():
    def bad(args, **ctx):
        raise RuntimeError("boom")

    r = make_registry()
    r.register("bad", "file", READ_SCHEMA, bad)
    out = await r.dispatch("bad", {"path": "/a"})
    assert '"error"' in out and "boom" in out


@pytest.mark.asyncio
async def test_dispatch_truncates_result():
    r = make_registry()
    r.register("big", "file", READ_SCHEMA, lambda a, **k: "x" * 100, max_result_size_chars=10)
    out = await r.dispatch("big", {"path": "/a"})
    assert len(out) <= 10 + len("…(truncated)")


def test_coerce_args_types():
    schema = {
        "parameters": {
            "properties": {
                "n": {"type": "integer"},
                "f": {"type": "boolean"},
                "o": {"type": "object"},
            }
        }
    }
    out = coerce_args({"n": "7", "f": "true", "o": '{"a":1}'}, schema)
    assert out == {"n": 7, "f": True, "o": {"a": 1}}


def test_tool_error_truncation():
    out = tool_error("e" * 5000, max_chars=100)
    assert len(out) < 200 and "truncated" in out

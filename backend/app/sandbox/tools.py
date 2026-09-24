"""沙箱工具集（ADR-004 详细设计 §6）：定义、调度与执行面。

设计定案：工具 schema 全任务通用，注册一次；任务间唯一变化是 exec 到哪个容器，
因此容器以 ToolContext（Executor 门面）在 dispatch 时注入，而非注册期绑定。
所有输出回填 messages 前经 sanitize 统一脱敏（PAT → ***）并截断。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from app.config import settings

log = logging.getLogger(__name__)

# ---- Executor 门面协议（fastclaw Executor 接口的 Python 版）----


class Executor(Protocol):
    """容器执行面。真实实现绑定 Docker 容器；测试注入 Fake。"""

    async def exec(self, command: str, *, timeout: float | None = None) -> str:
        """在容器工作区内执行 shell 命令，返回合并输出。"""
        ...

    async def read_file(self, path: str) -> str:
        """读文本文件；二进制/不存在时抛异常。"""
        ...

    async def write_file(self, path: str, content: str) -> None:
        """整体写文件（stdin 管道，不受 argv NUL 限制）。"""
        ...

    async def list_dir(self, path: str = ".") -> list[dict]:
        """列目录，返回 [{name, type, size}]。"""
        ...


@dataclass
class ToolContext:
    """一次任务执行的工具上下文。executor 即该任务绑定的容器门面。"""

    executor: Executor
    repo: str = ""
    task_id: str = ""


# ---- 输出处理：脱敏 + 截断 ----


def _secret_strings() -> list[str]:
    """需要从输出中抹掉的敏感串（PAT 等）。从仓库执行配置收集。"""
    from app.sandbox.repo_config import collect_secrets

    return collect_secrets()


def sanitize(text: str) -> str:
    """把已知凭据替换为 ***（fastclaw env 事故教训：模型会复述输出里的凭据）。"""
    for secret in _secret_strings():
        if secret and secret in text:
            text = text.replace(secret, "***")
    return text


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[已截断，原始 {len(text)} 字符]"


def _wrap(payload: Any) -> str:
    """工具成功输出统一 JSON 信封，并做脱敏 + 截断。"""
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    text = sanitize(text)
    return _truncate(text, settings.sandbox_tool_output_max_chars)


def _err(message: str) -> str:
    return json.dumps({"error": _truncate(sanitize(message), 2048)}, ensure_ascii=False)


# ---- 工具 schema（OpenAI function 格式，供 build_tool_definitions 输出）----

_SCHEMAS: dict[str, dict] = {
    "list_dir": {
        "name": "list_dir",
        "description": "列出仓库内目录内容（名称、类型、大小）。路径相对工作区根。",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "目录路径，默认根目录"}},
        },
    },
    "read_file": {
        "name": "read_file",
        "description": "读取仓库内文本文件全文（二进制文件会被拒绝）。超长文件尾部截断。",
        "parameters": {
            "type": "object",
            "required": ["path"],
            "properties": {"path": {"type": "string", "description": "相对工作区根的文件路径"}},
        },
    },
    "grep": {
        "name": "grep",
        "description": "在仓库内按正则搜索文本，返回匹配行（文件:行号:内容）。",
        "parameters": {
            "type": "object",
            "required": ["pattern"],
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式"},
                "glob": {"type": "string", "description": "可选文件过滤，如 '*.py'"},
            },
        },
    },
    "write_file": {
        "name": "write_file",
        "description": "整体写入一个文件（覆盖已有内容；目录不存在会自动创建）。",
        "parameters": {
            "type": "object",
            "required": ["path", "content"],
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string", "description": "完整文件内容"},
            },
        },
    },
    "apply_patch": {
        "name": "apply_patch",
        "description": "对已有文件应用统一 diff（unified diff）补丁。",
        "parameters": {
            "type": "object",
            "required": ["path", "diff"],
            "properties": {
                "path": {"type": "string"},
                "diff": {"type": "string", "description": "unified diff 文本"},
            },
        },
    },
    "run_command": {
        "name": "run_command",
        "description": "在工作区执行 bash 命令（构建、测试、git 等）。有单命令超时；输出尾部截断。",
        "parameters": {
            "type": "object",
            "required": ["command"],
            "properties": {"command": {"type": "string"}},
        },
    },
    "git_diff": {
        "name": "git_diff",
        "description": "查看当前工作区相对基线的全部改动（git diff）。无改动时返回空。",
        "parameters": {"type": "object", "properties": {}},
    },
    "git_log": {
        "name": "git_log",
        "description": "查看本分支相对基线的提交列表（git log）。",
        "parameters": {"type": "object", "properties": {}},
    },
}

# 危险命令黑名单（fastclaw 同款：整体小写后子串匹配，只防毁灭性命令）
_DANGEROUS = ("rm -rf /", "mkfs", "dd if=", ":(){:|:&};:", "> /dev/sd")


# ---- handler 实现（全部经 ctx.executor 执行）----


async def _h_list_dir(args: dict, ctx: ToolContext) -> str:
    return _wrap(await ctx.executor.list_dir(args.get("path", ".")))


async def _h_read_file(args: dict, ctx: ToolContext) -> str:
    try:
        return _wrap(await ctx.executor.read_file(args["path"]))
    except FileNotFoundError:
        return _err(f"文件不存在: {args['path']}")
    except UnicodeDecodeError:
        return _err(f"二进制文件，拒绝读取: {args['path']}")


async def _h_grep(args: dict, ctx: ToolContext) -> str:
    cmd = f"grep -rn --include={args.get('glob', '*')} -E {args['pattern']!r} ."
    return _wrap(await ctx.executor.exec(cmd))


async def _h_write_file(args: dict, ctx: ToolContext) -> str:
    await ctx.executor.write_file(args["path"], args["content"])
    return _wrap(f"ok: wrote {args['path']} ({len(args['content'])} chars)")


async def _h_apply_patch(args: dict, ctx: ToolContext) -> str:
    # patch 文件写入 /tmp（stdin 文件管道，避免 argv 长度与引号转义问题），工作区内应用
    diff_path = f"/tmp/.domino_patch_{ctx.task_id or 'x'}.diff"
    await ctx.executor.write_file(diff_path, args["diff"])
    return _wrap(await ctx.executor.exec(f"patch -p1 --forward < {diff_path}"))


async def _h_run_command(args: dict, ctx: ToolContext) -> str:
    command = str(args["command"]).strip().lower()
    for d in _DANGEROUS:
        if d in command:
            return _err(f"危险命令已拦截: {args['command'][:120]}")
    return _wrap(await ctx.executor.exec(args["command"]))


async def _h_git_diff(args: dict, ctx: ToolContext) -> str:
    return _wrap(await ctx.executor.exec("git diff HEAD --stat && git diff HEAD"))


async def _h_git_log(args: dict, ctx: ToolContext) -> str:
    return _wrap(await ctx.executor.exec("git log --oneline HEAD --not @{u} || git log --oneline -20"))


_HANDLERS = {
    "list_dir": _h_list_dir,
    "read_file": _h_read_file,
    "grep": _h_grep,
    "write_file": _h_write_file,
    "apply_patch": _h_apply_patch,
    "run_command": _h_run_command,
    "git_diff": _h_git_diff,
    "git_log": _h_git_log,
}


# ---- 对外接口（agent.py 调用）----


def build_tool_definitions() -> list[dict]:
    """给模型的 OpenAI 工具定义列表。"""
    return [{"type": "function", "function": s} for s in _SCHEMAS.values()]


async def dispatch_tool(name: str, args: dict, ctx: ToolContext) -> str:
    """工具调度入口。任何异常都转为错误信封返回，绝不中断执行循环。"""
    handler = _HANDLERS.get(name)
    if handler is None:
        return _err(f"未知工具: {name}。可用工具: {', '.join(_HANDLERS)}")
    try:
        return await handler(args or {}, ctx)
    except Exception as e:  # noqa: BLE001
        log.warning("tool %s failed: %s: %s", name, type(e).__name__, e)
        return _err(f"{type(e).__name__}: {e}")

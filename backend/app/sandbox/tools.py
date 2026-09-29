"""沙箱工具集（ADR-004 详细设计 §6）：定义、调度与执行面。

设计定案：工具 schema 全任务通用，注册一次；任务间唯一变化是 exec 到哪个容器，
因此容器以 ToolContext（Executor 门面）在 dispatch 时注入，而非注册期绑定。
所有输出回填 messages 前经 sanitize 统一脱敏（PAT → ***）并截断。
"""
from __future__ import annotations

import asyncio
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
        "description": (
            "列出仓库内目录内容（名称、类型、大小）。路径相对工作区根。"
            "用于了解目录结构；已知具体文件路径时直接 read_file，无需先列目录预检查。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "目录路径，默认根目录"}},
        },
    },
    "read_file": {
        "name": "read_file",
        "description": (
            "读取仓库内文本文件全文（二进制文件会被拒绝）。超长文件尾部截断。"
            "已知路径直接读，读取失败会返回错误——错误本身就是有效信息，"
            "不要先 list_dir 确认文件存在；文件刚写入成功后不要重读验证，"
            "写入成功即已落盘。"
        ),
        "parameters": {
            "type": "object",
            "required": ["path"],
            "properties": {"path": {"type": "string", "description": "相对工作区根的文件路径"}},
        },
    },
    "grep": {
        "name": "grep",
        "description": (
            "在仓库内按正则搜索文本，返回匹配行（文件:行号:内容）。"
            "用于查找不确定位置的内容或定义/调用点；"
            "已知具体文件要看内容时直接 read_file，不要用 grep 变相读文件。"
        ),
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
        "description": (
            "整体写入一个文件（覆盖已有内容；目录不存在会自动创建）。"
            "用于新建文件或放弃增量修补时整体重写；对已有文件做局部修改优先 apply_patch。"
            "写入成功后文件即已更新，无需重读确认。"
        ),
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
        "description": (
            "对已有文件应用 V4A 格式补丁（不是 unified diff）。格式：\n"
            "*** Begin Patch\n"
            "*** Update File: path/to/file.py\n"
            "@@ 上下文提示 @@\n"
            " 上下文行（行首保留空格）\n"
            "-被删除的行\n"
            "+新增的行\n"
            "*** End Patch\n"
            "规则：上下文行取自最近一次读到的文件内容，文件改过之后先重读再写补丁，"
            "不要重放旧补丁；同一处补丁失败两次就不要再试第三次，改用 write_file "
            "整体重写该函数或文件。"
        ),
        "parameters": {
            "type": "object",
            "required": ["diff"],
            "properties": {
                "diff": {"type": "string", "description": "V4A 格式补丁文本，见工具描述中的格式说明"},
            },
        },
    },
    "run_command": {
        "name": "run_command",
        "description": (
            "在工作区执行 bash 命令（构建、测试、git、安装依赖等）。有单命令超时；输出尾部截断。"
            "读文件用 read_file、搜内容用 grep、写文件用 write_file/apply_patch，"
            "不要用 cat/grep/sed/echo 等 shell 命令替代专用工具；"
            "命令输出里的测试结果要亲眼看确认通过后再声称完成。"
        ),
        "parameters": {
            "type": "object",
            "required": ["command"],
            "properties": {"command": {"type": "string"}},
        },
    },
    "git_diff": {
        "name": "git_diff",
        "description": (
            "查看当前工作区相对基线的全部改动（git diff）。"
            "提交前用它自查改动范围，确认没有无关改动混入。无改动时返回空。"
        ),
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
# read_file / write_file / apply_patch / grep 四个 handler 接入 hermes
# ShellFileOperations（vendor，见 _vendor/ + hermes_adapter.py）：
# 原子写、模糊补丁、rg→grep 降级搜索、行号分页读，全部是她修过几百个 issue 的实现。

from app.sandbox.hermes_adapter import HermesEnvAdapter  # noqa: E402
from app.sandbox._vendor import file_operations as _hfo  # noqa: E402

_ops_cache: dict[int, Any] = {}


def _get_file_ops(ctx: ToolContext):
    """每个 executor 复用一个 ShellFileOperations（内含 _has_command 缓存）。"""
    key = id(ctx.executor)
    if key not in _ops_cache:
        _ops_cache[key] = _hfo.ShellFileOperations(HermesEnvAdapter(ctx.executor), cwd="/workspace")
    return _ops_cache[key]


def _tool_name(fn) -> str:
    """vendor 侧报错时的调用方工具名（用于错误文案）。"""
    return fn.__name__.replace("_", " ")


async def _h_list_dir(args: dict, ctx: ToolContext) -> str:
    return _wrap(await ctx.executor.list_dir(args.get("path", ".")))


async def _h_read_file(args: dict, ctx: ToolContext) -> str:
    ops = _get_file_ops(ctx)
    r = await asyncio.to_thread(ops.read_file, args["path"])
    if r.error:
        return _err(r.error)
    return _wrap(r.content)


async def _h_grep(args: dict, ctx: ToolContext) -> str:
    ops = _get_file_ops(ctx)
    glob = args.get("glob") or None
    r = await asyncio.to_thread(
        ops.search, args["pattern"], path="/workspace", file_glob=glob, limit=50
    )
    if r.error:
        return _err(r.error)
    lines = [f"{m.path}:{m.line_number}:{m.content}" for m in r.matches]
    if r.total_count > len(lines):
        lines.append(f"…共 {r.total_count} 个匹配（已截断）")
    return _wrap("\n".join(lines) or "（无匹配）")


async def _h_write_file(args: dict, ctx: ToolContext) -> str:
    ops = _get_file_ops(ctx)
    r = await asyncio.to_thread(ops.write_file, args["path"], args["content"])
    if r.error:
        return _err(r.error)
    return _wrap(f"ok: wrote {args['path']} ({len(args['content'])} chars)")


async def _h_apply_patch(args: dict, ctx: ToolContext) -> str:
    ops = _get_file_ops(ctx)
    r = await asyncio.to_thread(ops.patch_v4a, args["diff"])
    if r.error:
        return _err(r.error)
    return _wrap(f"ok: applied patch to {args['path']}")


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

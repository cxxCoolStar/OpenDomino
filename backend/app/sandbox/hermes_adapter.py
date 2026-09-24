"""hermes ShellFileOperations ↔ OpenDomino DockerExecutor 适配层。

vendor 的 file_operations.py 要求 terminal_env 提供同步方法：
    execute(command, cwd=..., stdin_data=...) -> {"output": str, "returncode": int}

设计取舍（重要）：hermes 的原子写脚本把内容经 **stdin** 传入（cat > "$tmp"）。
exec_run 的 socket stdin 在我们 setsid+marker 包装下不可靠，因此 adapter 不做
heredoc 内嵌（引号/转义二次污染），改为把 stdin_data 写入宿主侧临时变量后经
**base64 参数**内嵌：`printf %s <b64> | base64 -d | { command with stdin}`——
脚本经 eval 时 stdin 由管道提供，行为等价且不引入转义问题。

DockerExecutor.exec 返回合并文本（exit code 附在尾部 [exit N]），
adapter 负责解析回 {"output", "returncode"} 结构。
"""
from __future__ import annotations

import asyncio
import base64
import re
import threading

_EXIT_RE = re.compile(r"\n?\[exit (\d+)\]\s*$")


class HermesEnvAdapter:
    """把 DockerExecutor 包装成 hermes 期望的 terminal_env（duck typing）。"""

    cwd = "/workspace"

    def __init__(self, domino_executor) -> None:
        self._ex = domino_executor

    def execute(self, command: str, cwd: str = "", timeout: int = None,
                stdin_data: str = None, **kw) -> dict:
        workdir = cwd or None
        if stdin_data is not None:
            # 内容经 base64 内嵌 + 管道注入 stdin（不碰引号转义）
            b64 = base64.b64encode(stdin_data.encode("utf-8")).decode("ascii")
            command = f"printf %s {b64} | base64 -d | {{ {command}\n}}"
        try:
            out, rc = _run_sync(self._ex.exec(command, timeout=timeout, workdir=workdir))
        except Exception as e:
            # 执行层失败（ExecError 等）：vendor 层只认 returncode 判失败，
            # 把异常降级为 rc=127 + 错误文本，让上游的 _has_command/降级逻辑正常工作
            return {"output": f"{type(e).__name__}: {e}", "returncode": 127}
        return {"output": out, "returncode": rc}


def _run_sync(coro) -> tuple[str, int | None]:
    """在有运行中事件循环时于独立线程跑（vendor 层为同步代码）；否则直接执行。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        out = asyncio.run(coro)
        return _split_rc(out)
    result: dict = {}

    def _runner() -> None:
        result["out"] = asyncio.run(coro)

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join()
    return _split_rc(result["out"])


def _split_rc(text: str) -> tuple[str, int | None]:
    """DockerExecutor 把 exit code 附在尾部 [exit N]；解析回结构化返回。"""
    m = _EXIT_RE.search(text)
    if m:
        return text[: m.start()].rstrip("\n"), int(m.group(1))
    return text, 0

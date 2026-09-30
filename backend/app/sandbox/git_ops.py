"""git 操作封装（ADR-004 详细设计 §3）：分支 / 提交 / push / diff，全部经容器 exec。

设计边界：凭据已由 container 层织入 clone URL，本模块不接触 token；
所有命令在工作区 /workspace 内执行；真超时由 DockerExecutor.exec 击杀进程组。
"""
from __future__ import annotations

import logging

from app.sandbox.container import DockerExecutor

log = logging.getLogger(__name__)


async def create_task_branch(executor: DockerExecutor, task_id: str) -> str:
    """从当前 HEAD 建任务分支 domino/task-{task_id}，返回分支名。"""
    branch = f"domino/task-{task_id[:8]}"
    await executor.exec(f"git checkout -b {branch}", timeout=60)
    return branch


async def stage_all(executor: DockerExecutor) -> bool:
    """暂存全部改动。返回是否有变化可提交。"""
    out = await executor.exec("git status --porcelain", timeout=60)
    if not out.strip():
        return False
    await executor.exec("git add -A", timeout=120)
    return True


async def commit(executor: DockerExecutor, message: str, author: str = "OpenDomino Sandbox") -> str | None:
    """提交。返回 commit sha；无可提交内容返回 None。"""
    if not await stage_all(executor):
        return None
    await executor.exec(
        f"git -c user.name='{author}' -c user.email='sandbox@opendomino.local' "
        f"commit -m '{message[:200].replace(chr(39), chr(39) * 2)}'",
        timeout=120,
    )
    sha = (await executor.exec("git rev-parse HEAD", timeout=60)).strip()
    return sha


async def push(executor: DockerExecutor, branch: str) -> bool:
    """推送任务分支到 origin。失败抛异常由上层转 TaskEvent。"""
    await executor.exec(f"git push -u origin {branch} 2>&1", timeout=300)
    log.info("pushed branch=%s", branch)
    return True


async def commit_log(executor: DockerExecutor, base_ref: str) -> list[dict]:
    """相对基线的提交列表 [{sha, message}]。"""
    out = await executor.exec(
        f"git log --format='%H%x00%s' {base_ref}..HEAD",
        timeout=60,
    )
    commits = []
    for line in out.splitlines():
        if "\x00" in line:
            sha, msg = line.split("\x00", 1)
            commits.append({"sha": sha, "message": msg})
    return commits


async def diff_stat(executor: DockerExecutor, base_ref: str) -> str:
    """相对基线的 diff --stat（回传群里的改动统计）。"""
    return await executor.exec(f"git diff --stat {base_ref}..HEAD", timeout=60)


async def full_diff(executor: DockerExecutor, base_ref: str, *, max_chars: int = 20000) -> str:
    """相对基线的完整 diff（供 AI 预评审/验证），尾部截断。"""
    out = await executor.exec(f"git diff {base_ref}..HEAD", timeout=120)
    if len(out) > max_chars:
        return out[:max_chars] + f"\n…[diff 已截断，原始 {len(out)} 字符]"
    return out

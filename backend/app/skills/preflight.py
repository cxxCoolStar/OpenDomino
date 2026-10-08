"""requires{env,bins,tools} 加载期可用性预检（ADR-005）。

目标：把「装了但跑不了」从运行期瞎找提前为加载期明示。
fastclaw 教训：模型曾烧 19 分钟 `find /` 找一个没装的二进制——预检失败的
输出必须直接给出缺什么、并禁止满盘找/装。
"""
from __future__ import annotations

import asyncio
import shutil

from app.skills.registry import SkillMeta


async def _bin_exists(bin_name: str) -> bool:
    """沙箱内探测可执行文件。宿主进程直接 which；容器内语义由调用方注入 executor。

    阶段一宿主侧实现用 shutil.which；容器内探测（executor.exec which）
    在阶段二 SandboxAgent 接入时由 loader 注入探测函数替换。
    """
    return shutil.which(bin_name) is not None


async def check_requires(
    meta: SkillMeta,
    *,
    available_tools: set[str] | None = None,
    bin_probe=None,
) -> str | None:
    """求值 requires，返回缺失说明（全部满足返回 None）。

    available_tools：本任务注入的工具面白名单（ADR-004 固定九项 + load_skill）。
    bin_probe：async (bin) -> bool，缺省用宿主 shutil.which（阶段二换成容器内探测）。
    env 探测宿主进程即真实语义（容器内 env 由镜像决定，阶段二同样经注入替换）。
    """
    probe = bin_probe or _bin_exists
    missing: list[str] = []

    for tool in meta.requires_tools:
        if available_tools is not None and tool not in available_tools:
            missing.append(f"tools: {tool}")

    for var in meta.requires_env:
        import os

        if not os.environ.get(var):
            missing.append(f"env: {var}")

    for bin_name in meta.requires_bins:
        if not await probe(bin_name):
            missing.append(f"bins: {bin_name}")

    if not missing:
        return None
    return f"[SKILL CURRENTLY UNAVAILABLE: 缺 {'、'.join(missing)}] 不要满盘查找或尝试安装，换方法或告知用户。"


def preflight_sync(meta: SkillMeta, *, available_tools: set[str] | None = None) -> str | None:
    """纯同步场景（无 bins 依赖）的便捷包装。"""
    if meta.requires_bins:
        raise ValueError("preflight_sync 不支持 bins 探测，用 check_requires")
    return asyncio.get_event_loop().run_until_complete(
        check_requires(meta, available_tools=available_tools)
    ) if False else _sync_check(meta, available_tools=available_tools)


def _sync_check(meta: SkillMeta, *, available_tools: set[str] | None) -> str | None:
    missing: list[str] = []
    for tool in meta.requires_tools:
        if available_tools is not None and tool not in available_tools:
            missing.append(f"tools: {tool}")
    for var in meta.requires_env:
        import os

        if not os.environ.get(var):
            missing.append(f"env: {var}")
    if missing:
        return f"[SKILL CURRENTLY UNAVAILABLE: 缺 {'、'.join(missing)}] 不要满盘查找或尝试安装，换方法或告知用户。"
    return None

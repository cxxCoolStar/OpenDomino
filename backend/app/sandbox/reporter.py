"""任务状态回传（ADR-004 详细设计 §4）。

四类事件回传来源群：受理 / 开始 / 待评审 / 终态；不做逐工具心跳播报。
发送经 ChannelRegistry；TaskEvent 永远落库（通知丢失不丢留痕）。
"""
from __future__ import annotations

import logging

from app.channels.registry import ChannelRegistry
from app.sandbox.types import Task, TaskStage, TaskStatus

log = logging.getLogger(__name__)


def _short(tid: str) -> str:
    return tid[:8] if len(tid) > 8 else tid


class TaskReporter:
    def __init__(self, registry: ChannelRegistry) -> None:
        self._registry = registry

    async def _notify(self, task: Task, text: str) -> None:
        await self._registry.send(task.chat_id, text)

    async def accepted(self, task: Task) -> None:
        await self._notify(
            task,
            f"✅ 已受理任务 #{_short(task.id)}：{task.title}\n"
            f"仓库：{task.repo}，排队执行中，完成后我会在群里同步结果。",
        )

    async def started(self, task: Task) -> None:
        kind = "排查" if task.type == "investigate" else "变更"
        await self._notify(
            task,
            f"🚀 {kind}任务 #{_short(task.id)} 开始执行：{task.title}"
            + (f"\n分支：`{task.branch_name}`（基于 {task.base_ref[:8]}）" if task.branch_name else ""),
        )

    async def concluded(self, task: Task, conclusion: str) -> None:
        """排查任务终态：结论正文回传（排查的交付物是结论本身）。"""
        await self._notify(
            task,
            f"🔍 排查任务 #{_short(task.id)} 完成：{task.title}\n\n{conclusion[:1500]}",
        )

    async def review_ready(
        self, task: Task, *, commits: list[dict], diff_stat: str, review_points: str
    ) -> None:
        lines = [
            f"📋 任务 #{_short(task.id)} 已完成待评审：{task.title}",
            f"分支：`{task.branch_name}`，共 {len(commits)} 个提交：",
        ]
        for c in commits[:5]:
            lines.append(f"- `{c.get('sha', '')[:7]}` {c.get('message', '')}")
        if len(commits) > 5:
            lines.append(f"- …等 {len(commits)} 个提交")
        if diff_stat:
            lines.append(f"\n改动统计：\n{diff_stat}")
        if review_points:
            lines.append(f"\n**自评要点**\n{review_points}")
        await self._notify(task, "\n".join(lines))

    async def finished(self, task: Task) -> None:
        if task.status == TaskStatus.DONE:
            await self._notify(
                task,
                f"🎉 任务 #{_short(task.id)} 完成：{task.title}\n"
                f"验证：{task.verify_result or '未运行'}；请到分支 `{task.branch_name}` 评审合入。",
            )
        elif task.status == TaskStatus.FAILED:
            await self._notify(
                task,
                f"❌ 任务 #{_short(task.id)} 失败：{task.title}\n原因：{task.error or '未知'}",
            )
        else:  # canceled
            await self._notify(task, f"🚫 任务 #{_short(task.id)} 已取消：{task.title}")

    async def event(self, task: Task, stage: TaskStage, kind: str, summary: str) -> None:
        """非关键事件：只落 TaskEvent，不推群（ADR-004：不做心跳播报）。"""
        log.info("task event: id=%s stage=%s kind=%s %s", task.id, stage, kind, summary[:100])

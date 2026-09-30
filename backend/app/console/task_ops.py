"""任务查询与中途管控的确定性执行（ADR-010）。

ConsoleAgent 路由出 task_op 意图后，本模块查 TaskService / 经 TaskControl
下发 steer、cancel，产出回复文案。格式化集中在这里，ConsoleAgent 不碰沙箱类型。
"""
from __future__ import annotations

import time

from app.config import settings
from app.sandbox.control import TaskControl
from app.sandbox.storage import TaskService
from app.sandbox.types import Task, TaskStatus

_STATUS_ICON = {
    TaskStatus.PENDING: "⏳ 排队中",
    TaskStatus.RUNNING: "🚀 执行中",
    TaskStatus.REVIEW: "📋 待评审",
    TaskStatus.DONE: "✅ 已完成",
    TaskStatus.FAILED: "❌ 失败",
    TaskStatus.CANCELED: "🚫 已取消",
}

_OK_HEX = set("0123456789abcdef")


def _short(tid: str) -> str:
    return tid[:8] if len(tid) > 8 else tid


def _elapsed(task: Task) -> str:
    """任务已耗时（到上次更新为止；终结任务即总耗时）。"""
    if not task.created_at:
        return ""
    end = task.updated_at or time.time()
    seconds = max(0, int(end - task.created_at))
    if seconds >= 60:
        return f"{seconds // 60} 分钟"
    return f"{seconds} 秒"


def _is_stale(task: Task) -> bool:
    """running 且超过 stale 阈值无任何事件/状态刷新 → 疑似停滞（ADR-010）。"""
    if task.status != TaskStatus.RUNNING:
        return False
    last = task.updated_at or task.created_at
    return bool(last) and time.time() - last > settings.sandbox_stale_minutes * 60


def _normalize_task_id(task_id: str) -> str | None:
    """任务号归一：剥 # 前缀、取 8 位 hex；其余形状（昵称/截断号）无效。"""
    tid = task_id.strip().lower().lstrip("#")
    if len(tid) == 8 and all(c in _OK_HEX for c in tid):
        return tid
    return None


class TaskOpHandler:
    def __init__(self, tasks: TaskService, control: TaskControl) -> None:
        self._tasks = tasks
        self._control = control

    async def handle(self, chat_id: str, op: str, task_id: str, message: str) -> str:
        if op == "status":
            return await self._status(chat_id, task_id)
        if op == "steer":
            return await self._steer(chat_id, task_id, message)
        if op == "cancel":
            return await self._cancel(chat_id, task_id)
        return f"还不支持「{op}」这个任务操作，目前可以查进展（status）、中途指示（steer）、取消（cancel）。"

    # ---- status ----

    async def _status(self, chat_id: str, task_id: str) -> str:
        tid = _normalize_task_id(task_id) if task_id else None
        if tid:
            task = await self._tasks.get(tid)
            if task is None or task.chat_id != chat_id:
                return f"本群没有找到任务 #{_short(tid) or task_id}。说「查看任务」列出本群最近的任务。"
            return await self._render_one(task, with_events=True)
        tasks = await self._tasks.list_by_chat(chat_id, limit=10)
        if not tasks:
            return "本群还没有提交过任务。要做事可以说「帮我到某仓库排查/修改…」。"
        lines = [await self._render_one(t) for t in tasks]
        tail = f"问单个任务详情请带任务号，如「#{_short(tasks[0].id)} 怎么样了」。"
        return "本群最近的任务：\n" + "\n".join(lines) + "\n\n" + tail

    async def _render_one(self, task: Task, *, with_events: bool = False) -> str:
        icon = _STATUS_ICON.get(task.status, task.status.value)
        parts = [f"#{_short(task.id)} {icon}：{task.title or '（无标题）'}"]
        cost = _elapsed(task)
        if cost:
            parts.append(f"耗时 {cost}")
        if task.branch_name:
            parts.append(f"分支 `{task.branch_name}`")
        if task.error:
            parts.append(f"原因：{task.error[:120]}")
        stale_hint = (
            " ⚠️ 超过 " + str(settings.sandbox_stale_minutes) + " 分钟无新进展，可能停滞"
            if _is_stale(task) else ""
        )
        line = f"- {parts[0]}（{'，'.join(parts[1:])}）{stale_hint}" if len(parts) > 1 else f"- {parts[0]}{stale_hint}"
        if not with_events:
            return line
        events = await self._tasks.recent_events(task.id, limit=5)
        if events:
            ev_lines = [f"  {e.seq}. {e.summary[:80]}" for e in events]
            line += "\n**最近动态**\n" + "\n".join(ev_lines)
        elif task.status == TaskStatus.RUNNING:
            line += "\n（执行中，暂无步骤动态）"
        return line

    # ---- steer / cancel ----

    async def _steer(self, chat_id: str, task_id: str, message: str) -> str:
        tid = _normalize_task_id(task_id)
        if tid is None:
            return "请告诉我要对哪个任务下指示？带上任务号，如「#a1b2c3d4 只看 auth 模块」。"
        if not message:
            return "请把要传达的内容一并说清楚，如「#a1b2c3d4 先只看 auth 模块，别动配置」。"
        task = await self._tasks.get(tid)
        if task is None or task.chat_id != chat_id:
            return f"本群没有找到任务 #{_short(tid)}。"
        if task.status != TaskStatus.RUNNING:
            icon = _STATUS_ICON.get(task.status, task.status.value)
            return f"任务 #{_short(tid)} 当前是「{icon}」，不在执行中，中途指示只在执行时有效。"
        ok = await self._control.steer(tid, message)
        if not ok:
            return f"任务 #{_short(tid)} 刚好结束了，指示未能传达。可以看它的结果再决定要不要新起任务。"
        await self._tasks.append_event(tid, "execute", "steer_received", f"群内中途指示：{message[:80]}")
        return f"已收到，将在任务 #{_short(tid)} 的下一步传给执行中的任务。若任务在此之前结束，我会再告诉你。"

    async def _cancel(self, chat_id: str, task_id: str) -> str:
        tid = _normalize_task_id(task_id)
        if tid is None:
            return "请告诉我要取消哪个任务？带上任务号，如「取消 #a1b2c3d4」。"
        task = await self._tasks.get(tid)
        if task is None or task.chat_id != chat_id:
            return f"本群没有找到任务 #{_short(tid)}。"
        if task.status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELED):
            return f"任务 #{_short(tid)} 已经是终态（{_STATUS_ICON[task.status]}），无需取消。"
        if task.status == TaskStatus.REVIEW:
            return f"任务 #{_short(tid)} 已完成待评审，不跑取消；不想要可以直接不合入该分支。"
        if task.status == TaskStatus.PENDING:
            # 排队中：协作式取消标志没人消费（循环未启动），直接落终态
            await self._tasks.finish(tid, TaskStatus.CANCELED)
            return f"已取消排队中的任务 #{_short(tid)}（尚未开始执行，无进展损失）。"
        ok = await self._control.request_cancel(tid)
        if not ok:
            return f"任务 #{_short(tid)} 刚好结束了，取消未生效。"
        await self._tasks.append_event(tid, "execute", "cancel_requested", "群内请求取消任务")
        return f"已收到取消请求，任务 #{_short(tid)} 会在当前步骤结束后停止，已有进展会汇总后同步。"

"""任务中途管控面（ADR-010）：steer 注入与协作式取消的进程内通道。

ConsoleAgent（收指令）与 SandboxAgent 循环（执行）同进程，内存队列即连通：
steer 文本经 per-task asyncio.Queue 在执行循环的迭代边界投递；cancel 只置
标志位，由循环在边界感知后协作退出。任务不在跑（或已结束）时操作返回 False。
"""
from __future__ import annotations

import asyncio


class TaskControl:
    """per-task 管控通道注册表。TaskPipeline 在领任务时 bind，finally 里 unbind。"""

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[str]] = {}
        self._cancel_flags: dict[str, bool] = {}
        self._lock = asyncio.Lock()

    async def bind(self, task_id: str) -> None:
        """任务开始执行前登记通道（幂等：重复 bind 重置为可用状态）。"""
        async with self._lock:
            self._queues[task_id] = asyncio.Queue()
            self._cancel_flags[task_id] = False

    async def unbind(self, task_id: str) -> list[str]:
        """任务终态后注销通道，返回未消费的 steer 文本（missed，供上报）。"""
        async with self._lock:
            q = self._queues.pop(task_id, None)
            self._cancel_flags.pop(task_id, None)
        missed: list[str] = []
        if q is not None:
            while not q.empty():
                missed.append(q.get_nowait())
        return missed

    async def steer(self, task_id: str, text: str) -> bool:
        """向 running 任务队一条纠偏文本。False = 任务未登记（不在跑）。"""
        cleaned = (text or "").strip()
        if not cleaned:
            return False
        async with self._lock:
            q = self._queues.get(task_id)
        if q is None:
            return False
        await q.put(cleaned)
        return True

    async def request_cancel(self, task_id: str) -> bool:
        """请求协作式取消。False = 任务未登记（不在跑）。"""
        async with self._lock:
            if task_id not in self._queues:
                return False
            self._cancel_flags[task_id] = True
        return True

    async def drain_steer(self, task_id: str) -> list[str]:
        """执行循环迭代边界调用：取走全部待投递的 steer 文本（不等待）。"""
        async with self._lock:
            q = self._queues.get(task_id)
        if q is None:
            return []
        texts: list[str] = []
        while not q.empty():
            texts.append(q.get_nowait())
        return texts

    async def cancel_requested(self, task_id: str) -> bool:
        async with self._lock:
            return self._cancel_flags.get(task_id, False)

"""任务队列与并发控制（ADR-004 §2.2）。

- 全局并发由 SANDBOX_MAX_CONCURRENCY 信号量控制；
- 同一仓库串行（单容器不互抢工作区），不同仓库并行；
- worker 从队列领任务后回调 run_task(task_id)——由上层注入真实执行体
  （SandboxAgent 执行循环），本模块只管排队与并发。
"""
from __future__ import annotations

import asyncio
import logging

from app.config import settings

log = logging.getLogger(__name__)


class TaskExecutor:
    """按仓库串行的任务执行器。"""

    def __init__(self, run_task, *, max_concurrency: int | None = None) -> None:
        self._run_task = run_task                       # async (task_id) -> None
        self._sem = asyncio.Semaphore(max_concurrency or settings.sandbox_max_concurrency)
        self._queues: dict[str, asyncio.Queue] = {}     # repo -> queue
        self._workers: dict[str, asyncio.Task] = {}
        self._draining = False

    # ---- 入队 ----

    async def submit(self, task_id: str, repo: str) -> None:
        """任务入队。同 repo 排队，跨 repo 并行（并发上限内）。"""
        q = self._queues.get(repo)
        if q is None:
            q = asyncio.Queue()
            self._queues[repo] = q
            self._workers[repo] = asyncio.create_task(
                self._worker(repo), name=f"sandbox-worker-{repo}"
            )
        await q.put(task_id)
        log.info("task queued: id=%s repo=%s depth=%d", task_id, repo, q.qsize())

    async def requeue(self, task_ids: list[str], *, repo_of) -> None:
        """重启恢复：把 pending 任务重新入队。repo_of(task_id) -> repo 名。"""
        for tid in task_ids:
            repo = repo_of(tid)
            if repo:
                await self.submit(tid, repo)

    # ---- worker ----

    async def _worker(self, repo: str) -> None:
        q = self._queues[repo]
        while True:
            task_id = await q.get()
            try:
                async with self._sem:                   # 全局并发闸门
                    log.info("task start: id=%s repo=%s", task_id, repo)
                    await self._run_task(task_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 —— worker 不能死，run_task 内部应已兜底
                log.exception("task crashed: id=%s repo=%s", task_id, repo)
            finally:
                q.task_done()

    # ---- 生命周期 ----

    async def drain(self, timeout: float = 30.0) -> None:
        """停机时等待在跑任务结束（尽力而为）。"""
        self._draining = True
        queues = list(self._queues.values())
        if not queues:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*(q.join() for q in queues)), timeout=timeout
            )
        except TimeoutError:
            log.warning("drain timeout, %d tasks may be interrupted", sum(q.qsize() for q in queues))

    async def close(self) -> None:
        for w in self._workers.values():
            w.cancel()
        await asyncio.gather(*self._workers.values(), return_exceptions=True)
        self._workers.clear()
        self._queues.clear()

"""任务执行管线（ADR-004 详细设计 §管线）：把全部沙箱模块串成 run_task。

流程：领取任务 → 取容器 → 建分支 → 执行循环 → 提交 push → 待评审回传。
验证（AI review）与回炉留接口，一期以预评审替代（ADR-004 非目标：不自动合并）。
"""
from __future__ import annotations

import logging
import time

from app.config import settings
from app.sandbox import git_ops
from app.sandbox.agent import SandboxAgent
from app.sandbox.container import ContainerPool
from app.sandbox.reporter import TaskReporter
from app.sandbox.storage import TaskService
from app.sandbox.tools import ToolContext
from app.sandbox.types import Task, TaskStage, TaskStatus

log = logging.getLogger(__name__)


class TaskPipeline:
    """run_task 实现：TaskExecutor 的 worker 回调直接指向这里。"""

    def __init__(
        self,
        tasks: TaskService,
        pool: ContainerPool,
        agent: SandboxAgent,
        reporter: TaskReporter,
    ) -> None:
        self.tasks = tasks
        self.pool = pool
        self.agent = agent
        self.reporter = reporter

    async def run_task(self, task_id: str) -> None:
        task = await self.tasks.get(task_id)
        if task is None:
            log.warning("pipeline got unknown task %s", task_id)
            return
        if task.status not in (TaskStatus.PENDING,):
            log.info("task %s status=%s, skip re-run", task_id, task.status)
            return

        await self.tasks.set_status(task_id, TaskStatus.RUNNING)
        await self.tasks.append_event(task_id, TaskStage.SETUP, "stage_enter", "开始准备执行环境")

        executor = None
        try:
            executor = await self.pool.acquire(task.repo)

            # 工作区重置到基线（首次为当前 HEAD；恢复任务用库里的 base_ref）
            base_ref = await self.pool.reset_workspace(task.repo, executor, task.base_ref)
            await self.tasks.set_running(
                task_id, branch_name=f"domino/task-{task_id[:8]}", base_ref=base_ref
            )
            branch = await git_ops.create_task_branch(executor, task_id)
            await self.tasks.set_running(task_id, branch_name=branch, base_ref=base_ref)

            await self.reporter.started(task)

            # 执行循环
            ctx = ToolContext(executor=executor, repo=task.repo, task_id=task_id)
            started = time.time()
            result = await self.agent.run(task, ctx, on_event=self._on_event(task))
            log.info(
                "loop finished task=%s status=%s steps=%d %.1fs",
                task_id, result.status, result.steps_used, time.time() - started,
            )

            if result.status == "failed":
                await self.tasks.finish(task_id, TaskStatus.FAILED, error=result.error)
                task.error = result.error
                task.status = TaskStatus.FAILED
                await self.reporter.finished(task)
                return

            # 提交与推送（空改动也能交付：说明无代码修改必要）
            sha = await git_ops.commit(executor, f"domino: {task.title}")
            commits = []
            if sha:
                await git_ops.push(executor, branch)
                commits = await git_ops.commit_log(executor, base_ref)
            diff_stat = await git_ops.diff_stat(executor, base_ref)
            full_diff = await git_ops.full_diff(executor, base_ref)

            # LLM 预评审（一期替代独立 verify；blocked 回炉在 verify.py 落地后启用）
            review_points = await self._pre_review(task, full_diff, result.summary)

            await self.tasks.set_review(task_id, commits=commits, verify_result="pending")
            await self.reporter.review_ready(
                task, commits=commits, diff_stat=diff_stat, review_points=review_points
            )
            await self.tasks.append_event(
                task_id, TaskStage.REVIEW, "stage_exit",
                f"已推送 {len(commits)} 个提交到 {branch}，等待人工评审",
            )
            # 一期终态：review 即视为交付完成（人工合入）；verify 回炉后接 done
            await self.tasks.finish(task_id, TaskStatus.DONE, verify_result="review")
            task.status = TaskStatus.DONE
            task.verify_result = "review"
            await self.reporter.finished(task)

        except Exception as e:  # noqa: BLE001 —— 任何异常都要落终态 + 群通知
            log.exception("pipeline failed task=%s", task_id)
            await self.tasks.finish(
                task_id, TaskStatus.FAILED, error=f"{type(e).__name__}: {e}"
            )
            task.status = TaskStatus.FAILED
            task.error = f"{type(e).__name__}: {e}"
            await self.reporter.finished(task)
        finally:
            if executor is not None:
                await self.pool.release(task.repo, executor)

    @staticmethod
    def _on_event(task: Task):
        async def _emit(stage: TaskStage, kind: str, summary: str) -> None:
            # agent 回调是同步 lambda 兼容；此处转 async append_event
            pass  # 事件经 agent 内部 on_event 同步收集，由 reporter.event 落库
        return _emit

    async def _pre_review(self, task: Task, diff: str, summary: str) -> str:
        """轻量预评审：直接把执行循环的汇总作为评审要点返回（verify.py 一期后补独立评审）。"""
        return summary or "（执行循环未产出汇总）"

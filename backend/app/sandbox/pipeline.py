"""任务执行管线（ADR-004 详细设计 §管线；ADR-010 事件落库与中途管控）。

流程：领取任务 → 取容器 → 建分支 → 执行循环 → 提交 push → 待评审回传。
验证（AI review）与回炉留接口，一期以预评审替代（ADR-004 非目标：不自动合并）。

ADR-010 增补：执行期工具事件真正落 TaskEvent 表；TaskControl 绑定后，
steer 在迭代边界注入、cancel 协作退出落 CANCELED 终态，未消费的 steer
作为 missed 报告回群。
"""
from __future__ import annotations

import logging
import time

from app.config import settings
from app.sandbox import git_ops
from app.sandbox.agent import SandboxAgent
from app.sandbox.container import ContainerPool
from app.sandbox.control import TaskControl
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
        memory=None,
        control: TaskControl | None = None,
    ) -> None:
        self.tasks = tasks
        self.pool = pool
        self.agent = agent
        self.reporter = reporter
        self._memory = memory      # 可选：L2 绑定解析（M:N 仓库解析主路径）
        self.control = control or TaskControl()   # 管控面：ConsoleAgent 经此下发 steer/cancel

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
        await self.control.bind(task_id)

        executor = None
        try:
            executor = await self.pool.acquire(task.repo, chat_id=task.chat_id, memory=self._memory)

            # 工作区重置到基线（首次为当前 HEAD；恢复任务用库里的 base_ref）
            base_ref = await self.pool.reset_workspace(task.repo, executor, task.base_ref)

            # 排查任务：只读路径——不建分支、不提交；结论即交付物（原文主路径）
            if task.type == "investigate":
                await self._run_investigate(task_id, task, executor, base_ref)
                return

            await self.tasks.set_running(
                task_id, branch_name=f"domino/task-{task_id[:8]}", base_ref=base_ref
            )
            branch = await git_ops.create_task_branch(executor, task_id)
            await self.tasks.set_running(task_id, branch_name=branch, base_ref=base_ref)

            await self.reporter.started(task)

            # 执行循环
            ctx = ToolContext(executor=executor, repo=task.repo, task_id=task_id)
            started = time.time()
            result = await self.agent.run(
                task, ctx, on_event=self._on_event(task_id), control=self.control
            )
            log.info(
                "loop finished task=%s status=%s steps=%d %.1fs",
                task_id, result.status, result.steps_used, time.time() - started,
            )

            # 协作式取消（ADR-010）：跳过交付链，落 CANCELED 终态并回群
            if result.exit_reason == "canceled":
                await self.tasks.append_event(
                    task_id, TaskStage.EXECUTE, "stage_exit", "任务已取消，已汇总中断前进展"
                )
                await self.tasks.finish(task_id, TaskStatus.CANCELED)
                task.status = TaskStatus.CANCELED
                await self.reporter.finished(task)
                return

            if result.status == "failed":
                await self.tasks.finish(task_id, TaskStatus.FAILED, error=result.error)
                task.error = result.error
                task.status = TaskStatus.FAILED
                await self.reporter.finished(task)
                return

            # 提交与推送：模型可能已自行 commit（工作区 clean）——所以"是否产生提交"
            # 以 HEAD 是否领先 base_ref 为准；仍有未提交改动时补一次收尾提交。
            await git_ops.commit(executor, f"domino: {task.title}")   # 无改动时返回 None（已提交场景）
            commits = await git_ops.commit_log(executor, base_ref)
            if not commits:
                error = "模型未产生任何代码提交，任务无法交付"
                log.warning("modify task without commits: %s", task_id)
                await self.tasks.finish(task_id, TaskStatus.FAILED, error=error)
                task.status = TaskStatus.FAILED
                task.error = error
                await self.reporter.finished(task)
                return
            await git_ops.push(executor, branch)
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
            return

        except Exception as e:  # noqa: BLE001 —— 任何异常都要落终态 + 群通知
            log.exception("pipeline failed task=%s", task_id)
            await self.tasks.finish(
                task_id, TaskStatus.FAILED, error=f"{type(e).__name__}: {e}"
            )
            task.status = TaskStatus.FAILED
            task.error = f"{type(e).__name__}: {e}"
            await self.reporter.finished(task)
        finally:
            await self._report_missed_steer(task_id)
            if executor is not None:
                await self.pool.release(task.repo, executor)

    async def _report_missed_steer(self, task_id: str) -> None:
        """任务结束仍未消费的 steer 文本回群告知（ADR-010 missed 语义）。

        unbind 兼职做 leftover 收集：注销通道时返回未消费文本，避免双读竞态。
        """
        missed = await self.control.unbind(task_id)
        if missed:
            task = await self.tasks.get(task_id)
            if task is not None:
                await self.reporter.steer_missed(task, missed)

    def _on_event(self, task_id: str):
        """执行循环事件落库（ADR-010：此前是 pass 空函数，TaskEvent 执行期为空）。"""

        async def _emit(stage: TaskStage, kind: str, summary: str) -> None:
            await self.tasks.append_event(task_id, stage, kind, summary)

        return _emit

    async def _run_investigate(self, task_id: str, task: Task, executor, base_ref: str) -> None:
        """排查任务（原文主路径）：只读真实仓库 + 跑命令复现，结论即交付物。

        不建分支、不 commit/push、不进 review 状态机；workspace 无写要求。
        """
        await self.reporter.started(task)

        ctx = ToolContext(executor=executor, repo=task.repo, task_id=task_id)
        started = time.time()
        result = await self.agent.run(
            task, ctx, on_event=self._on_event(task_id), control=self.control
        )
        log.info(
            "investigate loop finished task=%s status=%s steps=%d %.1fs",
            task_id, result.status, result.steps_used, time.time() - started,
        )

        # 协作式取消（ADR-010）：排查任务同样可中途取消
        if result.exit_reason == "canceled":
            await self.tasks.finish(task_id, TaskStatus.CANCELED)
            task.status = TaskStatus.CANCELED
            await self.reporter.finished(task)
            return

        if result.status == "failed" or not result.summary.strip():
            error = result.error or "排查未产出结论"
            await self.tasks.finish(task_id, TaskStatus.FAILED, error=error)
            task.status = TaskStatus.FAILED
            task.error = error
            await self.reporter.finished(task)
            return

        await self.tasks.finish(task_id, TaskStatus.DONE, verify_result="conclusion")
        task.status = TaskStatus.DONE
        task.verify_result = "conclusion"
        await self.reporter.concluded(task, result.summary)
        await self.tasks.append_event(
            task_id, TaskStage.REPORT, "stage_exit", "排查结论已回传群聊"
        )

    async def _pre_review(self, task: Task, diff: str, summary: str) -> str:
        """轻量预评审：直接把执行循环的汇总作为评审要点返回（verify.py 一期后补独立评审）。"""
        return summary or "（执行循环未产出汇总）"

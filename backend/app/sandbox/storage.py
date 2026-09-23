"""Task / TaskEvent 持久化与 TaskService（ADR-004 §2）。

独立于记忆层的 DeclarativeBase，但共用同一个 SQLite 库（memory_db_url），
复用其轻量列迁移惯例（create_all 不会给已有表加列）。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path

from sqlalchemy import Float, Index, Integer, String, Text, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import settings
from app.sandbox.types import Task, TaskEvent, TaskStage, TaskStatus

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class TaskRow(Base):
    __tablename__ = "sandbox_tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(Text, default="")
    instruction: Mapped[str] = mapped_column(Text, default="")
    repo: Mapped[str] = mapped_column(String(128), default="")
    chat_id: Mapped[str] = mapped_column(String(128), default="")
    session_id: Mapped[str] = mapped_column(String(64), default="")
    created_by: Mapped[str] = mapped_column(String(128), default="")
    trace_id: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default=TaskStatus.PENDING.value)
    branch_name: Mapped[str] = mapped_column(String(256), default="")
    base_ref: Mapped[str] = mapped_column(String(128), default="")
    commits_json: Mapped[str] = mapped_column(Text, default="[]")
    verify_result: Mapped[str] = mapped_column(String(32), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[float] = mapped_column(Float, default=0.0)


class TaskEventRow(Base):
    __tablename__ = "sandbox_task_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), default="")
    seq: Mapped[int] = mapped_column(Integer, default=0)
    stage: Mapped[str] = mapped_column(String(32), default="")
    kind: Mapped[str] = mapped_column(String(32), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    trace_id: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[float] = mapped_column(Float, default=0.0)

    __table_args__ = (Index("ix_task_events_task_seq", "task_id", "seq"),)


class TaskService:
    """Task 持久化、状态流转与事件追加。纯存储语义，不含队列/容器逻辑。"""

    def __init__(self, db_url: str | None = None) -> None:
        resolved = db_url or settings.memory_db_url
        parsed = make_url(resolved)
        if parsed.get_backend_name() == "sqlite" and parsed.database not in (None, ":memory:"):
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_async_engine(resolved, echo=False)
        self._session = async_sessionmaker(self._engine, class_=AsyncSession, expire_on_commit=False)

    async def init(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self._engine.dispose()

    # ---- 创建与查询 ----

    async def create(self, draft: dict) -> str:
        """落库 status=pending，返回 task_id。签名对齐 ConsoleAgent 的 DelegateFn。"""
        task_id = uuid.uuid4().hex[:8]
        now = time.time()
        async with self._session() as s:
            s.add(
                TaskRow(
                    id=task_id,
                    title=draft.get("title", ""),
                    instruction=draft.get("instruction", ""),
                    repo=draft.get("repo", ""),
                    chat_id=draft.get("chat_id", ""),
                    session_id=draft.get("session_id", ""),
                    created_by=draft.get("created_by", ""),
                    trace_id=draft.get("trace_id", ""),
                    status=TaskStatus.PENDING.value,
                    created_at=now,
                    updated_at=now,
                )
            )
            await s.commit()
        log.info("task created: id=%s repo=%s title=%s", task_id, draft.get("repo"), draft.get("title"))
        return task_id

    async def get(self, task_id: str) -> Task | None:
        async with self._session() as s:
            row = await s.get(TaskRow, task_id)
            return _task_from_row(row) if row else None

    async def list_by_chat(self, chat_id: str, *, limit: int = 10) -> list[Task]:
        async with self._session() as s:
            rows = (
                await s.execute(
                    select(TaskRow)
                    .where(TaskRow.chat_id == chat_id)
                    .order_by(TaskRow.created_at.desc())
                    .limit(limit)
                )
            ).scalars().all()
            return [_task_from_row(r) for r in rows]

    # ---- 状态流转 ----

    async def set_status(self, task_id: str, status: TaskStatus, *, error: str = "") -> None:
        values: dict = {"status": status.value, "updated_at": time.time()}
        if error:
            values["error"] = error
        async with self._session() as s:
            await s.execute(update(TaskRow).where(TaskRow.id == task_id).values(**values))
            await s.commit()

    async def set_running(self, task_id: str, *, branch_name: str, base_ref: str) -> None:
        async with self._session() as s:
            await s.execute(
                update(TaskRow)
                .where(TaskRow.id == task_id)
                .values(
                    status=TaskStatus.RUNNING.value,
                    branch_name=branch_name,
                    base_ref=base_ref,
                    updated_at=time.time(),
                )
            )
            await s.commit()

    async def set_review(
        self, task_id: str, *, commits: list[dict], verify_result: str = ""
    ) -> None:
        async with self._session() as s:
            await s.execute(
                update(TaskRow)
                .where(TaskRow.id == task_id)
                .values(
                    status=TaskStatus.REVIEW.value,
                    commits_json=json.dumps(commits, ensure_ascii=False),
                    verify_result=verify_result,
                    updated_at=time.time(),
                )
            )
            await s.commit()

    async def finish(self, task_id: str, status: TaskStatus, *, verify_result: str = "", error: str = "") -> None:
        """终态落库（done / failed / canceled）。"""
        values: dict = {
            "status": status.value,
            "updated_at": time.time(),
        }
        if verify_result:
            values["verify_result"] = verify_result
        if error:
            values["error"] = error
        async with self._session() as s:
            await s.execute(update(TaskRow).where(TaskRow.id == task_id).values(**values))
            await s.commit()

    # ---- TaskEvent ----

    async def append_event(
        self, task_id: str, stage: TaskStage | str, kind: str, summary: str, *, trace_id: str = ""
    ) -> None:
        """只追加事件。seq 服务端分配（同 task 内自增）。"""
        async with self._session() as s:
            last = (
                await s.execute(
                    select(TaskEventRow.seq)
                    .where(TaskEventRow.task_id == task_id)
                    .order_by(TaskEventRow.seq.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            s.add(
                TaskEventRow(
                    id=uuid.uuid4().hex,
                    task_id=task_id,
                    seq=(last or 0) + 1,
                    stage=stage.value if isinstance(stage, TaskStage) else stage,
                    kind=kind,
                    summary=summary,
                    trace_id=trace_id,
                    created_at=time.time(),
                )
            )
            await s.commit()

    async def events(self, task_id: str) -> list[TaskEvent]:
        async with self._session() as s:
            rows = (
                await s.execute(
                    select(TaskEventRow)
                    .where(TaskEventRow.task_id == task_id)
                    .order_by(TaskEventRow.seq.asc())
                )
            ).scalars().all()
            return [
                TaskEvent(
                    stage=r.stage,
                    kind=r.kind,
                    summary=r.summary,
                    trace_id=r.trace_id,
                    seq=int(r.seq),
                    created_at=r.created_at,
                )
                for r in rows
            ]

    # ---- 重启恢复（定案：pending 续跑，running 标 failed）----

    async def recover(self) -> list[str]:
        """服务重启时调用。返回应重新入队的 task_id 列表（status=pending）。"""
        requeue: list[str] = []
        async with self._session() as s:
            rows = (await s.execute(select(TaskRow).where(TaskRow.status == TaskStatus.PENDING.value))).scalars().all()
            requeue = [r.id for r in rows]

            stale = (
                await s.execute(
                    select(TaskRow).where(
                        TaskRow.status.in_([TaskStatus.RUNNING.value, TaskStatus.REVIEW.value])
                    )
                )
            ).scalars().all()
            for r in stale:
                r.status = TaskStatus.FAILED.value
                r.error = "服务重启导致中断，已产生的提交见任务分支"
                r.updated_at = time.time()
            await s.commit()
        if requeue:
            log.info("recovered pending tasks: %s", requeue)
        if stale:
            log.warning("marked interrupted tasks as failed: %s", [r.id for r in stale])
        return requeue


def _task_from_row(row: TaskRow) -> Task:
    return Task(
        id=row.id,
        title=row.title,
        instruction=row.instruction,
        repo=row.repo,
        chat_id=row.chat_id,
        session_id=row.session_id,
        created_by=row.created_by,
        trace_id=row.trace_id,
        status=TaskStatus(row.status),
        branch_name=row.branch_name,
        base_ref=row.base_ref,
        commits=json.loads(row.commits_json or "[]"),
        verify_result=row.verify_result,
        error=row.error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )

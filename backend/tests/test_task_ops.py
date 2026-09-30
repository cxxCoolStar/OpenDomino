"""TaskOpHandler 测试（ADR-010）：进展查询格式化、steer/cancel 校验与下发。

TaskService 用内存 SQLite 真实例，TaskControl 用真实控制面；
只 fake 掉跨层依赖（无 Docker、无 LLM、无群通知）。
"""
from __future__ import annotations

import time

import pytest

from app.console.task_ops import TaskOpHandler
from app.sandbox.control import TaskControl
from app.sandbox.storage import TaskService
from app.sandbox.types import TaskStage, TaskStatus


async def _make_svc(tmp_path) -> TaskService:
    svc = TaskService(db_url=f"sqlite+aiosqlite:///{tmp_path}/t.db")
    await svc.init()
    return svc


async def _mk_task(svc: TaskService, chat_id: str = "c1", **kw) -> str:
    return await svc.create({
        "title": kw.get("title", "排查登录超时"),
        "instruction": kw.get("instruction", "查一下"),
        "repo": kw.get("repo", "demo"),
        "type": kw.get("type", "investigate"),
        "chat_id": chat_id,
        "session_id": "s1",
        "created_by": "u1",
    })


@pytest.mark.asyncio
async def test_status_lists_chat_tasks(tmp_path):
    svc = await _make_svc(tmp_path)
    try:
        t1 = await _mk_task(svc)
        t2 = await _mk_task(svc)
        await svc.set_status(t1, TaskStatus.RUNNING)
        await svc.append_event(t1, TaskStage.EXECUTE, "tool_summary", "list_dir app/")
        handler = TaskOpHandler(svc, TaskControl())
        out = await handler.handle("c1", "status", "", "")
        assert f"#{t1[:8]}" in out and f"#{t2[:8]}" in out
        assert "执行中" in out and "list_dir app/" not in out   # 列表不带事件明细
    finally:
        await svc.close()


@pytest.mark.asyncio
async def test_status_single_task_with_events_and_stale(tmp_path, monkeypatch):
    svc = await _make_svc(tmp_path)
    try:
        tid = await _mk_task(svc)
        await svc.set_status(tid, TaskStatus.RUNNING)
        await svc.append_event(tid, TaskStage.EXECUTE, "tool_summary", "run_command pytest")
        # 伪造停滞：updated_at 拨回 20 分钟前
        from sqlalchemy import update

        from app.sandbox.storage import TaskRow
        async with svc._session() as s:
            await s.execute(update(TaskRow).where(TaskRow.id == tid).values(updated_at=time.time() - 1200))
            await s.commit()

        handler = TaskOpHandler(svc, TaskControl())
        out = await handler.handle("c1", "status", f"#{tid[:8]}", "")
        assert "run_command pytest" in out            # 单任务带最近动态
        assert "停滞" in out                          # 超过 stale 阈值提示
    finally:
        await svc.close()


@pytest.mark.asyncio
async def test_status_rejects_other_chat_task(tmp_path):
    svc = await _make_svc(tmp_path)
    try:
        tid = await _mk_task(svc, chat_id="c-other")
        handler = TaskOpHandler(svc, TaskControl())
        out = await handler.handle("c1", "status", tid, "")
        assert "没有找到" in out                       # 跨群隔离
    finally:
        await svc.close()


@pytest.mark.asyncio
async def test_steer_requires_running_and_delivers(tmp_path):
    svc = await _make_svc(tmp_path)
    try:
        tid = await _mk_task(svc)
        await svc.set_status(tid, TaskStatus.RUNNING)
        control = TaskControl()
        await control.bind(tid)
        handler = TaskOpHandler(svc, control)

        # 空内容拒绝
        out = await handler.handle("c1", "steer", tid, "")
        assert "说清楚" in out
        # 正常下发
        out = await handler.handle("c1", "steer", tid, "只看 auth 模块")
        assert "下一步" in out
        assert await control.drain_steer(tid) == ["只看 auth 模块"]
        assert (await svc.events(tid))[-1].kind == "steer_received"
        # 终态任务拒绝
        await svc.finish(tid, TaskStatus.DONE)
        out = await handler.handle("c1", "steer", tid, "再来一条")
        assert "不在执行中" in out
    finally:
        await svc.close()


@pytest.mark.asyncio
async def test_cancel_covers_all_states(tmp_path):
    svc = await _make_svc(tmp_path)
    try:
        control = TaskControl()
        handler = TaskOpHandler(svc, control)

        # pending：直接落终态
        t_pending = await _mk_task(svc)
        out = await handler.handle("c1", "cancel", t_pending, "")
        assert "排队中" in out
        assert (await svc.get(t_pending)).status == TaskStatus.CANCELED

        # running：置协作标志
        t_running = await _mk_task(svc)
        await svc.set_status(t_running, TaskStatus.RUNNING)
        await control.bind(t_running)
        out = await handler.handle("c1", "cancel", t_running, "")
        assert "停止" in out
        assert await control.cancel_requested(t_running) is True

        # done：拒绝
        t_done = await _mk_task(svc)
        await svc.finish(t_done, TaskStatus.DONE)
        out = await handler.handle("c1", "cancel", t_done, "")
        assert "终态" in out

        # review：引导不合入
        t_rev = await _mk_task(svc)
        await svc.set_review(t_rev, commits=[{"sha": "a", "message": "m"}])
        out = await handler.handle("c1", "cancel", t_rev, "")
        assert "评审" in out
    finally:
        await svc.close()


@pytest.mark.asyncio
async def test_invalid_task_id_format(tmp_path):
    svc = await _make_svc(tmp_path)
    try:
        handler = TaskOpHandler(svc, TaskControl())
        out = await handler.handle("c1", "steer", "zzzz!!", "x")   # 非 8 位 hex
        assert "任务号" in out
    finally:
        await svc.close()

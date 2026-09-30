"""OpenDomino API 与飞书长连接运行入口（ADR-008）。"""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app.channels.feishu import FeishuChannel
from app.channels.registry import ChannelRegistry
from app.config import settings
from app.console.agent import ConsoleAgent
from app.console.distiller import Distiller
from app.console.llm import OpenAIClient
from app.console.router import Router
from app.console.task_ops import TaskOpHandler
from app.memory.service import SQLiteMemoryService
from app.sandbox.agent import SandboxAgent
from app.sandbox.container import ContainerPool
from app.sandbox.control import TaskControl
from app.sandbox.executor import TaskExecutor
from app.sandbox.llm import OpenAIChatClient
from app.sandbox.pipeline import TaskPipeline
from app.sandbox.reporter import TaskReporter
from app.sandbox.storage import TaskService

logging.basicConfig(level=logging.INFO)
# 任务失败往往只有日志能定位（如 #dccfa430 空响应事故），前台进程日志必须落盘：
# 控制台照常输出，文件 5MB 轮转保 5 份（约 25MB 上限）
log = logging.getLogger("opendomino")


def _setup_file_logging() -> None:
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "opendomino.log", maxBytes=5 * 1024 * 1024,
        backupCount=5, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger().addHandler(handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _setup_file_logging()
    if not settings.llm_api_key:
        raise RuntimeError("LLM_API_KEY is required")
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        raise RuntimeError("FEISHU_APP_ID and FEISHU_APP_SECRET are required")

    memory = SQLiteMemoryService()
    await memory.init()
    llm = OpenAIClient()
    router = Router(llm)

    channel: FeishuChannel

    async def reply(chat_id: str, text: str, session_webhook: str) -> None:
        result = await channel.reply(chat_id, text)
        if not result.ok:
            log.error("Feishu reply failed chat_id=%s error=%s", chat_id, result.error)

    async def on_message(msg) -> None:
        await agent.handle(msg)

    async def save_observed(chat_id: str, msgs) -> None:
        """旁听消息落 L2（存而不检索，ADR-008）。"""
        try:
            added = await memory.save_observed(
                chat_id,
                [
                    _observed_to_chat_message(m)
                    for m in msgs
                ],
            )
            if added:
                log.debug("observed saved chat_id=%s added=%d", chat_id, added)
        except Exception:  # noqa: BLE001 —— 旁听落库失败不影响主管线
            log.exception("save_observed failed chat_id=%s", chat_id)

    # ---- Sandbox 侧装配（ADR-004；ADR-010 管控面）----
    tasks = TaskService()
    await tasks.init()
    registry = ChannelRegistry()
    reporter = TaskReporter(registry)
    pool = ContainerPool()
    pool.start_sweeper()
    sandbox_agent = SandboxAgent(OpenAIChatClient())
    control = TaskControl()
    # ADR-002 Phase B：任务完成提炼（与蒸馏/消息管线共享 per-chat 锁与回复通道）
    task_distiller = None
    if settings.task_distill_enabled:
        from app.console.task_distill import TaskDistiller

        task_distiller = TaskDistiller(
            memory,
            llm,
            chat_locks=None,    # 装配后回填：ConsoleAgent 构造晚于 pipeline
            reply=lambda chat_id, text: reply(chat_id, text, ""),
        )
    pipeline = TaskPipeline(tasks, pool, sandbox_agent, reporter, memory=memory, control=control,
                            task_distiller=task_distiller)
    executor = TaskExecutor(pipeline.run_task)

    # 重启恢复：pending 续跑，running/review 标失败（详细设计 §2.3）
    requeue = await tasks.recover()
    if requeue:
        log.info("requeue tasks after restart: %s", requeue)
        for tid in requeue:
            t = await tasks.get(tid)
            if t is not None:
                await executor.submit(tid, t.repo)

    async def delegate(draft: dict) -> str:
        """ConsoleAgent 的 DelegateFn：创建任务 → 入队 → 群内受理通知。"""
        task_id = await tasks.create(draft)
        await executor.submit(task_id, draft.get("repo", ""))
        await reporter.accepted(await tasks.get(task_id))
        return task_id

    # ADR-010：任务查询与中途管控（status/steer/cancel）的确定性执行入口
    task_op_handler = TaskOpHandler(tasks, control)

    async def task_op(chat_id: str, op: str, task_id: str, message: str) -> str:
        """ConsoleAgent 的 TaskOpFn：校验（归属/状态）在 handler 内，这里只转发。"""
        return await task_op_handler.handle(chat_id, op, task_id, message)

    agent = ConsoleAgent(memory=memory, router=router, reply=reply, delegate=delegate, task_op=task_op)
    # Phase B 提炼与消息管线共用 per-chat 锁（agent 构造后回填）
    if task_distiller is not None:
        task_distiller._chat_locks = agent.chat_locks()

    # ADR-010：上下文状态段活化——装配器经此闭包把裸任务 ID 渲染为实时状态
    async def task_status_section(task_ids: list[str]) -> str:
        lines: list[str] = []
        for tid in task_ids:
            t = await tasks.get(tid)
            if t is None:
                continue
            icon = {"pending": "排队中", "running": "执行中", "review": "待评审",
                    "done": "已完成", "failed": "失败", "canceled": "已取消"}.get(t.status.value, t.status.value)
            cost = ""
            if t.created_at:
                seconds = max(0, int((t.updated_at or 0) - t.created_at))
                cost = f"，已 {seconds // 60} 分钟" if seconds >= 60 else f"，已 {seconds} 秒"
            last_event = await tasks.recent_events(tid, limit=1)
            recent = f"；最近：{last_event[-1].summary[:40]}" if last_event else ""
            lines.append(f"# {tid[:8]} {icon}{cost}{recent}")
        return "[进行中任务]\n" + "\n".join(lines) if lines else ""

    agent._assembler.set_task_status_fn(task_status_section)
    channel = FeishuChannel(on_message=on_message, save_observed=save_observed)
    # channel 需先创建再注册进 registry（reply 闭包已持有引用）
    registry.register(channel)

    # 旁听蒸馏扫描器（ADR-009）：与消息管线共享 per-chat 锁与回复通道
    distiller = Distiller(
        memory,
        llm,
        chat_locks=agent.chat_locks(),
        reply=lambda chat_id, text: reply(chat_id, text, ""),
    )

    app.state.memory = memory
    app.state.console_agent = agent
    app.state.feishu_channel = channel
    app.state.task_service = tasks
    app.state.task_executor = executor
    app.state.task_control = control
    app.state.container_pool = pool

    # 知识库文档自动同步（ADR-011）：启动先跑一轮，之后周期轮询；独立 KB 实例
    syncer = None
    if settings.kbsync_enabled:
        from app.docs.knowledge import KnowledgeBase
        from app.docs.syncer import build_default_syncer

        sync_kb = KnowledgeBase()
        await sync_kb.init()
        syncer = build_default_syncer(sync_kb)
    if syncer is not None:
        syncer.start()
    app.state.kb_syncer = syncer

    ws_task = asyncio.create_task(channel.start(), name="feishu-ws")
    log.info("OpenDomino started; Feishu WS connecting")

    # 重启补拉：对已知群幂等回拉历史（ADR-008 旁听决策 4）
    backfill_task = asyncio.create_task(_backfill_known_chats(channel, memory), name="feishu-backfill")

    # 蒸馏器首扫必须晚于回拉落库（ADR-009 首见决议）：首见锚点取「表内最新
    # received_at」，回拉未落库时锚点早于回拉消息的消息时间戳，存量会被当作
    # 未蒸馏范围反复重蒸（2026-09-30 事故根因 2 的放大器）。
    if settings.distiller_enabled:
        async def _start_distiller_after_backfill() -> None:
            await backfill_task
            distiller.start()

        distiller_starter_task = asyncio.create_task(
            _start_distiller_after_backfill(), name="distiller-starter"
        )
    else:
        distiller_starter_task = None

    try:
        yield
    finally:
        await distiller.stop()
        if syncer is not None:
            await syncer.stop()
        for t in (ws_task, backfill_task, distiller_starter_task):
            if t is None:
                continue
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await executor.drain()
        await executor.close()
        await pool.close()
        await tasks.close()
        await channel.stop()
        await memory._engine.dispose()


async def _backfill_known_chats(channel: FeishuChannel, memory: SQLiteMemoryService) -> None:
    """重启补拉：旁听消息表出现过的群 + 已绑定仓库的群，按回拉时间窗幂等补拉。"""
    import time

    try:
        chats = set(await memory.list_observed_chats())
        chats.update(await memory.list_binding_chats())
        since = time.time() - settings.feishu_backfill_days * 86400 if settings.feishu_backfill_days > 0 else 0.0
        for chat_id in sorted(chats):
            await channel.backfill(chat_id, since=since)
        await memory.prune_observed(
            retain_days=settings.feishu_observe_retain_days,
            respect_watermark=settings.distiller_enabled,
        )
    except Exception:  # noqa: BLE001 —— 补拉失败不阻塞主流程
        log.exception("feishu backfill on startup failed")


def _observed_to_chat_message(m):
    from app.memory.types import ChatMessage

    return ChatMessage(
        role="user", text=m.text, user_id=m.user_id,
        user_name=m.user_name, message_id=m.message_id, timestamp=m.timestamp,
    )


app = FastAPI(title="OpenDomino", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    return {
        "status": "ready" if hasattr(app.state, "console_agent") else "starting",
        "channel": "feishu-ws",
    }

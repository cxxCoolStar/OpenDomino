"""OpenDomino API 与飞书长连接运行入口（ADR-008）。"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.channels.feishu import FeishuChannel
from app.channels.registry import ChannelRegistry
from app.config import settings
from app.console.agent import ConsoleAgent
from app.console.llm import OpenAIClient
from app.console.router import Router
from app.memory.service import SQLiteMemoryService
from app.sandbox.agent import SandboxAgent
from app.sandbox.container import ContainerPool
from app.sandbox.executor import TaskExecutor
from app.sandbox.llm import OpenAIChatClient
from app.sandbox.pipeline import TaskPipeline
from app.sandbox.reporter import TaskReporter
from app.sandbox.storage import TaskService

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("opendomino")


@asynccontextmanager
async def lifespan(app: FastAPI):
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

    # ---- Sandbox 侧装配（ADR-004）----
    tasks = TaskService()
    await tasks.init()
    registry = ChannelRegistry()
    reporter = TaskReporter(registry)
    pool = ContainerPool()
    pool.start_sweeper()
    sandbox_agent = SandboxAgent(OpenAIChatClient())
    pipeline = TaskPipeline(tasks, pool, sandbox_agent, reporter, memory=memory)
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

    agent = ConsoleAgent(memory=memory, router=router, reply=reply, delegate=delegate)
    channel = FeishuChannel(on_message=on_message, save_observed=save_observed)
    # channel 需先创建再注册进 registry（reply 闭包已持有引用）
    registry.register(channel)

    app.state.memory = memory
    app.state.console_agent = agent
    app.state.feishu_channel = channel
    app.state.task_service = tasks
    app.state.task_executor = executor
    app.state.container_pool = pool
    ws_task = asyncio.create_task(channel.start(), name="feishu-ws")
    log.info("OpenDomino started; Feishu WS connecting")

    # 重启补拉：对已知群幂等回拉历史（ADR-008 旁听决策 4）
    backfill_task = asyncio.create_task(_backfill_known_chats(channel, memory), name="feishu-backfill")

    try:
        yield
    finally:
        for t in (ws_task, backfill_task):
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
        await memory.prune_observed(retain_days=settings.feishu_observe_retain_days)
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

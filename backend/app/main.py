"""OpenDomino API 与钉钉 Stream 运行入口。"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.channels.dingtalk import DingTalkChannel
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
    if not settings.dingtalk_client_id or not settings.dingtalk_client_secret:
        raise RuntimeError("DINGTALK_CLIENT_ID and DINGTALK_CLIENT_SECRET are required")

    memory = SQLiteMemoryService()
    await memory.init()
    llm = OpenAIClient()
    router = Router(llm)

    channel: DingTalkChannel

    async def reply(chat_id: str, text: str, session_webhook: str) -> None:
        result = await channel.reply(chat_id, text, session_webhook=session_webhook)
        if not result.ok:
            log.error("DingTalk reply failed chat_id=%s error=%s", chat_id, result.error)

    async def on_message(msg) -> None:
        await agent.handle(msg)

    # ---- Sandbox 侧装配（ADR-004）----
    tasks = TaskService()
    await tasks.init()
    registry = ChannelRegistry()
    registry.register(channel)
    reporter = TaskReporter(registry)
    pool = ContainerPool()
    pool.start_sweeper()
    sandbox_agent = SandboxAgent(OpenAIChatClient())
    pipeline = TaskPipeline(tasks, pool, sandbox_agent, reporter)
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
    channel = DingTalkChannel(on_message=on_message)
    # channel 需先创建再注册进 registry（reply 闭包已持有引用）
    registry.register(channel)

    app.state.memory = memory
    app.state.console_agent = agent
    app.state.dingtalk_channel = channel
    app.state.task_service = tasks
    app.state.task_executor = executor
    app.state.container_pool = pool
    stream_task = asyncio.create_task(channel.start(), name="dingtalk-stream")
    log.info("OpenDomino started; DingTalk Stream connecting")
    try:
        yield
    finally:
        stream_task.cancel()
        try:
            await stream_task
        except asyncio.CancelledError:
            pass
        await executor.drain()
        await executor.close()
        await pool.close()
        await tasks.close()
        await channel._http.aclose()
        await memory._engine.dispose()


app = FastAPI(title="OpenDomino", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    return {
        "status": "ready" if hasattr(app.state, "console_agent") else "starting",
        "channel": "dingtalk-stream",
    }

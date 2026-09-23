"""OpenDomino API 与钉钉 Stream 运行入口。"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.channels.dingtalk import DingTalkChannel
from app.config import settings
from app.console.agent import ConsoleAgent
from app.console.llm import OpenAIClient
from app.console.router import Router
from app.memory.service import SQLiteMemoryService

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

    agent = ConsoleAgent(memory=memory, router=router, reply=reply)
    channel = DingTalkChannel(on_message=on_message)

    app.state.memory = memory
    app.state.console_agent = agent
    app.state.dingtalk_channel = channel
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

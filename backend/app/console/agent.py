"""ConsoleAgent 主管线（ADR-003）：会话恢复→装配→路由→动作→持久化，群级串行锁。"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from app.channels.base import UnifiedMessage
from app.config import settings
from app.console.context import ContextAssembler
from app.console.router import Router
from app.console.router_types import Action, PendingResolution
from app.memory.types import ChatMessage, MemoryService, PendingState

log = logging.getLogger(__name__)

ReplyFn = Callable[[str, str, str], Awaitable[None]]    # (chat_id, text, session_webhook)
DelegateFn = Callable[[dict], Awaitable[str]]            # task_draft -> task_id


class ConsoleAgent:
    def __init__(
        self,
        memory: MemoryService,
        router: Router,
        reply: ReplyFn,
        delegate: DelegateFn | None = None,
    ) -> None:
        self._memory = memory
        self._router = router
        self._reply = reply
        self._delegate = delegate
        self._assembler = ContextAssembler(memory)
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, chat_id: str) -> asyncio.Lock:
        if chat_id not in self._locks:
            self._locks[chat_id] = asyncio.Lock()
        return self._locks[chat_id]

    async def handle(self, msg: UnifiedMessage) -> None:
        async with self._lock(msg.chat_id):
            try:
                await self._pipeline(msg)
            except Exception:  # noqa: BLE001
                log.exception("console pipeline failed for chat %s", msg.chat_id)
                await self._reply(msg.chat_id, "处理时出错了，请稍后再试。", msg.session_webhook)

    async def _pipeline(self, msg: UnifiedMessage) -> None:
        session = await self._memory.load_session(msg.chat_id)

        # 追加用户消息
        session.messages.append(
            ChatMessage(
                role="user", text=msg.text, user_id=msg.user_id,
                user_name=msg.user_name, message_id=msg.message_id,
            )
        )

        context = await self._assembler.assemble(session, msg.text)
        decision = await self._router.route(context, msg.text)

        # 消费 pending 状态
        self._resolve_pending(session, decision.pending_resolution)

        # 动作执行
        reply_text = ""
        if decision.action == Action.ANSWER:
            reply_text = decision.answer
        elif decision.action == Action.CLARIFY:
            session.pending = PendingState(
                kind="clarify", payload={"question": decision.clarify_question}
            )
            reply_text = decision.clarify_question
        elif decision.action == Action.DELEGATE:
            reply_text = await self._do_delegate(session, msg, decision)

        if reply_text:
            await self._reply(msg.chat_id, reply_text, msg.session_webhook)
            session.messages.append(ChatMessage(role="assistant", text=reply_text))

        # 持久化
        session.last_route = {"action": decision.action.value, "ts": time.time()}
        await self._memory.save_session(session)

    @staticmethod
    def _resolve_pending(session, resolution: PendingResolution) -> None:
        if session.pending is None:
            return
        if resolution in (PendingResolution.ANSWERED, PendingResolution.NEW_QUESTION, PendingResolution.CANCEL):
            session.pending = None
        # NONE 时保留 pending（用户没正面回应，留待下一条）

    async def _do_delegate(self, session, msg: UnifiedMessage, decision) -> str:
        task = decision.task
        if task is None or not task.instruction:
            return "我需要创建任务，但任务信息不完整，能再说清楚要做什么吗？"
        if self._delegate is None:
            return "任务执行功能尚未就绪（SandboxAgent 未接入）。"
        draft = {
            "title": task.title or msg.text[:30],
            "instruction": task.instruction,
            "repo": task.repo,
            "chat_id": msg.chat_id,
            "session_id": session.session_id,
            "created_by": msg.user_id,
        }
        task_id = await self._delegate(draft)
        session.active_task_ids.append(task_id)
        return f"已受理，任务 #{task_id[:8]}：{draft['title']}。完成后我会在群里同步。"

    # 待澄清超时被动清理（下一条消息到达时调用）
    @staticmethod
    def _expire_pending(session) -> None:
        if session.pending and session.pending.kind == "clarify":
            ttl = settings.console_clarify_ttl_minutes * 60
            if time.time() - session.pending.created_at > ttl:
                session.pending = None

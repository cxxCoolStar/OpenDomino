"""ConsoleAgent 主管线（ADR-003）：会话恢复→状态检查→确认流拦截→装配→路由→动作→持久化，群级串行锁。"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from app.channels.base import UnifiedMessage
from app.config import settings
from app.console.context import ContextAssembler
from app.console.memory_confirm import MemoryConfirmHandler
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
        self._confirm = MemoryConfirmHandler(memory)
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, chat_id: str) -> asyncio.Lock:
        if chat_id not in self._locks:
            self._locks[chat_id] = asyncio.Lock()
        return self._locks[chat_id]

    async def handle(self, msg: UnifiedMessage) -> None:
        lock = self._lock(msg.chat_id)
        if lock.locked():
            # 排队反馈：webhook 仅 5 分钟有效，先确认收到，正式回答走完整管线
            await self._reply(msg.chat_id, "收到，正在处理前面的消息，请稍候。", msg.session_webhook)
        async with lock:
            try:
                await self._pipeline(msg)
            except Exception:  # noqa: BLE001
                log.exception("console pipeline failed for chat %s", msg.chat_id)
                await self._reply(
                    msg.chat_id, "模型服务暂时不可用，请稍后再试。", msg.session_webhook
                )

    async def _pipeline(self, msg: UnifiedMessage) -> None:
        session = await self._memory.load_session(msg.chat_id)
        self._expire_pending(session)

        # 记忆确认流拦截（确定性，@ 门控；消费后不走路由器）
        if await self._confirm.try_handle(msg, session, self._reply):
            session.last_route = {"action": "memory_confirm", "ts": time.time()}
            await self._memory.save_session(session)
            return

        # 追加用户消息
        session.messages.append(
            ChatMessage(
                role="user", text=msg.text, user_id=msg.user_id,
                user_name=msg.user_name, message_id=msg.message_id,
            )
        )

        # 滚动压缩（Phase A）：溢出消息在路由前压进摘要
        from app.console.compaction import maybe_compact
        await maybe_compact(session, self._router.llm)

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
                kind="clarify",
                payload={
                    "question": decision.clarify_question,
                    "asked_user_id": msg.user_id,       # 多人场景：澄清归属（并发修复 3）
                    "asked_user_name": msg.user_name,
                },
            )
            reply_text = decision.clarify_question
        elif decision.action == Action.DELEGATE:
            reply_text = await self._do_delegate(session, msg, decision)

        # 纠正提炼（Phase A）：用户纠正 → 自动建草稿进入确认流（复用 Phase 0 状态机）
        if (
            decision.correction_detected
            and decision.action == Action.ANSWER
            and session.pending_memory is None          # 已有待确认时不叠加（T2 同规则）
        ):
            reply_text = await self._draft_correction(session, msg, decision, reply_text)

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

    async def _draft_correction(self, session, msg: UnifiedMessage, decision, reply_text: str) -> str:
        """纠正提炼：从纠正消息建草稿，挂 pending_memory，回复末尾附确认提示。"""
        from app.memory.types import MemoryDraft

        fact = self._extract_correction_fact(decision.answer)
        if not fact:
            return reply_text
        draft = MemoryDraft(
            title=fact[:30],
            statement=fact,
            memory_type="decision",
            source_session_id=session.session_id,
        )
        memory_id = await self._memory.create_memory_draft(draft)
        session.pending_memory = PendingState(
            kind="memory_confirm", payload={"memory_id": memory_id}
        )
        log.info("correction drafted: chat=%s memory=%s", msg.chat_id, memory_id)
        return reply_text

    @staticmethod
    def _extract_correction_fact(answer: str) -> str:
        """从回答中取纠正后的事实：优先「（待确认）」标注句，否则取首段。"""
        for line in answer.splitlines():
            if "待确认" in line or "团队" in line:
                cleaned = line.strip().lstrip("-*1234567890.、 【】").strip()
                if cleaned:
                    return cleaned[:200]
        first = answer.strip().splitlines()[0] if answer.strip() else ""
        return first[:200]

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
            "type": task.type if task.type in ("investigate", "modify") else "investigate",
            "chat_id": msg.chat_id,
            "session_id": session.session_id,
            "created_by": msg.user_id,
        }
        task_id = await self._delegate(draft)
        session.active_task_ids.append(task_id)
        return f"已受理，任务 #{task_id[:8]}：{draft['title']}。完成后我会在群里同步。"

    # 待处理状态超时被动清理（下一条消息到达时调用）
    @staticmethod
    def _expire_pending(session) -> None:
        ttl = settings.console_clarify_ttl_minutes * 60
        if session.pending and session.pending.kind == "clarify":
            if time.time() - session.pending.created_at > ttl:
                session.pending = None
        # pending_memory 的 TTL 由确认流拦截器在匹配时惰性检查

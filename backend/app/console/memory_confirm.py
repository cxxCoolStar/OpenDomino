"""记忆写入确认流（ADR-002 Phase 0）。

状态机：IDLE → AWAITING_CONFIRM → IDLE，核心状态存 Session.pending_memory（kind=memory_confirm），
草稿行在 KBase（status=pending）。设计要点：

- 确定性拦截，不走 LLM 路由：写入团队共享记忆是安全门，不能交给可误判的模型
  分类（依据 cumora COORDINATION.md 元原则：能用代码机制解决的绝不加 prompt 规则）；
- 所有记忆操作必须 @ 机器人：普通群里 require_mention 已保证；免 @ 群里靠
  is_at_bot 区分「对机器人说确认」和「成员闲聊说确认」，防止误写；
- 钉钉 @ 消息的 text 可能带「@机器人名 」前缀，匹配前必须归一化剥离；
  确认/取消精确匹配还剥离句中所有 @token（「取消@Domino」句尾 @ 粘连；
  2026-09-30 事故：失配落路由器后被 LLM 代答「已取消」，草稿实际未删）；
- 确认词在 clarify 槽占用时不拦截：此时「确认/取消」大概率在回应澄清问题，
  交给路由器判定（T5 例外）；
- 拒绝/过期物理删除草稿：不留下半条记忆（ADR-002 写入治理）。
"""
from __future__ import annotations

import logging
import re
import time

from app.channels.base import UnifiedMessage
from app.config import settings
from app.memory.types import MemoryDraft, PendingState, SessionContext

log = logging.getLogger(__name__)

_MEM_CONFIRM = "memory_confirm"


def _split_keywords(raw: str) -> list[str]:
    return [k.strip() for k in (raw or "").split(",") if k.strip()]


def confirm_keywords() -> list[str]:
    return _split_keywords(settings.memory_confirm_keywords)


def cancel_keywords() -> list[str]:
    return _split_keywords(settings.memory_cancel_keywords)


_AT_PREFIX_RE = re.compile(r"^(?:@\S+\s*)+")
_AT_TOKEN_RE = re.compile(r"@\S+")


def normalize_text(text: str) -> str:
    """剥离开头的 @ 前缀并去除首尾空白。

    群聊 @机器人 时 text.content 常带「@机器人名 」前缀，不剥离则精确匹配永远失配。
    """
    return _AT_PREFIX_RE.sub("", (text or "").strip()).strip()


def normalize_for_match(text: str) -> str:
    """确认/取消精确匹配专用归一化：剥离消息中所有 @mention token。

    群友习惯把 @ 放句尾（「取消@Domino」），@ 与关键词粘连会让精确匹配失配，
    消息落到 LLM 路由器后被代答「已取消」而草稿不动（2026-09-30 事故根因 1）。
    @token 在确认/取消这类短语里只可能是点名机器人，剥离不改变语义；
    「记住：」「/沉淀」前缀识别仍用 normalize_text，避免误删正文中的 @。
    """
    return normalize_text(_AT_TOKEN_RE.sub(" ", (text or "")))


def normalize_remember(text: str) -> str | None:
    """识别「记住：<内容>」直达指令，返回内容；非该指令返回 None。"""
    t = normalize_text(text)
    for prefix in ("记住：", "记住:"):
        if t.startswith(prefix):
            content = t[len(prefix):].strip()
            return content or None
    return None


def normalize_deposit(text: str) -> str | None:
    """识别「/沉淀 <内容>」指令，返回内容；非该指令返回 None。"""
    t = normalize_text(text)
    for prefix in ("/沉淀", "沉淀：", "沉淀:"):
        if t.startswith(prefix):
            content = t[len(prefix):].strip()
            return content or None
    return None


def _is_exact(text: str, keywords: list[str], *, match=normalize_text) -> bool:
    return match(text) in keywords


def _free_response_chats() -> set[str]:
    return {c.strip() for c in (settings.feishu_free_response_chats or "").split(",") if c.strip()}


class MemoryConfirmHandler:
    """确认流拦截器。pipeline 在调 Router 之前调用 try_handle。"""

    def __init__(self, memory) -> None:
        self._memory = memory

    def _require_at(self, msg: UnifiedMessage) -> bool:
        # require_mention 的普通群，能进 pipeline 的消息必然 @ 过机器人；
        # 免 @ 群里必须显式校验 is_at_bot，防止旁人闲聊「确认」误写团队记忆。
        # 飞书全量接收下（ADR-008）@ 消息与旁听消息在通道层分流，进入管线的
        # 消息必然已过 @ 门控，此处保留 is_at_bot 校验作为免 @ 场景的兜底。
        if settings.feishu_require_mention and msg.chat_id not in _free_response_chats():
            return True
        return msg.is_at_bot

    async def try_handle(self, msg: UnifiedMessage, session: SessionContext, reply) -> bool:
        """返回 True 表示消息被本模块消费（拦截后 pipeline 直接返回）。"""
        if not self._require_at(msg):
            return False

        # ---- 指令入口（不受已有 pending 影响）----
        remember = normalize_remember(msg.text)
        if remember is not None:
            await self._direct_save(msg, session, remember, reply)
            return True
        deposit = normalize_deposit(msg.text)
        if deposit is not None:
            await self._start_confirm(msg, session, deposit, reply)
            return True

        # ---- 确认/取消（仅在 pending_memory 存在时）----
        if session.pending_memory is None or session.pending_memory.kind != _MEM_CONFIRM:
            return False
        # clarify 槽占用时不拦截：用户大概率在回应澄清问题（T5 例外）
        if session.pending is not None:
            return False
        # TTL 过期：惰性清理，消息放行给路由器
        if self._expired(session.pending_memory):
            memory_id = session.pending_memory.payload.get("memory_id", "")
            if memory_id:
                await self._memory.delete_memory(memory_id)
            session.pending_memory = None
            log.info("memory confirm expired: chat=%s memory=%s", msg.chat_id, memory_id)
            return False

        memory_id = session.pending_memory.payload.get("memory_id", "")
        if _is_exact(msg.text, confirm_keywords(), match=normalize_for_match):
            hit = await self._memory.get_memory(memory_id)
            if hit is None:
                session.pending_memory = None
                await reply(msg.chat_id, "这条待确认记忆已失效。", msg.session_webhook)
                return True
            await self._memory.confirm_memory(memory_id, msg.user_id)
            await self._advance_watermark(session)   # 三态语义：确认推进（ADR-009）
            session.pending_memory = None
            log.info("memory confirmed: chat=%s memory=%s by=%s", msg.chat_id, memory_id, msg.user_id)
            await reply(msg.chat_id, f"已保存团队记忆：「{hit.title}」。", msg.session_webhook)
            return True
        if _is_exact(msg.text, cancel_keywords(), match=normalize_for_match):
            await self._memory.delete_memory(memory_id)
            # 三态语义：取消也推进——人已明确拒绝，下个周期不再重提（ADR-009）
            await self._advance_watermark(session)
            session.pending_memory = None
            log.info("memory confirm cancelled: chat=%s memory=%s", msg.chat_id, memory_id)
            await reply(msg.chat_id, "好的，未保存。", msg.session_webhook)
            return True
        # 非确认/取消消息：pending_memory 保留（非阻塞提议），消息放行
        return False

    async def _advance_watermark(self, session: SessionContext) -> None:
        """确认/取消时把蒸馏游标推进到提议携带的目标值（单调 max；ADR-009）。

        人工入口（/沉淀、记住、纠正）的提议 payload 无 watermark_ts，此处为空操作。
        """
        if not hasattr(self._memory, "get_distill_state"):
            return
        target = session.pending_memory.payload.get("watermark_ts") if session.pending_memory else None
        if not target:
            return
        state = await self._memory.get_distill_state(session.chat_id)
        if target > state.watermark_ts:
            state.watermark_ts = float(target)
            await self._memory.save_distill_state(state)
            log.info("watermark advanced: chat=%s -> %.0f", session.chat_id, state.watermark_ts)

    def _expired(self, pending: PendingState) -> bool:
        ttl = settings.memory_confirm_ttl_minutes * 60
        return time.time() - pending.created_at > ttl

    async def _direct_save(self, msg: UnifiedMessage, session: SessionContext, content: str, reply) -> None:
        """记住：直达通道。存原文，直接 confirmed。"""
        draft = MemoryDraft(
            title=content[:30],
            statement=content,
            memory_type="convention",
            source_session_id=session.session_id,
        )
        memory_id = await self._memory.create_memory_draft(draft)
        await self._memory.confirm_memory(memory_id, msg.user_id)
        log.info("memory direct-saved: chat=%s memory=%s", msg.chat_id, memory_id)
        await reply(msg.chat_id, f"已记住：「{content[:50]}」。", msg.session_webhook)

    async def _start_confirm(self, msg: UnifiedMessage, session: SessionContext, content: str, reply) -> None:
        """/沉淀：建草稿并进入 AWAITING_CONFIRM。已有一条待确认时不叠加（T2）。"""
        if session.pending_memory is not None and not self._expired(session.pending_memory):
            await reply(
                msg.chat_id,
                "已有一条待确认记忆，请先回复「确认」或「取消」再沉淀新的。",
                msg.session_webhook,
            )
            return
        draft = MemoryDraft(
            title=content[:30],
            statement=content,
            memory_type="convention",
            source_session_id=session.session_id,
        )
        memory_id = await self._memory.create_memory_draft(draft)
        session.pending_memory = PendingState(
            kind=_MEM_CONFIRM, payload={"memory_id": memory_id}
        )
        log.info("memory draft created: chat=%s memory=%s", msg.chat_id, memory_id)
        await reply(
            msg.chat_id,
            f"我准备保存这条团队记忆：\n「{content}」\n回复「确认」保存，「取消」放弃（{settings.memory_confirm_ttl_minutes} 分钟内有效）。",
            msg.session_webhook,
        )

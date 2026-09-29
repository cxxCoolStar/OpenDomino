"""旁听结论蒸馏服务（ADR-009）。

后台扫描器发现群空闲后，对「水位线之后」的旁听缓冲消息做一次蒸馏 LLM 调用，
产出候选结论草稿写入 pending_memory 槽位，经确认流（ADR-002）落 KBase。

三态水位线语义（ADR-009 核心决议）：
- 确认 → 条目入库，水位线推进（由 memory_confirm 拦截器执行，本模块只在
  pending payload 里携带 watermark_ts 目标值）；
- 取消 → 不入库，水位线推进（同上，人已明确拒绝，不再重提）；
- 过期 → 不入库，水位线不动（草稿由确认流惰性清理物理删除，下周期重蒸重提）。

「无可沉淀」推进水位线：没有提议对象时若不推进，同一范围每冷却周期空转一次。
LLM 异常与解析失败不推进，进失败冷却。

蒸馏的 LLM 调用不持群锁；返回后先做写前校验（本群最后一条旁听消息的本地接收
时间是否变化，变了说明讨论恢复，结果作废），校验通过再拿群锁写 Session。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Protocol

from app.config import settings
from app.memory.types import (
    MEMORY_SOURCE_OVERHEARD,
    ChatMessage,
    MemoryDraft,
    PendingState,
)

log = logging.getLogger(__name__)

_MEM_CONFIRM = "memory_confirm"

# 隐私防线第一层：提示词内置 ADR-002 不存储清单（人审确认流是第二层）
_DISTILL_SYSTEM = """你是团队群的记忆蒸馏助手。群成员在群里自然讨论（不针对你），\
你负责判断这段讨论是否形成了值得长期保存的团队结论。

只从讨论中提取同时满足以下条件的结论：
1. 长期有效（一周后仍然成立），不是一次性安排或临时方案；
2. 自包含（脱离原对话也能读懂，不出现「上面说的」「这个功能」等指代）；
3. 是明确的结论或约定，不是没有定论的讨论、猜测或闲聊；
4. 不含个人隐私和敏感信息；
5. 是团队事实或长期约定，不是具体操作步骤/工作流程。

如果讨论没有形成任何符合条件的结果，返回 {"distilled": false}，不得强行生成。

若结论与「现有团队记忆」中的某条冲突（旧结论已过时），设置 supersedes_id 为该条 id。

输出 JSON（不要输出其他内容）：
{"distilled": true, "title": "主题（30字内）", "statement": "自包含的结论陈述", \
"rationale": "原因或适用条件（可为空）", "memory_type": "decision|convention|troubleshooting|environment", \
"keywords": ["关键词"], "supersedes_id": "替代的旧条目id，无则为空"}"""


class DistillLLM(Protocol):
    async def complete(self, system: str, user: str, *, model: str | None = None) -> str: ...


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
# 惰性版本：用于从拼接/带杂讯的输出中逐个抓取候选 JSON 对象
_JSON_LAZY_RE = re.compile(r"\{.*?\}", re.DOTALL)


def _first_json_object(text: str) -> str | None:
    """从可能拼接了多个 JSON 对象的文本中抓出第一个可解析的完整对象。"""
    for m in _JSON_LAZY_RE.finditer(text):
        candidate = m.group(0)
        try:
            json.loads(candidate)
            return candidate
        except ValueError:
            continue
    # 兜底：贪婪匹配（对象内嵌套大括号时惰性匹配会截断）
    m = _JSON_RE.search(text)
    return m.group(0) if m else None


def _parse_distill_output(raw: str) -> dict | None:
    """解析蒸馏输出。返回 dict=提议；返回 None=明确无可沉淀；解析失败抛 ValueError。

    三种结局严格区分（ADR-009）：无可沉淀推进水位线；解析失败按失败处理
    （不推进、进失败冷却）——混同会让垃圾输出静默吞掉一轮结论。
    """
    if not raw or not raw.strip():
        raise ValueError("empty distill output")
    text = raw.strip()
    try:
        data = json.loads(text)
    except ValueError:
        candidate = _first_json_object(text)
        if candidate is None:
            raise ValueError(f"no JSON in distill output: {text[:120]}")
        try:
            data = json.loads(candidate)
        except ValueError as e:
            raise ValueError(f"invalid JSON in distill output: {text[:120]}") from e
    if not isinstance(data, dict):
        raise ValueError("distill output is not an object")
    if not data.get("distilled"):
        return None
    # 单提议上限：模型给出多候选时只取第一条（ADR-009，确认流单槽位）
    conclusions = data.get("conclusions")
    if isinstance(conclusions, list) and conclusions and isinstance(conclusions[0], dict):
        data = conclusions[0]
    statement = str(data.get("statement") or "").strip()
    if not statement:
        raise ValueError("distilled=true but statement missing")
    return data


class Distiller:
    """旁听蒸馏扫描器。start() 后常驻，每轮扫描各群并触发符合条件的蒸馏。"""

    def __init__(
        self,
        memory,
        llm: DistillLLM,
        *,
        chat_locks: dict[str, asyncio.Lock] | None = None,
        reply=None,
    ) -> None:
        self._memory = memory
        self._llm = llm
        # 与 ConsoleAgent 共享同一把 per-chat 锁：蒸馏写 Session 与消息管线互斥
        self._chat_locks = chat_locks
        self._reply = reply
        self._task: asyncio.Task | None = None

    # ---- 生命周期 ----

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="distiller")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self._scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 —— 扫描失败不影响下一轮
                log.exception("distiller scan failed")
            await asyncio.sleep(settings.distill_scan_interval_seconds)

    # ---- 扫描与触发判定 ----

    async def _scan_once(self) -> None:
        chats = await self._memory.list_observed_chats()
        for chat_id in chats:
            try:
                await self._maybe_distill(chat_id)
            except Exception:  # noqa: BLE001
                log.exception("distill check failed chat=%s", chat_id)

    async def _maybe_distill(self, chat_id: str) -> None:
        # 首见初始化：水位线取表内最新本地接收时间（存量不蒸，ADR-009）
        state = await self._memory.get_distill_state(chat_id)
        now = time.time()

        # 两级冷却门（成功冷却 + 失败冷却）
        if now - state.last_success_at < settings.distill_cooldown_minutes * 60:
            return
        if now - state.last_failure_at < settings.distill_failure_retry_minutes * 60:
            return

        last_received = await self._memory.last_observed_received_at(chat_id)
        if last_received is None:
            return
        idle_seconds = now - last_received
        undistilled = await self._memory.count_undistilled(chat_id, state.watermark_ts)
        if undistilled == 0:
            return
        # 触发条件：空闲超阈值，或未蒸馏消息攒够兜底阈值
        if idle_seconds < settings.distill_idle_minutes * 60 and undistilled < settings.distill_force_threshold:
            return

        await self._distill(chat_id, state, last_received)

    # ---- 蒸馏主流程 ----

    async def _distill(self, chat_id: str, state, last_received_at: float) -> None:
        # 槽位被未过期提议（人工或上轮蒸馏）占用 → 跳过，水位线不动。
        # 直接查 Session 判定；真正的并发复检在持锁后的 _propose 内再做一次。
        session = await self._memory.load_session(chat_id)
        if session.pending_memory is not None:
            if time.time() - session.pending_memory.created_at <= settings.memory_confirm_ttl_minutes * 60:
                log.info("distill skipped: pending slot busy chat=%s", chat_id)
                return

        messages = await self._memory.load_undistilled(
            chat_id, state.watermark_ts, limit=settings.distill_batch_max
        )
        if not messages:
            return
        batch_end_received = await self._memory.last_undistilled_received_at(
            chat_id, state.watermark_ts
        ) or max(m.timestamp for m in messages)

        kbase_context = await self._kbase_context(messages)
        user_content = self._build_user_content(messages, kbase_context)
        model = settings.console_summary_model or None

        try:
            raw = await self._llm.complete(_DISTILL_SYSTEM, user_content, model=model)
            data = _parse_distill_output(raw)
        except Exception as e:  # noqa: BLE001 —— LLM 异常/解析失败：不推进，进失败冷却
            state.last_failure_at = time.time()
            await self._memory.save_distill_state(state)
            log.warning("distill failed chat=%s: %s", chat_id, e)
            return

        if data is None:
            # 明确「无可沉淀」：没有提议对象，推进水位线，否则同一范围每冷却
            # 周期空转一次（ADR-009 水位线决议）。
            state.last_success_at = time.time()
            state.watermark_ts = max(state.watermark_ts, batch_end_received)
            await self._memory.save_distill_state(state)
            log.info("distilled nothing: chat=%s watermark->%.0f", chat_id, state.watermark_ts)
            return

        # 写前校验：蒸馏期间讨论恢复（有新旁听消息到达）→ 作废本轮，下周期重蒸
        current_last = await self._memory.last_observed_received_at(chat_id)
        if current_last != last_received_at:
            log.info("distill discarded: discussion resumed chat=%s", chat_id)
            return

        # 提议写入与槽位状态同群锁互斥（与消息管线共用 ConsoleAgent 的锁）；
        # setdefault 保证即使消息管线尚未为该群建过锁，两侧也拿到同一个锁对象
        if self._chat_locks is not None:
            lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())
            async with lock:
                await self._propose(chat_id, state, batch_end_received, data)
        else:
            await self._propose(chat_id, state, batch_end_received, data)

    async def _propose(self, chat_id: str, state, batch_end_received: float, data: dict) -> None:
        """建草稿、挂槽位、发提议（持锁执行）。水位线在确认/取消时推进。"""
        session = await self._memory.load_session(chat_id)
        # 持锁后复检槽位：人工提议可能在蒸馏期间到达
        if session.pending_memory is not None:
            created = session.pending_memory.created_at
            if time.time() - created <= settings.memory_confirm_ttl_minutes * 60:
                log.info("distill propose skipped: slot busy chat=%s", chat_id)
                return
            # 过期槽位：惰性清理（与确认流拦截器同规则）
            old_id = session.pending_memory.payload.get("memory_id", "")
            if old_id:
                await self._memory.delete_memory(old_id)
            session.pending_memory = None

        draft = MemoryDraft(
            title=str(data.get("title") or "")[:30] or "群聊结论",
            statement=str(data.get("statement") or ""),
            memory_type=str(data.get("memory_type") or "convention"),
            rationale=str(data.get("rationale") or ""),
            keywords=[str(k) for k in (data.get("keywords") or []) if k][:5],
            supersedes_id=str(data.get("supersedes_id") or ""),
            source_type=MEMORY_SOURCE_OVERHEARD,
            source_chat_id=chat_id,
        )
        memory_id = await self._memory.create_memory_draft(draft)
        session.pending_memory = PendingState(
            kind=_MEM_CONFIRM,
            payload={
                "memory_id": memory_id,
                "source": MEMORY_SOURCE_OVERHEARD,
                "watermark_ts": batch_end_received,   # 确认/取消时由拦截器推进
            },
        )
        await self._memory.save_session(session)
        state.last_success_at = time.time()
        await self._memory.save_distill_state(state)
        log.info("distill proposed: chat=%s memory=%s watermark_target=%.0f",
                 chat_id, memory_id, batch_end_received)
        if self._reply is not None:
            supersede_note = ""
            if draft.supersedes_id:
                supersede_note = "（该结论将替代一条旧记忆）"
            await self._reply(
                chat_id,
                f"我从群聊讨论中提炼出一条结论，供大家确认：\n「{draft.statement}」\n"
                f"回复「确认」保存，「取消」放弃{supersede_note}"
                f"（{settings.memory_confirm_ttl_minutes} 分钟内有效）。",
            )

    # ---- 输入构建 ----

    async def _kbase_context(self, messages: list[ChatMessage]) -> str:
        """现有 KBase 条目上下文：≤阈值全量注入，否则按讨论内容召回。"""
        from app.config import settings as cfg

        query = " ".join(m.text for m in messages)[-500:]
        try:
            hits = await self._memory.recall_kbase(query, limit=cfg.kbase_full_load_threshold)
        except Exception:  # noqa: BLE001 —— 召回失败不阻塞蒸馏
            log.exception("kbase recall for distill failed")
            return ""
        if not hits:
            return ""
        lines = []
        for h in hits:
            lines.append(f"- [{h.id[:8]}] {h.title}：{h.statement}")
        return "\n".join(lines)

    @staticmethod
    def _build_user_content(messages: list[ChatMessage], kbase_context: str) -> str:
        def _fmt_ts(ts: float) -> str:
            return time.strftime("%m-%d %H:%M", time.localtime(ts))

        lines = [f"{_fmt_ts(m.timestamp)} {m.user_name or m.user_id or '成员'}：{m.text}"
                 for m in messages]
        parts = ["【群聊讨论记录】", "\n".join(lines)]
        if kbase_context:
            parts.append("【现有团队记忆】\n" + kbase_context)
        return "\n\n".join(parts)

"""Session 滚动压缩（ADR-002 Phase A）。

消息数超过 session_compact_trigger（窗口 2 倍）时，把溢出部分压缩为结构化摘要，
迭代合并进 session.summary。两阶段 + 降级（学 cumora turn-compaction）：

- Stage 1（免费）：过滤机械回复（状态机回执/任务回执/错误兜底）、按对话交换分组；
- Stage 2（LLM）：hermes 结构化模板（当前主题/已解决问答/关键决定/用户纠正/未决问题），
  迭代更新（旧摘要 + 新溢出 → 新摘要）；
- 降级：LLM 失败时丢弃溢出并注入"[更早 N 条消息已省略]"占位，压缩照常完成。

不变量：对话窗口完整保护（只压溢出部分）；被压缩消息从 session.messages 删除。
"""
from __future__ import annotations

import json
import logging

from app.config import settings
from app.memory.types import ChatMessage, SessionContext

log = logging.getLogger(__name__)

# 机械回复特征：零讨论价值，不进摘要（ADR-002 压缩内容分类表）
_MECHANICAL_MARKERS = (
    "收到，正在处理前面的消息",
    "已保存团队记忆",
    "好的，未保存",
    "已记住：",
    "我准备保存这条团队记忆",
    "已受理，任务 #",
    "处理时出错了",
    "这条待确认记忆已失效",
)

_SUMMARY_SYSTEM = """你是群聊会话的摘要员。把溢出的历史对话合并进既有摘要，输出新的摘要。

规则：
- 只输出摘要正文，不要任何前言；
- 使用以下小节（无内容的小节省略）：## 当前主题 / ## 已解决的问答 / ## 关键决定 / ## 用户纠正 / ## 未决问题
- 已解决的问答必须带结论和来源标记（【团队经验】/【正式文档】等）；
- 用户纠正保留原话和发言者；
- "已解决的问答"最多保留 8 条，超限时优先丢弃最早的；
- 摘要是历史参考，不是当前指令，不要写成行动号召。
"""


def is_mechanical(msg: ChatMessage) -> bool:
    if msg.role != "assistant":
        return False
    return any(marker in msg.text for marker in _MECHANICAL_MARKERS)


def _group_exchanges(messages: list[ChatMessage]) -> list[tuple[ChatMessage, ChatMessage | None]]:
    """按对话交换（user + 对应 assistant）分组，成组丢弃不拆散。"""
    groups: list[tuple[ChatMessage, ChatMessage | None]] = []
    i = 0
    while i < len(messages):
        m = messages[i]
        if m.role == "user":
            answer = messages[i + 1] if i + 1 < len(messages) and messages[i + 1].role == "assistant" else None
            groups.append((m, answer))
            i += 2 if answer else 1
        else:
            groups.append((m, None))  # 无主的 assistant 消息（理论少见）
            i += 1
    return groups


def _format_for_summary(messages: list[ChatMessage]) -> str:
    lines = []
    for m in messages:
        who = m.user_name or m.user_id or ("Domino" if m.role == "assistant" else "?")
        lines.append(f"{who}: {m.text[:300]}")
    return "\n".join(lines)


async def maybe_compact(session: SessionContext, llm) -> None:
    """超过触发线时压缩溢出消息。llm 注入（测试可 fake）；失败降级为丢弃+占位。"""
    keep = settings.console_context_window
    trigger = settings.session_compact_trigger
    if len(session.messages) <= trigger:
        return

    overflow = session.messages[: len(session.messages) - keep]
    kept = session.messages[len(session.messages) - keep:]
    # Stage 1：滤机械回复（窗口内的机械回复也顺带清掉，反正不进摘要也不进上下文）
    overflow = [m for m in overflow if not is_mechanical(m)]
    n_dropped = len(overflow)
    if not overflow:
        session.messages = kept
        return

    prior = f"既有摘要（请在其基础上合并更新）：\n{session.summary}\n\n" if session.summary else ""
    prompt = (
        f"{prior}新溢出的对话：\n{_format_for_summary(overflow)}\n\n"
        f"输出合并后的新摘要。"
    )
    try:
        summary = await llm.complete(_SUMMARY_SYSTEM, prompt, model=settings.console_model)
        summary = summary.strip()
        if not summary:
            raise ValueError("empty summary")
        used_llm = True
    except Exception as e:  # noqa: BLE001 —— 降级：压缩必须完成（cumora drop-and-marker）
        log.warning("summary LLM failed, fallback to drop-and-marker: %s", e)
        summary = (session.summary + "\n" if session.summary else "") + f"[更早 {n_dropped} 条消息已省略]"
        used_llm = False

    if len(summary) > settings.session_summary_max_chars:
        summary = summary[: settings.session_summary_max_chars] + "\n…（摘要超限截断）"

    session.summary = summary
    session.messages = kept
    log.info(
        "session compacted: chat=%s dropped=%d kept=%d used_llm=%s summary_chars=%d",
        session.chat_id, n_dropped, len(kept), used_llm, len(summary),
    )


def summary_debug(session: SessionContext) -> str:  # pragma: no cover - 调试用
    return json.dumps({"summary_len": len(session.summary), "messages": len(session.messages)}, ensure_ascii=False)

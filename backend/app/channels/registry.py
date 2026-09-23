"""ChannelRegistry：chat_id → 通道寻址（ADR-004 详细设计 §4.3）。

SandboxAgent 回传经此寻址，不 import 具体通道；main.py 装配时注册。
"""
from __future__ import annotations

import logging
from typing import Protocol

log = logging.getLogger(__name__)


class ReplyChannel(Protocol):
    """回传所需的通道能力子集（与 ChannelAdapter.reply 同签名）。"""

    async def reply(self, chat_id: str, text: str, *, session_webhook: str = "") -> object: ...


class ChannelRegistry:
    def __init__(self) -> None:
        self._primary: ReplyChannel | None = None

    def register(self, channel: ReplyChannel) -> None:
        self._primary = channel

    async def send(self, chat_id: str, text: str) -> bool:
        """向来源群发送状态通知。永远不抛异常——通知失败只记日志。"""
        if self._primary is None:
            log.warning("no channel registered, drop notice for chat %s", chat_id)
            return False
        try:
            result = await self._primary.reply(chat_id, text)
            ok = bool(getattr(result, "ok", True))
            if not ok:
                log.warning("channel send failed chat=%s err=%s", chat_id, getattr(result, "error", ""))
            return ok
        except Exception:  # noqa: BLE001
            log.exception("channel send crashed chat=%s", chat_id)
            return False

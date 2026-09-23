"""入口层统一抽象（ADR-001）。

UnifiedMessage 是各 Channel 归一化后的消息结构；ChannelAdapter 是通道接口。
第一阶段只有钉钉一种来源，但保留抽象以便 ConsoleAgent 不依赖具体通道、便于测试与回传寻址。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class UnifiedMessage:
    """归一化消息。is_group 第一阶段恒为 True（仅群聊）。"""

    text: str
    user_id: str
    chat_id: str
    message_id: str
    user_name: str = ""
    chat_name: str = ""
    is_group: bool = True
    session_webhook: str = ""          # 钉钉回消息用的临时 webhook（5 分钟过期）
    timestamp: float = field(default_factory=time.time)
    raw: dict[str, Any] = field(default_factory=dict)   # 原始报文，调试用


@dataclass
class SendResult:
    ok: bool
    error: str = ""


class ChannelAdapter(Protocol):
    """通道接口。收消息经回调注入，发消息经 reply。"""

    async def reply(self, chat_id: str, text: str, *, session_webhook: str = "") -> SendResult:
        """向指定群回复文本（markdown）。"""
        ...

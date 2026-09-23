"""钉钉消息纯函数解析（ADR-001）。

无 I/O、无状态，便于单测。把 SDK 的 ChatbotMessage 报文解析为统一的中间结构。
关键原则：@ 判断用 SDK 结构化的 is_in_at_list，不用正则扒文本（正则会误删邮箱/SSH 中的 @）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ParsedDingTalkMessage:
    text: str
    user_id: str
    user_name: str
    chat_id: str
    chat_name: str
    is_group: bool
    message_id: str
    session_webhook: str
    is_at_bot: bool
    raw: dict[str, Any]


def _get(data: Any, *keys: str, default: Any = "") -> Any:
    """从 dict 或对象上按优先级取第一个非 None 的值。"""
    for k in keys:
        if isinstance(data, dict):
            v = data.get(k)
        else:
            v = getattr(data, k, None)
        if v is not None:
            return v
    return default


def extract_text(data: Any) -> str:
    """提取纯文本内容。钉钉文本在 text.content 或 text 字段。"""
    text_obj = _get(data, "text", default={})
    if isinstance(text_obj, dict):
        content = text_obj.get("content", "")
    else:
        content = getattr(text_obj, "content", "") or ""
    if not content:
        content = _get(data, "content", default="")
    return (content or "").strip()


def extract_is_at_bot(data: Any) -> bool:
    """是否 @ 了机器人。优先用结构化 is_in_at_list / at_users，不用正则。

    SDK 回调对象通常带 is_in_at_list（当前机器人是否在 at 列表里）；
    字典报文则在 atUsers / at_users 中含机器人 staffId 或 atAll。
    """
    # 对象属性（SDK ChatbotMessage）
    for attr in ("is_in_at_list", "isInAtList"):
        v = getattr(data, attr, None)
        if v is not None:
            return bool(v)
        if isinstance(data, dict) and data.get(attr) is not None:
            return bool(data.get(attr))
    # 字典报文 atUsers
    at_users = _get(data, "atUsers", "at_users", "atUserIds", default=[])
    if isinstance(at_users, list) and at_users:
        # 无法在此判定哪个是机器人，交由上层结合机器人 staffId 判断；
        # 这里保守地把「有 at」记为 True，由门控层再核对。
        return True
    return False


def parse_message(data: Any) -> ParsedDingTalkMessage:
    """把 SDK 消息（对象或 dict）解析为中间结构。"""
    conversation_type = str(_get(data, "conversationType", "conversation_type", default="2"))
    is_group = conversation_type != "1"   # "1"=单聊，"2"=群聊
    return ParsedDingTalkMessage(
        text=extract_text(data),
        user_id=str(_get(data, "senderStaffId", "sender_staff_id", "senderId", "sender_id", default="")),
        user_name=str(_get(data, "senderNick", "sender_nick", "senderName", default="")),
        chat_id=str(_get(data, "conversationId", "conversation_id", "chatId", default="")),
        chat_name=str(_get(data, "conversationTitle", "conversation_title", "chatName", default="")),
        is_group=is_group,
        message_id=str(_get(data, "msgId", "msg_id", "messageId", default="")),
        session_webhook=str(_get(data, "sessionWebhook", "session_webhook", default="")),
        is_at_bot=extract_is_at_bot(data),
        raw=data if isinstance(data, dict) else {},
    )

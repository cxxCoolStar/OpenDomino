"""飞书消息纯函数解析（ADR-008）。

无 I/O、无状态，便于单测。把 lark-oapi 事件（对象或 dict）解析为统一中间结构。
关键原则沿用 ADR-001：@ 判断用结构化 mentions 列表，不用正则扒文本。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ParsedFeishuMessage:
    text: str
    user_id: str
    user_name: str
    chat_id: str
    chat_name: str
    is_group: bool
    message_id: str
    is_at_bot: bool
    mentioned_bot: bool          # mentions 结构化判定结果（事件解析时已知机器人身份则由上层覆盖）
    mentioned_ids: list[str] = field(default_factory=list)  # @ 目标 id 列表（open_id/user_id 混合）
    create_time: int = 0         # 毫秒时间戳（飞书原生）
    raw: dict[str, Any] = field(default_factory=dict)


def _get(data: Any, *keys: str, default: Any = "") -> Any:
    """从 dict 或对象上按优先级取第一个非 None 的值（兼容 SDK 对象与事件 JSON dict）。"""
    for k in keys:
        if isinstance(data, dict):
            v = data.get(k)
        else:
            v = getattr(data, k, None)
        if v is not None:
            return v
    return default


def _walk_to_event(data: Any) -> Any:
    """dict 形态的事件 JSON 需下钻到 event 层；SDK 对象自带 .event 属性。"""
    if isinstance(data, dict):
        header_event = data.get("event")
        return header_event if isinstance(header_event, dict) else data
    return _get(data, "event", default=data)


def parse_message(data: Any, *, bot_open_id: str = "", bot_user_id: str = "") -> ParsedFeishuMessage:
    """把 im.message.receive_v1 事件（SDK 对象或 dict）解析为中间结构。

    bot_open_id / bot_user_id：机器人自身身份，用于结构化判定 is_at_bot。
    飞书事件里 mentions[].id.open_id / .user_id 标注了每条 @ 的目标。
    """
    event = _walk_to_event(data)
    message = _get(event, "message", default={})
    sender = _get(event, "sender", default={})
    sender_id = _get(sender, "sender_id", default={})

    message_id = str(_get(message, "message_id", default=""))
    chat_type = str(_get(message, "chat_type", default="group"))
    is_group = chat_type != "p2p"

    user_id = str(
        _get(sender_id, "user_id", default="") or _get(sender_id, "open_id", default="")
    )
    # 飞书事件不带发送者昵称（sender 仅含 id），第一阶段以 user_id 兜底展示；
    # 群名称同理不在事件内，需要 chat API，第一阶段不取。
    user_name = user_id

    text, mentioned_ids = _extract_text_and_mentions(message)
    is_at_bot = _mentions_bot(mentioned_ids, bot_open_id=bot_open_id, bot_user_id=bot_user_id)

    chat_id = str(_get(message, "chat_id", default=""))
    create_time = int(_get(message, "create_time", default=0) or 0)

    return ParsedFeishuMessage(
        text=text,
        user_id=user_id,
        user_name=user_name,
        chat_id=chat_id,
        chat_name="",  # 群名称不在事件内，需要 chat API，第一阶段不取
        is_group=is_group,
        message_id=message_id,
        is_at_bot=is_at_bot,
        mentioned_bot=is_at_bot,
        mentioned_ids=mentioned_ids,
        create_time=create_time,
        raw=data if isinstance(data, dict) else {},
    )


def _extract_text_and_mentions(message: Any) -> tuple[str, list[str]]:
    """提取文本与 @ 目标列表。text.content 为 JSON 字符串（text/post 两类），mentions 为结构化 @ 列表。

    飞书正文中 @某人 是占位符（@_user_1 按序编号），真实名字在 mentions[].key/name。
    返回前用该映射把占位符还原为显示名（@_user_1 → @Domino），LLM 才能读懂
    「谁被 @ 了」；回拉链路（_parse_item_body）经 _placeholder_map 共用本还原逻辑。
    """
    mentions: list[str] = []
    placeholder_map: dict[str, str] = {}
    raw_mentions = _get(message, "mentions", default=[]) or []
    for m in raw_mentions:
        mid = _get(m, "id", default={})
        key = str(_get(m, "key", default="") or "")
        name = str(_get(m, "name", default="") or "")
        for id_key in ("open_id", "user_id", "union_id"):
            v = _get(mid, id_key, default="")
            if v:
                mentions.append(str(v))
                break
        else:
            if name:
                mentions.append(name)  # key 无 id 时以 name 兜底（@_all 等）
        if key and name:
            placeholder_map[key] = name

    content_str = _get(_get(message, "text", default={}), "content", default="") or str(
        _get(message, "content", default="")
    )
    message_type = str(_get(message, "message_type", default="text"))

    if message_type == "text":
        # text.content 是 {"text":"..."} 的 JSON 字符串
        try:
            obj = json.loads(content_str) if isinstance(content_str, str) else content_str
            text = str(obj.get("text", "")) if isinstance(obj, dict) else str(content_str)
        except (ValueError, TypeError):
            text = str(content_str)
        text = _strip_at_tags(text)
        text = _restore_placeholders(text, placeholder_map)
        return text.strip(), mentions

    if message_type == "post":
        return _restore_placeholders(_render_post_text(content_str), placeholder_map), mentions

    # 非文本类型：第一阶段记占位符（ADR-008 仅文本口径）
    return f"[不支持的消息类型: {message_type}]", mentions


def _restore_placeholders(text: str, placeholder_map: dict[str, str]) -> str:
    """把飞书 @ 占位符（@_user_1 等）替换为真实显示名。"""
    if not text or not placeholder_map:
        return text
    for key, name in placeholder_map.items():
        text = text.replace(key, f"@{name}")
    return text


def _strip_at_tags(text: str) -> str:
    """去掉 <at user_id="...">名字</at> 内联标签，保留名字。@ 结构化判定不依赖它。"""
    import re

    return re.sub(r"<at[^>]*>([^<]*)</at>", r"\1", text or "")


def _render_post_text(content_str: str) -> str:
    """富文本 post：content 为 {title, content[[elements]]} 的 JSON 字符串，拼接 text 元素。"""
    try:
        obj = json.loads(content_str) if isinstance(content_str, str) else content_str
    except (ValueError, TypeError):
        return ""
    if not isinstance(obj, dict):
        return ""
    title = str(obj.get("title", "") or "")
    lines: list[str] = []
    for row in obj.get("content", []) or []:
        if not isinstance(row, list):
            continue
        parts = []
        for el in row:
            if isinstance(el, dict) and el.get("tag") == "text":
                parts.append(str(el.get("text", "") or ""))
            elif isinstance(el, dict) and el.get("tag") == "a":
                parts.append(str(el.get("text", "") or el.get("href", "") or ""))
        if parts:
            lines.append("".join(parts))
    body = "\n".join(lines)
    return f"{title}\n{body}".strip() if title else body


def _mentions_bot(mentioned_ids: list[str], *, bot_open_id: str, bot_user_id: str) -> bool:
    """结构化判定：mentions 里是否包含机器人身份；@_all（占位名）视为 @。"""
    for mid in mentioned_ids:
        if bot_open_id and mid == bot_open_id:
            return True
        if bot_user_id and mid == bot_user_id:
            return True
        if mid == "_all":  # 飞书 @所有人 的占位（hermes 同规则）
            return True
    return False
